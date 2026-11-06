#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
心率桌面挂件 —— 读取 iPhone(HeartCast/心率广播) 广播出的标准蓝牙心率带信号，
在桌面右上角(可拖动)显示一个常驻置顶的小卡片：❤ + 心率 + 心率区间。

右键菜单可断开/重连、开关鼠标穿透、退出。
鼠标穿透开启后窗口点击会穿到下面，按 Ctrl+Alt+H 可恢复。
"""
import os
import sys
import json
import math
import time
import queue
import ctypes
import ctypes.wintypes
import threading
import traceback

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)      # 打包成 exe 后，配置文件放 exe 旁边
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(APP_DIR, "挂件设置.json")
LOG_PATH = os.path.join(APP_DIR, "挂件.log")

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_CHAR = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"
BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
SCAN_LOCK = threading.Lock()
DATA_TIMEOUT = 20.0

# 透明色键：这个颜色的像素会变成透明（窗口无底板，只有文字和爱心）
KEY_COLOR = "#010203"
CARD_BG = "#0e131b"
CARD_LINE = "#33405a"
TEXT = "#ffd166"
HEART_COLOR = "#ff3b5c"
SHADOW = "#04060a"
MUTED = "#7b8798"

ZONES = [
    (0.00, 0.60, "热身", "#4f9dff"),
    (0.60, 0.70, "燃脂", "#35d07f"),
    (0.70, 0.80, "有氧", "#ffd23f"),
    (0.80, 0.90, "阈值", "#ff8a3d"),
    (0.90, 9.99, "极限", "#ff3b5c"),
]

DEFAULTS = {
    "x": None,
    "y": None,
    "clickthrough": False,
    "show_zone": False,
    "maxhr": 190,
    "name_hint": "heartcast",
    "address": "",
}


def log(*args):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + " ".join(str(a) for a in args) + "\n")
    except Exception:
        pass


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log("save cfg failed", e)
        return False


def zone_of(bpm, maxhr):
    ratio = bpm / float(maxhr or 190)
    for lo, hi, name, color in ZONES:
        if lo <= ratio < hi:
            return name, color
    return ZONES[-1][2], ZONES[-1][3]


def ease_beat(p):
    """0..1 的心跳周期 -> 缩放系数，模仿 CSS 的 keyframes beat"""
    pts = [(0.00, 1.00), (0.10, 1.16), (0.20, 0.97), (0.30, 1.07), (0.42, 1.00), (1.00, 1.00)]
    for i in range(len(pts) - 1):
        t0, v0 = pts[i]
        t1, v1 = pts[i + 1]
        if p <= t1:
            k = 0 if t1 == t0 else (p - t0) / (t1 - t0)
            return v0 + (v1 - v0) * k
    return 1.0


# --------------------------------------------------------------------------
# 蓝牙线程
# --------------------------------------------------------------------------
async def scan_devices(scanner_cls, timeout, stop_event, match=None, on_update=None):
    """同一时刻只运行一个扫描器；发现目标即可返回，完整扫描可逐步更新列表。"""
    import asyncio

    while not SCAN_LOCK.acquire(blocking=False):
        if stop_event.is_set():
            return {}
        await asyncio.sleep(0.05)
    found = {}
    matched = asyncio.Event()
    last_update = 0.0

    def detected(dev, adv):
        nonlocal last_update
        found[dev.address] = (dev, adv)
        if match is not None and match(dev, adv):
            matched.set()
        now = time.monotonic()
        if on_update is not None and now - last_update >= 0.25:
            last_update = now
            on_update(dict(found))

    try:
        if stop_event.is_set():
            return {}
        async with scanner_cls(detection_callback=detected, scanning_mode="active"):
            deadline = time.monotonic() + timeout
            while not stop_event.is_set() and not matched.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(matched.wait(), min(0.1, remaining))
                except TimeoutError:
                    pass
        return found
    finally:
        SCAN_LOCK.release()


class BleWorker(threading.Thread):
    def __init__(self, q, cfg, device=None):
        super().__init__(daemon=True)
        self.q = q
        self.cfg = dict(cfg)
        self.device = device
        self.stop_evt = threading.Event()
        self.loop = None
        self.task = None
        self.last_packet_at = 0.0
        self.awaiting_first_packet = False

    def stop(self):
        if self.stop_evt.is_set():
            return
        self.stop_evt.set()
        loop, task = self.loop, self.task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    def run(self):
        try:
            import asyncio
            asyncio.run(self._run())
        except Exception:
            self.q.put(("error", traceback.format_exc(limit=2)))
            log(traceback.format_exc())

    async def _run(self):
        import asyncio

        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        try:
            if not self.stop_evt.is_set():
                await self._main()
        except asyncio.CancelledError:
            if not self.stop_evt.is_set():
                raise
        finally:
            self.task = None
            self.loop = None

    async def _main(self):
        import asyncio
        from bleak import BleakClient, BleakScanner
        from bleak.exc import BleakCharacteristicNotFoundError

        refresh_services = False
        while not self.stop_evt.is_set():
            attempt_started = time.monotonic()
            device = None
            try:
                device, self.device = self.device, None
                if device is None:
                    self.q.put(("status", "searching"))
                    device = await self._find(BleakScanner)
            except Exception as e:
                self.q.put(("error", "扫描失败：%s" % e))
                log("scan error", type(e).__name__, repr(e))

            if self.stop_evt.is_set():
                break
            if device is None:
                self.q.put(("status", "nodevice"))
                await self._sleep(1.0)
                continue

            self.q.put(("status", "connecting"))
            disconnected = asyncio.Event()
            try:
                options = {"use_cached_services": False} if refresh_services else {}
                async with BleakClient(
                    device, timeout=12, services=[HR_SERVICE, BATTERY_SERVICE],
                    disconnected_callback=lambda c: disconnected.set(), winrt=options,
                ) as client:
                    if self.stop_evt.is_set():
                        break
                    reason = await self._connected_session(client, device, disconnected, attempt_started)
                    log("disconnected", device.address, reason)
                    # 已验证设备断开后先直接重试一次，失败才重新扫描。
                    self.device = device
            except BleakCharacteristicNotFoundError as e:
                self.q.put(("status", "noservice"))
                log("notify error", type(e).__name__, repr(e), "refresh services")
                if not refresh_services:
                    self.device = device
                refresh_services = True
            except Exception as e:
                self.q.put(("error", "连接失败：%s" % e))
                log("connect error", type(e).__name__, repr(e),
                    "%.2fs" % (time.monotonic() - attempt_started))
            finally:
                self.q.put(("device", None))
                self.q.put(("status", "lost"))

            if self.stop_evt.is_set():
                break
            await self._sleep(0.5 if self.device is not None else 1.5)

    async def _connected_session(self, client, device, disconnected, attempt_started):
        import asyncio

        self.last_packet_at = 0.0
        self.awaiting_first_packet = True
        self.notify_started_at = time.monotonic()
        async with asyncio.timeout(5.0):
            await client.start_notify(HR_CHAR, self._on_data)
        ready_at = time.monotonic()
        self.q.put(("device", device))
        self.q.put(("status", "connected"))
        log("heart rate ready", device.name, device.address,
            "%.2fs" % (ready_at - attempt_started))
        battery_task = asyncio.create_task(self._read_battery(client))
        try:
            while client.is_connected and not self.stop_evt.is_set() and not disconnected.is_set():
                if time.monotonic() - (self.last_packet_at or ready_at) >= DATA_TIMEOUT:
                    self.q.put(("status", "stale"))
                    return "no heart rate for %.0fs; reconnecting" % DATA_TIMEOUT
                try:
                    await asyncio.wait_for(disconnected.wait(), 0.5)
                except TimeoutError:
                    pass
            return "stopped" if self.stop_evt.is_set() else "link lost"
        finally:
            battery_task.cancel()
            await asyncio.gather(battery_task, return_exceptions=True)

    async def _read_battery(self, client):
        import asyncio

        try:
            async with asyncio.timeout(2.0):
                data = await client.read_gatt_char(BATTERY_CHAR)
                if data:
                    self.q.put(("battery", int(data[0])))
        except Exception:
            pass

    async def _sleep(self, seconds):
        import asyncio
        step = 0.2
        waited = 0.0
        while waited < seconds and not self.stop_evt.is_set():
            await asyncio.sleep(step)
            waited += step

    async def _find(self, BleakScanner):
        addr = (self.cfg.get("address") or "").strip().upper()
        hint = (self.cfg.get("name_hint") or "").strip().lower()

        def preferred(dev, adv):
            if addr:
                return (dev.address or "").upper() == addr
            name = (adv.local_name or dev.name or "").lower()
            if hint:
                return hint in name
            return HR_SERVICE in [str(u).lower() for u in (adv.service_uuids or [])]

        started = time.monotonic()
        found = await scan_devices(BleakScanner, 6.0, self.stop_evt, match=preferred)

        for dev, adv in found.values():
            if preferred(dev, adv):
                log("device found", adv.local_name or dev.name, dev.address,
                    "%.2fs" % (time.monotonic() - started))
                return dev

        if addr:
            for a, (dev, adv) in found.items():
                if (dev.address or "").upper() == addr:
                    return dev
            # 用户指定设备后只等这一个，避免误连附近的其他心率带。
            return None

        for a, (dev, adv) in found.items():
            try:
                uuids = [str(u).lower() for u in (adv.service_uuids or [])]
            except Exception:
                uuids = []
            if HR_SERVICE in uuids:
                return dev

        if hint:
            for a, (dev, adv) in found.items():
                if hint in (dev.name or "").lower():
                    return dev

        for a, (dev, adv) in found.items():
            n = (dev.name or "").lower()
            if any(k in n for k in ("heart", "hrm", "polar", "h10", "心率")):
                return dev

        return None

    def _on_data(self, characteristic, data: bytearray):
        try:
            if not data or len(data) < 2:
                return
            flags = data[0]
            i = 1
            if flags & 0x01:
                if len(data) < 3:
                    return
                bpm = data[i] | (data[i + 1] << 8)
                i += 2
            else:
                bpm = data[i]
                i += 1
            if bpm <= 0 or bpm > 260:
                return
            self.last_packet_at = time.monotonic()
            if self.awaiting_first_packet:
                self.awaiting_first_packet = False
                log("first heart rate", bpm, "%.2fs" % (self.last_packet_at - self.notify_started_at))
            self.q.put(("hr", bpm))
        except Exception as e:
            log("parse error", e)


# --------------------------------------------------------------------------
# 设备选择：扫描在线程里运行，所有界面操作都留在 Tk 主线程。
# --------------------------------------------------------------------------
class DevicePicker:
    def __init__(self, overlay):
        from tkinter import ttk

        self.overlay = overlay
        tk = overlay.tk
        self.window = tk.Toplevel(overlay.root)
        self.window.title("选择设备")
        self.window.attributes("-topmost", True)
        self.window.geometry("760x450")
        self.window.minsize(620, 360)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.closed = False
        self.scanning = False
        self.results = queue.Queue()
        self.scan_stop = threading.Event()
        self.devices = {}
        self.scan_rows = []

        frame = ttk.Frame(self.window, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        self.connection_text = tk.StringVar()
        ttk.Label(frame, textvariable=self.connection_text).grid(row=0, column=0, sticky="w")
        saved = overlay.cfg.get("address") or "自动选择"
        ttk.Label(frame, text="已记住的地址：" + saved).grid(row=1, column=0, sticky="w")
        ttk.Label(frame, text="单击设备即可连接并记住地址；心率带排在前面。").grid(
            row=2, column=0, sticky="w", pady=(4, 10))
        table = ttk.Frame(frame)
        table.grid(row=3, column=0, sticky="nsew")
        self.tree = ttk.Treeview(table, columns=("name", "address", "hr", "state"),
                                 show="headings", selectmode="browse")
        for key, title, width in (("name", "设备名称", 260), ("address", "MAC 地址", 180),
                                  ("hr", "心率带", 110), ("state", "状态", 100)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=90, stretch=key == "name")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<ButtonRelease-1>", self._click_device)
        self.tree.bind("<Return>", lambda e: self.connect_selected())

        self.status = tk.StringVar()
        status_label = ttk.Label(frame, textvariable=self.status, wraplength=650)
        status_label.grid(row=4, column=0, sticky="w", pady=(10, 4))
        frame.bind("<Configure>", lambda e: status_label.configure(wraplength=max(100, e.width - 28)))
        ttk.Label(frame, text="“未广播”表示未在广播中发现心率服务，仍可尝试连接。").grid(
            row=5, column=0, sticky="w")
        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, sticky="ew", pady=(12, 0))
        self.scan_button = ttk.Button(buttons, text="重新扫描", command=self.scan)
        self.scan_button.pack(side="left")
        ttk.Button(buttons, text="恢复自动选择", command=self.use_auto).pack(side="left", padx=8)
        ttk.Button(buttons, text="关闭", command=self.close).pack(side="right")
        self.connect_button = ttk.Button(buttons, text="连接所选设备", command=self.connect_selected,
                                         state="disabled")
        self.connect_button.pack(side="right", padx=8)
        self.window.bind("<Escape>", lambda e: self.close())
        self.poll_id = self.window.after(100, self._poll)
        self.scan()

    def scan(self):
        if self.scanning or self.closed:
            return
        self.scanning = True
        self.scan_button.configure(state="disabled")
        self.scan_rows = []
        self.refresh_devices()
        self.status.set("正在扫描附近设备，发现后立即显示，约 8 秒完成…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def refresh_devices(self):
        if self.closed:
            return
        selected = self.tree.selection()
        previous = self.devices.get(selected[0]) if selected else None
        selected_address = (previous.address if previous else self.overlay.cfg.get("address") or "").upper()
        current = self.overlay.current_device
        current_address = current.address.upper() if current else ""
        self.connection_text.set("当前连接：%s  %s" % (current.name or "（无名设备）", current.address)
                                 if current else "当前连接：未连接")
        rows = [(dev, name, is_hr, "扫描发现") for dev, name, is_hr in self.scan_rows
                if dev.address.upper() != current_address]
        if current:
            # 订阅成功的设备已确认支持心率，不依赖它是否仍在广播。
            rows.insert(0, (current, current.name or "（无名设备）", True, "当前连接"))
        self.tree.delete(*self.tree.get_children())
        self.devices.clear()
        for index, (dev, name, is_hr, state) in enumerate(rows):
            key = str(index)
            self.devices[key] = dev
            hr = "是（已验证）" if state == "当前连接" else "是" if is_hr else "未广播"
            self.tree.insert("", "end", iid=key, values=(name, dev.address, hr, state))
            if dev.address.upper() == selected_address:
                self.tree.selection_set(key)
                self.tree.focus(key)
                self.tree.see(key)
        self.connect_button.configure(state="normal" if rows else "disabled")

    def _scan_worker(self):
        try:
            import asyncio
            from bleak import BleakScanner

            def publish(found, kind="devices_progress"):
                rows = []
                for dev, adv in found.values():
                    name = adv.local_name or dev.name or "（无名设备）"
                    is_hr = HR_SERVICE in [str(u).lower() for u in (adv.service_uuids or [])]
                    rows.append((dev, name, is_hr))
                rows.sort(key=lambda row: (not row[2], row[1].casefold(), row[0].address))
                self.results.put((kind, rows))

            found = asyncio.run(scan_devices(BleakScanner, 8.0, self.scan_stop, on_update=publish))
            if not self.scan_stop.is_set():
                publish(found, "devices")
        except Exception as e:
            log("device picker scan error", e)
            self.results.put(("error", str(e)))

    def _poll(self):
        if self.closed:
            return
        if self.poll_id is not None:
            self.window.after_cancel(self.poll_id)
            self.poll_id = None
        try:
            kind, payload = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            if kind != "devices_progress":
                self.scanning = False
                self.scan_button.configure(state="normal")
            if kind == "error":
                self.status.set("扫描失败：%s。请检查电脑蓝牙后重新扫描。" % payload)
            else:
                self.scan_rows = payload
                if kind == "devices_progress":
                    self.status.set("正在扫描，已发现 %d 个设备，单击即可连接…" % len(payload))
                elif payload or self.overlay.current_device:
                    self.status.set("扫描发现 %d 个设备；当前连接设备始终保留在顶部，单击即可记住地址。" % len(payload))
                else:
                    self.status.set("未发现设备。请打开电脑蓝牙，并让手机解锁、HeartCast 在前台广播后重试。")
            self.refresh_devices()
        self.poll_id = self.window.after(100, self._poll)

    def _click_device(self, event):
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return
        row = self.tree.identify_row(event.y)
        if row and self.tree.selection() == (row,):
            # 扫描期间行可能重新排序，鼠标松开时只连接仍选中的那一行。
            self.connect_selected()

    def connect_selected(self):
        selected = self.tree.selection()
        if not selected or self.closed:
            return
        device = self.devices.get(selected[0])
        if device is not None:
            self._choose(device.address, device)

    def use_auto(self):
        self._choose("")

    def _choose(self, address, device=None):
        old_address = self.overlay.cfg.get("address", "")
        self.overlay.cfg["address"] = address
        if not save_cfg(self.overlay.cfg):
            self.overlay.cfg["address"] = old_address
            self.status.set("无法保存地址，请检查挂件目录的写入权限后重试。")
            return
        log("selected device", address or "auto")
        current = self.overlay.current_device
        if not (current and address.upper() == current.address.upper()):
            self.overlay.reconnect(device=device)
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.scan_stop.set()
        if self.poll_id is not None:
            self.window.after_cancel(self.poll_id)
            self.poll_id = None
        self.window.destroy()
        self.overlay.device_picker = None


# --------------------------------------------------------------------------
# 鼠标穿透 / 全局热键
# --------------------------------------------------------------------------
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1


def set_clickthrough(hwnd, enabled):
    try:
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enabled:
            style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
            style |= WS_EX_LAYERED
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        return True
    except Exception as e:
        log("clickthrough failed", e)
        return False


def keep_topmost(hwnd):
    try:
        ctypes.windll.user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
    except Exception:
        pass


def start_hotkey(q):
    """在独立线程里注册 Ctrl+Alt+H，成功后返回 True"""
    result = {"ok": False}

    def run():
        try:
            user32 = ctypes.windll.user32
            if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT, ord("H")):
                return
            result["ok"] = True
            msg = ctypes.wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    return
                if msg.message == WM_HOTKEY:
                    q.put(("hotkey", None))
        except Exception as e:
            log("hotkey thread error", e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.25)
    return result["ok"]


# --------------------------------------------------------------------------
# 界面
# --------------------------------------------------------------------------
class Overlay:
    def __init__(self):
        import tkinter as tk

        self.tk = tk
        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.ble_q = queue.Queue()
        self.device_picker = None
        self.current_device = None
        self._reconnect_id = 0

        self.root = tk.Tk()
        self.root.title("心率挂件")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=KEY_COLOR)
        try:
            self.root.attributes("-transparentcolor", KEY_COLOR)
        except Exception as e:
            log("transparentcolor unsupported", e)

        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.W = int(140 * self.scale)
        self.H = int(60 * self.scale)

        x, y = self._initial_pos()
        self.root.geometry("%dx%d+%d+%d" % (self.W, self.H, x, y))

        self.canvas = tk.Canvas(
            self.root, width=self.W, height=self.H,
            bg=KEY_COLOR, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        self.hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()

        self.bpm = 0
        self.last_data_at = 0.0
        self.status = "idle"
        self.battery = None
        self.zone_name, self.zone_color = "未连接", MUTED
        self.phase_origin = time.time()
        self.clickthrough = False

        self._build_items()
        self._bind_events()
        self._build_menu()

        self.hotkey_ok = start_hotkey(self.q)
        if self.hotkey_ok:
            log("hotkey Ctrl+Alt+H ok")

        if self.cfg.get("clickthrough"):
            self.toggle_clickthrough(True, quiet=True)

        self.worker = BleWorker(self.ble_q, self.cfg)
        self.worker.start()

        self.root.after(60, self._tick)

    # ---------------- 位置 ----------------
    def _initial_pos(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = self.cfg.get("x")
        y = self.cfg.get("y")
        if not isinstance(x, int) or not isinstance(y, int) or x < -self.W or y < -self.H:
            x = sw - self.W - 28
            y = 28
        x = max(-self.W + 40, min(x, sw - 40))
        y = max(0, min(y, sh - 40))
        return x, y

    # ---------------- 绘制 ----------------
    def _make_text(self, x, y, text, fill, font, anchor="w", outline=True):
        """文字 + 四周深色描边，保证在浅色桌面/深色游戏上都看得清"""
        c = self.canvas
        items = []
        if outline:
            o = max(0.9, 1.0 * self.scale)
            for dx, dy in ((-o, -o), (o, -o), (-o, o), (o, o)):
                items.append(c.create_text(x + dx, y + dy, text=text, anchor=anchor,
                                           fill=SHADOW, font=font))
        main = c.create_text(x, y, text=text, anchor=anchor, fill=fill, font=font)
        items.append(main)
        return items

    def _build_items(self):
        c = self.canvas
        s = self.scale

        self.heart_pos = (int(29 * s), int(30 * s))
        self.heart_size = 23 * s
        self.heart = c.create_polygon(0, 0, 0, 0, fill=HEART_COLOR, outline="")
        c.coords(self.heart, *self._heart_points(1.0))

        num_font = ("Bahnschrift SemiCondensed", int(-27 * s), "bold")
        self.num_items = self._make_text(int(52 * s), int(28 * s), "--", TEXT, num_font, "w")
        self.num = self.num_items[-1]

        zone_font = ("Microsoft YaHei UI", int(-9 * s))
        self.zone_items = self._make_text(int(53 * s), int(51 * s),
                                          "未连接", MUTED, zone_font, "w", outline=False)
        self.zone = self.zone_items[-1]

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        c = self.canvas
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return c.create_polygon(pts, smooth=True, **kw)

    def _heart_points(self, scale):
        cx, cy = getattr(self, "heart_pos", (29, 30))
        size = getattr(self, "heart_size", 22.0)
        pts = []
        for i in range(48):
            t = 2 * math.pi * i / 48
            hx = 16 * math.sin(t) ** 3
            hy = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
            pts.append(cx + hx * (size / 32.0) * scale)
            pts.append(cy + hy * (size / 32.0) * scale - 1)
        return pts

    # ---------------- 事件 ----------------
    def _bind_events(self):
        self._drag = None
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_menu)

    def _on_press(self, e):
        self._drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _on_drag(self, e):
        if not self._drag:
            return
        sx, sy, wx, wy = self._drag
        self.root.geometry("+%d+%d" % (wx + e.x_root - sx, wy + e.y_root - sy))

    def _on_release(self, e):
        if self._drag:
            self.cfg["x"] = self.root.winfo_x()
            self.cfg["y"] = self.root.winfo_y()
            save_cfg(self.cfg)
        self._drag = None

    def _on_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _build_menu(self):
        tk = self.tk
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="选择设备…", command=self.select_device)
        m.add_command(label="重新连接", command=self.reconnect)
        m.add_separator()
        self.var_zone = tk.BooleanVar(value=bool(self.cfg.get("show_zone", True)))
        m.add_checkbutton(label="显示心率区间", variable=self.var_zone, command=self._apply_zone_toggle)
        self.var_ct = tk.BooleanVar(value=bool(self.cfg.get("clickthrough", False)))
        m.add_checkbutton(label="鼠标穿透（Ctrl+Alt+H 恢复）", variable=self.var_ct,
                          command=lambda: self.toggle_clickthrough(self.var_ct.get()))
        m.add_separator()
        m.add_command(label="重置位置到右上角", command=self.reset_pos)
        m.add_command(label="退出", command=self.quit)
        self.menu = m

    def _apply_zone_toggle(self):
        self.cfg["show_zone"] = bool(self.var_zone.get())
        save_cfg(self.cfg)
        self._last_zone = None
        self._redraw()

    def select_device(self):
        if self.device_picker is not None and not self.device_picker.closed:
            self.device_picker.window.lift()
            self.device_picker.window.focus_force()
            return
        self.device_picker = DevicePicker(self)

    def reconnect(self, device=None):
        try:
            self.worker.stop()
        except Exception:
            pass
        self.bpm = 0
        self.last_data_at = 0.0
        self.battery = None
        self.status = "searching"
        self.current_device = None
        if self.device_picker is not None:
            self.device_picker.refresh_devices()
        # 丢弃旧连接的消息，等它释放蓝牙后再连接新设备。
        self.ble_q = queue.Queue()
        self._reconnect_id += 1
        self._finish_reconnect(self._reconnect_id, device)

    def _finish_reconnect(self, request_id, device):
        if request_id != self._reconnect_id:
            return
        if self.worker.is_alive():
            self.root.after(100, lambda: self._finish_reconnect(request_id, device))
            return
        self.worker = BleWorker(self.ble_q, self.cfg, device=device)
        self.worker.start()

    def reset_pos(self):
        sw = self.root.winfo_screenwidth()
        x = sw - self.W - 28
        self.root.geometry("+%d+%d" % (x, 28))
        self.cfg["x"], self.cfg["y"] = x, 28
        save_cfg(self.cfg)

    def toggle_clickthrough(self, enabled, quiet=False):
        if enabled and not self.hotkey_ok:
            self.var_ct.set(False)
            if not quiet:
                self._flash("热键失败")
            return
        ok = set_clickthrough(self.hwnd, enabled)
        self.clickthrough = bool(ok and enabled)
        self.cfg["clickthrough"] = self.clickthrough
        save_cfg(self.cfg)
        if hasattr(self, "var_ct"):
            self.var_ct.set(self.clickthrough)
        if self.clickthrough and not quiet:
            self._flash("穿透·按 Ctrl+Alt+H 恢复")

    def _flash(self, text, ms=2600):
        self._flash_until = time.time() + ms / 1000.0
        self._flash_text = text

    # ---------------- 主循环 ----------------
    def _tick(self):
        try:
            self._drain_queue()
            self._redraw()
            now = time.time()
            if now - getattr(self, "_last_top", 0) > 2.0:
                self._last_top = now
                keep_topmost(self.hwnd)
        except Exception:
            log("tick error", traceback.format_exc())
        self.root.after(33, self._tick)

    def _drain_queue(self):
        for source in (self.q, self.ble_q):
            self._drain_source(source)

    def _drain_source(self, source):
        while True:
            try:
                kind, payload = source.get_nowait()
            except queue.Empty:
                return
            if kind == "hr":
                self.bpm = int(payload)
                self.last_data_at = time.time()
                self.phase_origin = self.last_data_at
            elif kind == "status":
                self.status = payload
            elif kind == "battery":
                self.battery = payload
            elif kind == "device":
                self.current_device = payload
                if self.device_picker is not None:
                    self.device_picker.refresh_devices()
            elif kind == "hotkey":
                if self.clickthrough:
                    self.toggle_clickthrough(False)
                    self._flash("已恢复点击")
                else:
                    self.toggle_clickthrough(True)
            elif kind == "error":
                log("worker:", payload)

    def _redraw(self):
        c = self.canvas
        now = time.time()
        maxhr = int(self.cfg.get("maxhr") or 190)
        fresh = self.status == "connected" and bool(self.bpm) and (now - self.last_data_at) < 6

        if fresh:
            zname, zcolor = zone_of(self.bpm, maxhr)
            period = 60.0 / max(30, self.bpm)
            phase = ((now - self.last_data_at) % period) / period
            scale = ease_beat(phase)
            text = str(self.bpm)
        else:
            zname = {
                "searching": "搜索中…",
                "connecting": "连接中…",
                "connected": "等待数据",
                "lost": "已断开",
                "nodevice": "未找到设备",
                "noservice": "无心率服务",
                "stale": "数据中断，重连中…",
            }.get(self.status, "未连接")
            zcolor = MUTED
            text = "--"
            scale = 1.0

        if getattr(self, "_flash_until", 0) > now:
            zname, zcolor = self._flash_text, "#ffc44d"

        show_zone = bool(self.cfg.get("show_zone"))
        if fresh:
            z_text, z_fill, z_vis, z_shadow = zname, zcolor, show_zone, False
        else:
            z_text, z_fill, z_vis, z_shadow = zname, TEXT, True, True

        if text != getattr(self, "_last_text", None):
            self._last_text = text
            for it in self.num_items:
                c.itemconfigure(it, text=text)
        key = (z_text, z_fill, z_vis, z_shadow)
        if key != getattr(self, "_last_zone", None):
            self._last_zone = key
            for it in self.zone_items[:-1]:
                c.itemconfigure(it, text=z_text,
                                state="normal" if (z_vis and z_shadow) else "hidden")
            c.itemconfigure(self.zone, text=z_text, fill=z_fill,
                            state="normal" if z_vis else "hidden")
        c.coords(self.heart, *self._heart_points(scale))

    def quit(self):
        try:
            self.worker.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)


# --------------------------------------------------------------------------
def run_scan():
    import asyncio
    from bleak import BleakScanner

    async def main():
        print("扫描 8 秒，列出所有广播设备…\n")
        found = await BleakScanner.discover(timeout=8.0, return_adv=True)
        rows = []
        for addr, (dev, adv) in found.items():
            uuids = [str(u).lower() for u in (adv.service_uuids or [])]
            rows.append((HR_SERVICE in uuids, dev.name or "", addr))
        rows.sort(key=lambda r: (not r[0], r[1]))
        for is_hr, name, addr in rows:
            mark = "  <== 心率带" if is_hr else ""
            print("%-28s %-20s%s" % ((name or "(无名)")[:28], addr, mark))
        print("\n共 %d 个设备。" % len(rows))

    asyncio.run(main())
    try:
        input("\n按回车退出…")
    except EOFError:
        pass


def enable_dpi():
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def selftest():
    ov = Overlay()
    try:
        ov.worker.stop()
    except Exception:
        pass
    ov.q.put(("status", "connected"))
    ov.q.put(("hr", 78))
    end = time.time() + 1.2
    while time.time() < end:
        ov.root.update()
        time.sleep(0.02)
    print("SELFTEST OK: num=%r zone=%r hwnd=%s" % (
        ov.canvas.itemcget(ov.num, "text"),
        ov.canvas.itemcget(ov.zone, "text"),
        ov.hwnd,
    ))
    ov.root.destroy()


def main():
    if "--scan" in sys.argv:
        run_scan()
        return
    enable_dpi()
    if "--selftest" in sys.argv:
        selftest()
        return
    try:
        ov = Overlay()
        if "--demo" in sys.argv:
            import random

            def feed():
                v = random.randint(68, 150)
                if random.random() < 0.08:
                    v = random.randint(52, 60)
                ov.q.put(("status", "connected"))
                ov.q.put(("hr", v))
                ov.root.after(1000, feed)

            ov.root.after(600, feed)
        ov.root.mainloop()
    except Exception:
        log(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
