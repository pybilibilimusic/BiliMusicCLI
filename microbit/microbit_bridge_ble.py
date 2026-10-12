"""BLE 版 micro:bit 桥接（主机侧）。

板子的 USB 串口(CDC) 物理链路坏了，蓝牙是绕开它的那条路。这一份是
`microbit_bridge.MicrobitBridge` 的子类 —— **只把传输层从串口换成 BLE**，
协议解析、状态缓存、keepalive、回调分发全部复用父类，串口那份一行没改。

用法跟串口版完全一样（除了不用传 port）：
    bridge = MicrobitBleBridge(callbacks={
        "toggle_pause": fn, "next_track": fn, "prev_track": fn,
        "stop": fn, "adjust_volume": fn(delta), "set_volume": fn(value),
        "get_state": fn() -> dict, "log": fn(message),
    })
    bridge.start()
    bridge.send_state()

⚠️ micro:bit 的 UART 服务跟 Nordic 标准是反的（血泪坑，别改回标准写法）：
    6e400003 = 电脑往这写（真·RX）
    6e400002 = 板子从这发（真·TX，用 indicate 不是 notify）
"""

import asyncio
import ctypes
import queue
import threading
import time
import sys
from typing import Optional

import os

# 设 BILIMUSIC_MICROBIT_DEBUG=1 才把 BLE 的堆栈写进 temp/microbit_ble.log
DEBUG = os.environ.get("BILIMUSIC_MICROBIT_DEBUG") == "1"

from microbit_bridge import MicrobitBridge, _KEEPALIVE_INTERVAL

# ⚠️ 反的，别照抄标准 NUS
UART_TO_BOARD = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"      # 电脑 -> 板子
UART_FROM_BOARD = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"    # 板子 -> 电脑

CHUNK = 19          # 板子的 BLE 缓冲只有 20 字节，留 1 给换行
CHUNK_GAP = 0.02    # 分片之间喘口气，连着猛写会把板上协议栈写崩（崩溃码 070）
DEFAULT_SCAN_SECONDS = 10.0
CONNECT_WAIT_EXTRA = 20.0
RECONNECT_INTERVAL = 5.0   # 掉线/连不上之后隔多久再试（静默重试，不刷屏）


def _tb() -> str:
    """取当前异常堆栈。独立出来是因为只有调试时才需要。"""
    import traceback
    return traceback.format_exc()


