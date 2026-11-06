"""设备选择、持久化及切换连接的回归测试；不接触真实蓝牙或用户配置。"""
import asyncio
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


SOURCE = Path(__file__).resolve().parents[1] / "heart_overlay.pyw"
loader = importlib.machinery.SourceFileLoader("heart_overlay", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
app = importlib.util.module_from_spec(spec)
loader.exec_module(app)


def device(address, name="HeartCast"):
    return SimpleNamespace(address=address, name=name)


class IdleWorker:
    instances = []

    def __init__(self, q, cfg, device=None):
        self.q, self.cfg, self.device = q, dict(cfg), device
        self.alive = False
        self.stopped = False
        self.instances.append(self)

    def start(self):
        self.alive = True

    def stop(self):
        self.stopped = True

    def is_alive(self):
        return self.alive


class DeviceMatchingTests(unittest.TestCase):
    def test_explicit_address_never_falls_back_to_other_heart_rate_device(self):
        other = device("AA:00:00:00:00:01")

        worker = app.BleWorker(queue.Queue(), {"address": "AA:00:00:00:00:02"})
        found = {other.address: (other, SimpleNamespace(local_name=other.name, service_uuids=[app.HR_SERVICE]))}
        with patch.object(app, "scan_devices", new=AsyncMock(return_value=found)), patch.object(app, "log"):
            self.assertIsNone(asyncio.run(worker._find(None)))
            worker.cfg["address"] = other.address.lower()
            self.assertIs(asyncio.run(worker._find(None)), other)
            worker.cfg["address"] = ""
            self.assertIs(asyncio.run(worker._find(None)), other)

    def test_stopped_scan_cannot_start_a_connection(self):
        worker = app.BleWorker(queue.Queue(), {})

        async def find(scanner):
            worker.stop()
            return device("AA:00:00:00:00:01")

        worker._find = find
        with patch("bleak.BleakClient") as client:
            asyncio.run(worker._main())
            client.assert_not_called()

    def test_current_device_reported_only_after_successful_heart_rate_subscription(self):
        target = device("AA:00:00:00:00:02")
        for notify_succeeds in (True, False):
            with self.subTest(notify_succeeds=notify_succeeds):
                worker = app.BleWorker(queue.Queue(), {}, target)

                class Client:
                    is_connected = True

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args):
                        pass

                    async def start_notify(self, *args):
                        if not notify_succeeds:
                            raise RuntimeError("no heart rate characteristic")

                    async def read_gatt_char(self, *args):
                        worker.stop()
                        return b""

                async def stop_waiting(seconds):
                    worker.stop()

                worker._sleep = stop_waiting
                with patch("bleak.BleakClient", return_value=Client()), patch.object(app, "log"):
                    asyncio.run(worker._main())
                events = []
                while not worker.q.empty():
                    events.append(worker.q.get_nowait())
                self.assertEqual(("device", target) in events, notify_succeeds)
                self.assertEqual(("status", "connected") in events, notify_succeeds)
                self.assertIn(("device", None), events)


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patches = [
            patch.object(app, "CFG_PATH", str(Path(self.temp.name) / "settings.json")),
            patch.object(app, "LOG_PATH", str(Path(self.temp.name) / "test.log")),
            patch.object(app, "start_hotkey", return_value=False),
            patch.object(app, "BleWorker", IdleWorker),
            patch.object(app.DevicePicker, "_scan_worker", lambda self: None),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.overlay = app.Overlay()
        self.addCleanup(self.close_overlay)

    def close_overlay(self):
        if self.overlay.device_picker is not None:
            self.overlay.device_picker.close()
        for callback in self.overlay.root.tk.call("after", "info"):
            self.overlay.root.after_cancel(callback)
        self.overlay.root.destroy()

    def picker_with_rows(self, rows):
        self.overlay.select_device()
        picker = self.overlay.device_picker
        picker.results.put(("devices", rows))
        picker._poll()
        self.overlay.root.update()
        return picker

    def pump(self):
        end = time.monotonic() + 0.2
        while time.monotonic() < end:
            self.overlay.root.update()
            time.sleep(0.01)

    def test_click_persists_address_and_waits_for_old_connection(self):
        target = device("AA:00:00:00:00:02")
        old = self.overlay.worker
        old.q.put(("hr", 123))
        picker = self.picker_with_rows([(target, "HeartCast 客厅", True)])
        bounds = picker.tree.bbox("0")
        self.assertTrue(bounds)
        picker.tree.event_generate("<ButtonPress-1>", x=bounds[0] + 20, y=bounds[1] + 5)
        picker.tree.event_generate("<ButtonRelease-1>", x=bounds[0] + 20, y=bounds[1] + 5)
        self.overlay.root.update()
        self.assertTrue(picker.closed)
        self.assertTrue(old.stopped)
        self.assertIs(self.overlay.worker, old)
        self.assertEqual(self.overlay.bpm, 0)
        self.assertEqual(app.load_cfg()["address"], target.address)
        old.alive = False
        self.pump()
        self.assertIs(self.overlay.worker.device, target)
        self.assertEqual(self.overlay.worker.cfg["address"], target.address)
        self.overlay.worker.q.put(("hr", 78))
        self.overlay._drain_queue()
        self.assertEqual(self.overlay.bpm, 78)

    def test_saved_device_preselected_and_auto_choice_clears_address(self):
        target = device("AA:00:00:00:00:02")
        self.overlay.cfg["address"] = target.address.lower()
        picker = self.picker_with_rows([(target, "HeartCast", True)])
        self.assertEqual(picker.tree.selection(), ("0",))
        picker.use_auto()
        self.assertEqual(app.load_cfg()["address"], "")
        self.assertTrue(picker.closed)

    def test_save_failure_keeps_picker_and_previous_address(self):
        self.overlay.cfg["address"] = "old"
        picker = self.picker_with_rows([(device("new"), "HeartCast", True)])
        picker.tree.selection_set("0")
        with patch.object(app, "save_cfg", return_value=False):
            picker.connect_selected()
        self.assertFalse(picker.closed)
        self.assertEqual(self.overlay.cfg["address"], "old")
        self.assertIn("无法保存", picker.status.get())
        self.assertFalse(self.overlay.worker.stopped)

    def test_empty_scan_error_retry_close_and_reopen(self):
        picker = self.picker_with_rows([])
        self.assertIn("未发现设备", picker.status.get())
        picker.scan()
        picker.results.put(("error", "adapter unavailable"))
        picker._poll()
        self.assertIn("扫描失败", picker.status.get())
        self.assertFalse(picker.scanning)
        self.overlay.select_device()
        self.assertIs(self.overlay.device_picker, picker)
        picker.close()
        picker.results.put(("devices", []))
        picker._poll()  # 关闭后的异步结果不得再访问已销毁的 Tk 控件。
        self.overlay.select_device()
        self.assertIsNot(self.overlay.device_picker, picker)

    def test_latest_reconnect_wins(self):
        old = self.overlay.worker
        self.overlay.reconnect(device("first"))
        self.overlay.cfg["address"] = "second"
        second = device("second")
        self.overlay.reconnect(second)
        old.alive = False
        self.pump()
        self.assertIs(self.overlay.worker.device, second)
        self.assertEqual(self.overlay.worker.cfg["address"], "second")

    def test_buttons_remain_fully_visible_in_small_window(self):
        picker = self.picker_with_rows([])
        picker.window.geometry("560x320")
        self.overlay.root.update()
        buttons = [picker.scan_button, picker.connect_button]
        for button in buttons:
            self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight())
            bottom = button.winfo_rooty() + button.winfo_height()
            self.assertLessEqual(bottom, picker.window.winfo_rooty() + picker.window.winfo_height())
        self.assertGreater(picker.tree.winfo_height(), 30)

    def test_connected_device_visible_even_without_advertisements(self):
        current = device("AA:00:00:00:00:02", "HeartCast-iPhon")
        self.overlay.ble_q.put(("device", current))
        self.overlay._drain_queue()
        picker = self.picker_with_rows([])
        values = picker.tree.item("0", "values")
        self.assertEqual(values, (current.name, current.address, "是（已验证）", "当前连接"))
        self.assertIn(current.address, picker.connection_text.get())
        picker.scan()
        self.assertEqual(len(picker.tree.get_children()), 1)
        self.assertEqual(picker.tree.item("0", "values")[-1], "当前连接")

    def test_current_device_merged_once_and_click_saves_without_reconnect(self):
        current = device("AA:00:00:00:00:02", "HeartCast-iPhon")
        self.overlay.current_device = current
        old = self.overlay.worker
        self.overlay.bpm = 78
        picker = self.picker_with_rows([
            (device(current.address.lower(), None), "（无名设备）", False),
            (device("AA:00:00:00:00:03"), "Other", True),
        ])
        self.assertEqual(len(picker.tree.get_children()), 2)
        self.assertEqual(picker.tree.item("0", "values")[0], current.name)
        picker.tree.selection_set("0")
        picker.connect_selected()
        self.assertEqual(app.load_cfg()["address"], current.address)
        self.assertFalse(old.stopped)
        self.assertIs(self.overlay.worker, old)
        self.assertEqual(self.overlay.bpm, 78)

    def test_current_device_survives_scan_error_but_disconnect_removes_badge(self):
        current = device("AA:00:00:00:00:02")
        self.overlay.current_device = current
        picker = self.picker_with_rows([])
        picker.results.put(("error", "unavailable"))
        picker._poll()
        self.assertEqual(picker.tree.item("0", "values")[-1], "当前连接")
        self.overlay.ble_q.put(("device", None))
        self.overlay._drain_queue()
        self.assertEqual(picker.tree.get_children(), ())
        self.assertIn("未连接", picker.connection_text.get())

    def test_can_remember_current_device_while_scan_in_progress(self):
        current = device("AA:00:00:00:00:02")
        self.overlay.current_device = current
        self.overlay.select_device()
        picker = self.overlay.device_picker
        self.assertTrue(picker.scanning)
        picker.tree.selection_set("0")
        picker.connect_selected()
        self.assertTrue(picker.closed)
        self.assertEqual(app.load_cfg()["address"], current.address)
        self.assertFalse(self.overlay.worker.stopped)

    def test_can_connect_discovered_device_before_scan_finishes(self):
        target = device("AA:00:00:00:00:02")
        self.overlay.select_device()
        picker = self.overlay.device_picker
        picker.results.put(("devices_progress", [(target, target.name, True)]))
        picker._poll()
        self.assertTrue(picker.scanning)
        picker.tree.selection_set("0")
        picker.connect_selected()
        self.assertTrue(picker.closed)
        self.assertTrue(picker.scan_stop.is_set())
        self.assertEqual(app.load_cfg()["address"], target.address)

    def test_disconnect_immediately_hides_previous_heart_rate(self):
        self.overlay.ble_q.put(("status", "connected"))
        self.overlay.ble_q.put(("hr", 78))
        self.overlay._drain_queue()
        self.overlay._redraw()
        self.assertEqual(self.overlay.canvas.itemcget(self.overlay.num, "text"), "78")
        self.overlay.ble_q.put(("status", "lost"))
        self.overlay._drain_queue()
        self.overlay._redraw()
        self.assertEqual(self.overlay.canvas.itemcget(self.overlay.num, "text"), "--")


class ScannerTests(unittest.TestCase):
    def test_all_devices_retained_and_heart_rate_devices_sorted_first(self):
        hr = device("AA:00:00:00:00:02", "fallback name")
        other = device("AA:00:00:00:00:01", None)

        async def discover(scanner, timeout, stop_event, on_update):
            found = {
                other.address: (other, SimpleNamespace(local_name=None, service_uuids=[])),
                hr.address: (hr, SimpleNamespace(local_name="HeartCast 广播名", service_uuids=[app.HR_SERVICE.upper()])),
            }
            on_update(found)
            return found

        picker = object.__new__(app.DevicePicker)
        picker.results = queue.Queue()
        picker.scan_stop = threading.Event()
        with patch.object(app, "scan_devices", side_effect=discover):
            picker._scan_worker()
        self.assertEqual(picker.results.get_nowait()[0], "devices_progress")
        kind, rows = picker.results.get_nowait()
        self.assertEqual(kind, "devices")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], (hr, "HeartCast 广播名", True))
        self.assertEqual(rows[1], (other, "（无名设备）", False))


if __name__ == "__main__":
    unittest.main()
