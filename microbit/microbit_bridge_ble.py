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
RECONNECT_INTERVAL = 5.0   # 扫到了但连不上 -> 隔多久再试（静默重试，不刷屏）
RESCAN_INTERVAL = 30.0     # 根本扫不到（板子关机/拔电池）-> 隔多久重新扫一次
                           # ⚠️ 板子随时会被关掉，所以这里是**永远重试**，不是试几次就放弃

# 判定「其实已经断了」的阈值：
# 板子关机/拔电池/走远时**不会通知主机**，Windows 也要等链路超时才上报，
# 所以只能自己主动问。写不进去攒够这么多次就算断了。
MAX_WRITE_FAILURES = 3
MAX_PENDING = 50           # 待发报文上限，超了就丢（攒着说明链路已经不对了）


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
        self._missing = False        # 当前是不是"找不到板子"状态（用来只在状态变化时说一句）
        self._write_failures = 0     # 连续写失败次数（攒够就判定断线）

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
        if self._outbox.qsize() >= MAX_PENDING:
            # 攒这么多还没发出去，说明链路已经不对劲了。
            # 丢掉比攒着安全 —— 重连成功后会重新推一份最新状态。
            return False
        self._outbox.put(payload)
        return True

    # ---------- 后台线程 ----------

    @staticmethod
    def _cancel_pending(loop):
        """
        关事件循环之前把没跑完的任务收干净。

        不收的话会刷 "Task was destroyed but it is pending!" —— 退出时那句
        说的就是 stop() 从别的线程调度过来的 _async_stop()：它跟主循环
        抢着断开连接，主循环先跑完就把循环关了，它还没落地。
        """
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        except Exception:
            return
        if not pending:
            return
        for task in pending:
            task.cancel()
        try:
            # return_exceptions=True：被取消的任务自己会抛 CancelledError，别再往外冒
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass

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
                    self._cancel_pending(self._loop)
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

        # ⭐ 板子随时会被关机/拔电池（教室场景就是随手开关），所以这里**永不退出**：
        #    扫不到 -> 等 RESCAN_INTERVAL 再扫；扫到了连不上 -> 回去重新扫。
        #    只在"状态变化"时说一句话，别每秒刷屏。
        fails = 0
        while self._running:
            dev = await self._find_board(BleakScanner)
            if dev is None:
                if not self._missing:
                    self._missing = True
                    self._log("micro:bit BLE：没扫到板子（通电了吗？每 %d 秒再找一次）"
                              % int(RESCAN_INTERVAL))
                await asyncio.sleep(RESCAN_INTERVAL)
                continue

            try:
                await self._connect_and_serve(BleakClient, dev)
                fails = 0
                if self._missing:
                    self._missing = False
                    self._log("micro:bit BLE：板子回来了")
            except Exception as exc:
                fails += 1
                self._debug("connect failed #%d: %r\n%s" % (fails, exc, _tb()))
                if fails == 1:
                    # 第一次失败才告诉用户一声，后面的重试都静默
                    self._log("micro:bit 还没连上（还在重试）")

            if not self._running:
                return
            await asyncio.sleep(RECONNECT_INTERVAL)

    async def _connect_and_serve(self, client_cls, dev):
        try:
            async with client_cls(dev) as client:
                self._client = client
                await client.start_notify(UART_FROM_BOARD, self._on_notify)
                self._drop_outbox()          # 上次失联攒下的旧报文别带过来
                self._linked = True
                self._write_failures = 0
                self._ready.set()
                self._log("connected on BLE (%s)" % getattr(dev, "address", "?"))

                last_keepalive = time.time()
                while self._running:
                    await asyncio.sleep(0.1)

                    # ⭐ 谎报军情的根源就在这儿：板子断电/走远时**不会发断开通知**，
                    #    不主动问的话 _linked 会一直挂着 True，心跳就一直说「已连板子」。
                    #    is_connected 由 Windows 的 GATT 层维护，链路超时后会变 False。
                    if not client.is_connected:
                        raise ConnectionError("BLE 连接断了（板子关机、拔电池或超出范围）")

                    await self._drain_outbox()

                    # 链路断了之后 is_connected 可能还要过一阵才翻脸，
                    # 写不进去是更早也更实在的信号。
                    if self._write_failures >= MAX_WRITE_FAILURES:
                        raise ConnectionError(
                            "连续 %d 次写不进板子，判定已断线" % self._write_failures)

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
            self._drop_outbox()
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

    def _drop_outbox(self):
        """
        把待发报文清光。

        失联期间（板子关机那几分钟）keepalive 还在一直往队列里塞 STATE，
        不清的话重连成功那一刻会一次性喷出去几十条，板子 20 字节的缓冲
        直接被写崩（崩溃码 070）。丢了没关系，重连后会重新推最新状态。
        """
        try:
            while True:
                self._outbox.get_nowait()
        except queue.Empty:
            pass

    async def _send_line(self, line: str) -> bool:
        """板子的 BLE 缓冲只有 20 字节，长了必须分片。返回有没有写进去。"""
        if self._client is None:
            return False
        payload = (line + "\n").encode("utf-8", "replace")
        try:
            for i in range(0, len(payload), CHUNK):
                await self._client.write_gatt_char(UART_TO_BOARD,
                                                   payload[i:i + CHUNK],
                                                   response=False)
            if len(payload) > CHUNK:
                await asyncio.sleep(CHUNK_GAP)
            self._write_failures = 0
            return True
        except Exception as exc:
            self._debug("BLE write failed: %r" % (exc,))
            # ⚠️ 以前只打日志不上报，外层循环就一直以为还连着 —— 谎报军情的另一半。
            self._write_failures += 1
            return False

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