class MicrobitBleBridge(MicrobitBridge):
    """和 MicrobitBridge 同一套 API，底下走 BLE。

    bleak 是异步的，但 main_CUI 是同步的，所以这里在后台线程里跑一个
    自己的 asyncio 循环；主线程照旧用 send_* 往队列里塞，循环负责发出去。
    """

    def __init__(self, callbacks=None, scan_seconds: float = DEFAULT_SCAN_SECONDS):
        super().__init__(port="", baudrate=0, callbacks=callbacks)
        self.scan_seconds = scan_seconds
        self._client = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # type: ignore
        self._thread: Optional[threading.Thread] = None         # type: ignore
        self._ready = threading.Event()
        self._outbox: "queue.Queue[str]" = queue.Queue()
        self._rx_buffer = ""
        self._linked = False

    # ---------- 生命周期 ----------

    def start(self) -> bool:
        """后台起 BLE 循环，等到真的连上（或超时）才返回。"""
        if self._running:
            return True
        self._running = True
        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

        ok = self._ready.wait(self.scan_seconds + CONNECT_WAIT_EXTRA)
        if ok:
            # 跟串口版保持一致：连上就先把状态推过去
            self.send_status("stop")
        return bool(ok)

    def stop(self):
        self._running = False
        self._linked = False
        loop = self._loop
        # ⚠️ 循环没在跑的时候别调度，否则协程永远等不到 await，
        #    退出时刷一屏 "coroutine was never awaited" 警告
        if loop is not None:
            try:
                running = loop.is_running()
            except Exception:
                running = False
            if running:
                try:
                    fut = asyncio.run_coroutine_threadsafe(self._async_stop(), loop)
                    fut.result(timeout=6)
                except Exception:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=6)
            self._thread = None
        self._client = None
        self._loop = None

    @property
    def connected(self) -> bool:
        return bool(self._linked and self._running)

    # ---------- 发送（主线程调用） ----------

    def _write(self, payload: str) -> bool:
        """只入队，真正发送在后台循环里做。"""
        if not self._linked:
            return False
        self._outbox.put(payload)
        return True

    # ---------- 后台线程 ----------

    def _thread_main(self):
        # ⚠️ 血泪坑：bleak 的 WinRT 后端要求**多线程套间(MTA)**，
        #    而 Python 新建的线程默认不初始化 COM —— 不写这两行的话
        #    扫描能跑，但 BleakClient.connect() 会直接卡死（实测）。
        #    反过来初始化成 STA 更糟：bleak 内部 assert_mta() 会抛错。
        #    主线程本来就满足，所以 ble_test.py / ble_demo.py 从来没遇到。
        _com_ready = False
        if sys.platform == "win32":
            try:
                import ctypes
                # COINIT_MULTITHREADED = 0x0
                ctypes.windll.ole32.CoInitializeEx(None, 0x0)
                _com_ready = True
            except Exception:
                _com_ready = False
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._async_main())
        except Exception as exc:
            self._log("micro:bit BLE loop failed: %r" % (exc,))
        finally:
            try:
                if self._loop is not None:
                    self._loop.close()
            except Exception:
                pass
            if _com_ready:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass

    async def _async_main(self):
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            self._log("micro:bit BLE: 没装 bleak（pip install bleak）")
            return

        dev = await self._find_board(BleakScanner)
        if dev is None:
            self._log("micro:bit BLE: 没扫到板子（板子通电了吗？固件是 MakeCode 吗？）")
            return

        # 板子偶发连不上（WinRT 侧常见 "Unreachable"），而且掉线也是常态 ——
        # 所以在 _running 期间一直重连，不达成就安静等着，别刷屏打扰用户。
        attempt = 0
        while self._running:
            attempt += 1
            try:
                await self._connect_and_serve(BleakClient, dev)
                if not self._running:
                    return
            except Exception as exc:
                self._debug("connect failed #%d: %r\n%s"
                            % (attempt, exc, _tb()))
            if attempt == 1:
                # 第一次失败才告诉用户一声，后面的重试都静默
                self._log("micro:bit 还没连上（还在重试）")
            await asyncio.sleep(RECONNECT_INTERVAL)

    async def _connect_and_serve(self, client_cls, dev):
        try:
            async with client_cls(dev) as client:
                self._client = client
                await client.start_notify(UART_FROM_BOARD, self._on_notify)
                self._linked = True
                self._ready.set()
                self._log("connected on BLE (%s)" % getattr(dev, "address", "?"))

                last_keepalive = time.time()
                while self._running:
                    await asyncio.sleep(0.1)
                    await self._drain_outbox()
                    if time.time() - last_keepalive > _KEEPALIVE_INTERVAL:
                        last_keepalive = time.time()
                        self.send_state()
        except Exception as exc:
            # ⚠️ 以前这里直接把整段 traceback 打给用户，一旦连不上就刷满屏，
            #    把正在看的搜索结果全冲掉了。现在堆栈只进调试通道（_debug），
            #    用户侧只看到一句短提示。
            self._debug("BLE error: %r\n%s" % (exc, _tb()))
            raise          # 抛给上层，好让它重连
        finally:
            self._linked = False
            self._client = None
            self._ready.set()      # 免得 start() 一直等到超时

    async def _async_stop(self):
        client = self._client
        if client is not None:
            try:
                await client.stop_notify(UART_FROM_BOARD)
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass
        self._client = None

    def _debug(self, message: str):
        """调试信息：只在 BILIMUSIC_MICROBIT_DEBUG=1 时写文件，不占控制台。"""
        if not DEBUG:
            return
        try:
            import os
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "temp", "microbit_ble.log")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("%s  %s\n" % (time.strftime("%H:%M:%S"), message))
        except Exception:
            pass

    async def _find_board(self, scanner_cls):
        try:
            result = await scanner_cls.discover(timeout=self.scan_seconds,
                                                return_adv=True)
            pairs = list(result.values())
        except TypeError:
            devs = await scanner_cls.discover(timeout=self.scan_seconds)
            pairs = [(d, None) for d in devs]

        for dev, _adv in pairs:
            name = (getattr(dev, "name", "") or "").lower()
            if "micro:bit" in name or "bbc" in name:
                return dev
        return None

    async def _drain_outbox(self):
        while True:
            try:
                line = self._outbox.get_nowait()
            except queue.Empty:
                return
            await self._send_line(line)

    async def _send_line(self, line: str):
        """板子的 BLE 缓冲只有 20 字节，长了必须分片。"""
        if self._client is None:
            return
        payload = (line + "\n").encode("utf-8", "replace")
        try:
            for i in range(0, len(payload), CHUNK):
                await self._client.write_gatt_char(UART_TO_BOARD,
                                                   payload[i:i + CHUNK],
                                                   response=False)
                if len(payload) > CHUNK:
                    await asyncio.sleep(CHUNK_GAP)
        except Exception as exc:
            self._debug("BLE write failed: %r" % (exc,))

    # ---------- 接收（BLE 线程调用） ----------

    def _on_notify(self, *args):
        """bleak 各版本回调签名不同，最后一个参数一定是 bytes。"""
        data = args[-1]
        try:
            text = data.decode("utf-8", "replace")
        except Exception:
            return
        # 一次通知可能不是一整行，攒起来再按 \n 切
        self._rx_buffer += text
        while "\n" in self._rx_buffer:
            line, _, self._rx_buffer = self._rx_buffer.partition("\n")
            line = line.strip()
            if line:
                self.handle_line(line)
