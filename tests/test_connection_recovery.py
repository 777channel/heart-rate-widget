"""连接延迟、取消和无数据恢复的回归测试，不依赖真实蓝牙。"""
import asyncio
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_device_selection import app, device


class DiscoveryTests(unittest.TestCase):
    def test_target_connects_without_waiting_for_full_scan(self):
        target = device("AA:00:00:00:00:01")
        advertisement = SimpleNamespace(local_name="HeartCast-iPhon", service_uuids=[app.HR_SERVICE])
        closed = []

        class Scanner:
            def __init__(self, detection_callback, scanning_mode):
                self.callback = detection_callback

            async def __aenter__(self):
                asyncio.get_running_loop().call_soon(self.callback, target, advertisement)
                return self

            async def __aexit__(self, *args):
                closed.append(True)

        worker = app.BleWorker(queue.Queue(), {"name_hint": "heartcast"})

        async def find():
            return await asyncio.wait_for(worker._find(Scanner), 0.5)

        with patch.object(app, "log"):
            self.assertIs(asyncio.run(find()), target)
        self.assertTrue(closed)

    def test_scanners_never_run_concurrently(self):
        active = 0
        peak = 0

        class Scanner:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                return self

            async def __aexit__(self, *args):
                nonlocal active
                active -= 1

        async def run():
            await asyncio.gather(
                app.scan_devices(Scanner, 0.05, threading.Event()),
                app.scan_devices(Scanner, 0.05, threading.Event()),
            )

        asyncio.run(run())
        self.assertEqual(peak, 1)
        self.assertEqual(active, 0)

    def test_cancelled_scan_releases_adapter_for_next_scan(self):
        entered = asyncio.Event()
        closed = []

        class Scanner:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                entered.set()
                return self

            async def __aexit__(self, *args):
                closed.append(True)

        async def run():
            task = asyncio.create_task(app.scan_devices(Scanner, 6, threading.Event()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(app.scan_devices(Scanner, 0.01, threading.Event()), 0.5)

        asyncio.run(run())
        self.assertEqual(len(closed), 2)


class RecoveryTests(unittest.TestCase):
    def test_stop_cancels_pending_discovery(self):
        worker = app.BleWorker(queue.Queue(), {})
        entered = threading.Event()
        cleaned_up = threading.Event()

        async def find(scanner):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

        worker._find = find
        with patch.object(app, "log"):
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
            finally:
                worker.stop()
                worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(cleaned_up.is_set())

    def test_disconnect_retries_same_device_without_rescanning(self):
        target = device("AA:00:00:00:00:01")
        worker = app.BleWorker(queue.Queue(), {})
        worker._find = AsyncMock(return_value=target)
        worker._sleep = AsyncMock()
        sessions = []

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        async def session(client, dev, disconnected, started):
            sessions.append(dev)
            if len(sessions) == 2:
                worker.stop()
            return "link lost"

        worker._connected_session = session
        with patch("bleak.BleakClient", return_value=Client()), patch.object(app, "log"):
            asyncio.run(worker._main())
        self.assertEqual(sessions, [target, target])
        self.assertEqual(worker._find.await_count, 1)
        worker._sleep.assert_awaited_once_with(0.5)

    def test_failed_cached_device_falls_back_to_fresh_scan(self):
        old = device("AA:00:00:00:00:01")
        new = device("AA:00:00:00:00:02")
        worker = app.BleWorker(queue.Queue(), {}, old)
        worker._find = AsyncMock(return_value=new)
        worker._sleep = AsyncMock()

        class Client:
            def __init__(self, fail):
                self.fail = fail

            async def __aenter__(self):
                if self.fail:
                    raise TimeoutError("old address unavailable")
                return self

            async def __aexit__(self, *args):
                pass

        async def session(client, dev, disconnected, started):
            self.assertIs(dev, new)
            worker.stop()
            return "stopped"

        worker._connected_session = session
        with patch("bleak.BleakClient", side_effect=[Client(True), Client(False)]), patch.object(app, "log"):
            asyncio.run(worker._main())
        worker._find.assert_awaited_once()

    def test_missing_characteristic_retries_with_fresh_windows_services(self):
        from bleak.exc import BleakCharacteristicNotFoundError

        target = device("AA:00:00:00:00:01")
        worker = app.BleWorker(queue.Queue(), {}, target)
        worker._find = AsyncMock()
        worker._sleep = AsyncMock()
        attempts = 0

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        async def session(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise BleakCharacteristicNotFoundError(app.HR_CHAR)
            worker.stop()
            return "stopped"

        worker._connected_session = session
        with patch("bleak.BleakClient", return_value=Client()) as factory, patch.object(app, "log"):
            asyncio.run(worker._main())
        self.assertEqual(factory.call_args_list[1].kwargs["winrt"], {"use_cached_services": False})
        worker._find.assert_not_awaited()

    def test_data_timeout_recovers_even_when_battery_query_hangs(self):
        worker = app.BleWorker(queue.Queue(), {})
        battery_cancelled = []

        class Client:
            is_connected = True

            async def start_notify(self, *args):
                pass

            async def read_gatt_char(self, *args):
                try:
                    await asyncio.Event().wait()
                finally:
                    battery_cancelled.append(True)

        async def run():
            return await asyncio.wait_for(worker._connected_session(
                Client(), device("AA:00:00:00:00:01"), asyncio.Event(), app.time.monotonic()), 1)

        with patch.object(app, "DATA_TIMEOUT", 0.03), patch.object(app, "log"):
            reason = asyncio.run(run())
        self.assertIn("reconnecting", reason)
        self.assertTrue(battery_cancelled)

    def test_valid_packets_keep_connection_alive(self):
        worker = app.BleWorker(queue.Queue(), {})

        class Client:
            is_connected = True

            async def start_notify(self, characteristic, callback):
                self.callback = callback

            async def read_gatt_char(self, *args):
                return b"\x64"

        async def run():
            disconnected = asyncio.Event()
            client = Client()
            task = asyncio.create_task(worker._connected_session(
                client, device("AA:00:00:00:00:01"), disconnected, app.time.monotonic()))
            for _ in range(7):
                await asyncio.sleep(0.1)
                client.callback(None, bytearray([0, 78]))
            self.assertFalse(task.done())
            disconnected.set()
            return await task

        with patch.object(app, "DATA_TIMEOUT", 0.25), patch.object(app, "log"):
            reason = asyncio.run(run())
        self.assertEqual(reason, "link lost")


if __name__ == "__main__":
    unittest.main()
