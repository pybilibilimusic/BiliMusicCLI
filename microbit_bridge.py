"""
micro:bit 遥控桥接模块。

把串口收发封装成一个「协议 + 回调」的小桥，主程序只需要提供几个动作，
不必再关心串口读写的细节。这样 main_CUI 里那堆 if/elif 也能收干净。

## 串口协议

主机（本程序） → micro:bit：

| 报文 | 含义 | 示例 |
| --- | --- | --- |
| `STATUS:<state>` | 播放状态 | `STATUS:play` / `STATUS:pause` / `STATUS:stop` |
| `VOL:<0-100>` | 音量 | `VOL:65` |
| `TITLE:<text>` | 当前歌名（纯 ASCII） | `TITLE:Jay Chou - Qing Tian` |
| `TIME:<pos>/<dur>` | 播放进度（秒） | `TIME:37/242` |
| `STATE:<state>|<vol>|<pos>/<dur>` | 一次给全，回应 `QUERY` | `STATE:play|65|37/242` |

micro:bit → 主机：

| 报文 | 含义 |
| --- | --- |
| `PAUSE` | 播放 / 暂停切换 |
| `NEXT` / `PREV` | 切下一首 / 上一首 |
| `STOP` | 停止播放 |
| `VOLUP` / `VOLDOWN` | 音量 ±10 |
| `VOL:<n>` | 音量直接设为 n |
| `QUERY` | 请求主机把当前状态全量发回来（掉电重连用） |

每行以 `\n` 结尾，全部 ASCII。串口是 115200 波特率。

## 一个必须知道的坑

micro:bit 自带的 5×5 点阵字体**只有 ASCII**，中文歌名显示出来是一坨方块。
所以 `TITLE:` 会先做 ASCII 化处理：如果环境里装了 `pypinyin` 就把中文转成拼音，
没有就退化成去掉非 ASCII 字符。想要拼音效果就 `pip install pypinyin`（纯可选依赖）。

板端参考实现见 `microbit/microbit_remote.py`。
"""

import threading
import time
from typing import Callable, Dict, Optional

DEFAULT_BAUDRATE = 115200
LINE_ENDING = "\n"

VOLUME_STEP = 10          # VOLUP / VOLDOWN 每次调整的幅度
_KEEPALIVE_INTERVAL = 5   # 秒；空闲时重发一次状态，便于板端判断在线


def ascii_display(text: str) -> str:
    """
    把标题转成 micro:bit 能显示的 ASCII 串。

    装了 pypinyin 就把汉字转拼音（"晴天" -> "Qing Tian"），
    没装就退化为丢弃非 ASCII 字符。

    相邻汉字之间会补空格并首字母大写 —— 不这样的话「晴天」出来是 `qingtian`，
    5×5 点阵上一串连写小写字母根本读不出来。
    """
    if not text:
        return ""
    # 去掉换行：串口是按行读的，标题里带换行会撕裂报文
    cleaned = text.replace("\r", " ").replace("\n", " ").strip()
    if cleaned.isascii():
        return cleaned

    # pypinyin 没装 / 转换失败时的降级：丢掉非 ASCII（日文假名之类的也只能这样）
    def strip_non_ascii(text):
        return "".join(char for char in text if ord(char) < 128)

    try:
        from pypinyin import lazy_pinyin

        out = []
        prev_cjk = False
        for char in cleaned:
            if "\u4e00" <= char <= "\u9fff":
                syllable = lazy_pinyin(char)[0]
                if prev_cjk:
                    out.append(" ")          # 汉字之间补空格，避免连成一坨
                out.append(syllable.capitalize())
                prev_cjk = True
            elif ord(char) < 128:
                out.append(char)
                prev_cjk = False
            # 剩下的（日文假名之类）直接丢掉：点阵字体里它们是一坨方块。
            # 别在这里把 prev_cjk 清掉，否则后续汉字会被判成新词。
        result = "".join(out)
    except Exception:
        result = strip_non_ascii(cleaned)

    return " ".join(result.split())


def clamp_volume(value: int) -> int:
    return max(0, min(100, int(value)))


class MicrobitBridge:
    """
    主机侧的串口桥接。

    用法：
        bridge = MicrobitBridge(port="COM4", callbacks={
            "toggle_pause": fn, "next_track": fn, "prev_track": fn,
            "stop": fn, "adjust_volume": fn(delta), "set_volume": fn(value),
            "get_state": fn() -> dict, "log": fn(message),
        })
        bridge.start()
        bridge.send_status("play")
        bridge.send_title("晴天 - 周杰伦")
        bridge.stop()

    callbacks 里缺哪个功能就自动禁用哪个，不会因为没有实现而崩掉。
    """

    def __init__(self, port: str, baudrate: int = DEFAULT_BAUDRATE,
                 callbacks: Optional[Dict[str, Callable]] = None,
                 serial_factory=None):
        self.port = port
        self.baudrate = baudrate
        self.callbacks = callbacks or {}
        self._serial_factory = serial_factory   # 测试时注入 loop:// 或假串口
        self._serial = None
        self._thread: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()
        self._running = False
        self._last_state = {}                   # 缓存最近一次状态，供 keepalive 重发

    # ---------- 生命周期 ----------

    def start(self) -> bool:
        """打开串口并启动监听线程。返回是否成功。"""
        if self._running:
            return True
        if not self.port:
            return False
        try:
            if self._serial_factory is not None:
                self._serial = self._serial_factory()
            else:
                import serial
                self._serial = serial.Serial(self.port, self.baudrate, timeout=1)
        except Exception as exc:
            self._log(f"micro:bit error: {exc!r}")
            self._serial = None
            return False

        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        self._log(f"micro:bit connected on {self.port}")
        self.send_status("stop")
        return True

    def stop(self):
        self._running = False
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    @property
    def connected(self) -> bool:
        return self._serial is not None and self._running

    # ---------- 发送 ----------

    def _write(self, payload: str) -> bool:
        """落一行到串口。写失败只做降级（置空串口等待重连），不影响主流程。"""
        if self._serial is None:
            return False
        try:
            with self._write_lock:
                self._serial.write((payload + LINE_ENDING).encode("utf-8"))
            return True
        except Exception as exc:
            self._log(f"micro:bit write failed: {exc!r}")
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
            return False

    def send_status(self, status: str):
        self._last_state["status"] = status
        self._write(f"STATUS:{status}")

    def send_volume(self, volume: int):
        volume = clamp_volume(volume)
        self._last_state["volume"] = volume
        self._write(f"VOL:{volume}")

    def send_title(self, title: str):
        safe = ascii_display(title)
        self._last_state["title"] = safe
        self._write(f"TITLE:{safe}")

    def send_time(self, position, duration):
        """进度：两个参数都可以是 None（比如流媒体拿不到总时长）。"""
        pos = f"{int(position)}" if position is not None else "?"
        dur = f"{int(duration)}" if duration is not None else "?"
        self._last_state["time"] = (pos, dur)
        self._write(f"TIME:{pos}/{dur}")

    def send_state(self, compact=False):
        """
        把当前状态一次性推给板子（回应 QUERY，或连接刚建立时同步）。

        :param compact: True 时合并成一行 `STATE:play|65|37/242`，
                        比拆成四行省串口带宽；False（默认）是原来的四封报文。
                        板端 microbit_remote.py 两种都能解析。
        """
        state = self._get_state()
        status = state.get("status", "stop")
        volume = clamp_volume(state.get("volume", 0))
        position, duration = state.get("time", (None, None))

        if compact:
            pos = f"{int(position)}" if position is not None else "?"
            dur = f"{int(duration)}" if duration is not None else "?"
            self._last_state.update({"status": status, "volume": volume,
                                     "time": (pos, dur)})
            self._write(f"STATE:{status}|{volume}|{pos}/{dur}")
            return

        self.send_status(status)
        self.send_volume(volume)
        title = state.get("title")
        if title:
            self.send_title(title)
        self.send_time(position, duration)

    # ---------- 接收与分发 ----------

    def _listen_loop(self):
        """串口读取循环：读一行 → 分发。异常时不崩，降级后退出等待重连。"""
        last_keepalive = time.time()
        try:
            while self._running and self._serial is not None:
                try:
                    raw = self._serial.readline()
                except Exception as exc:
                    self._log(f"micro:bit read failed: {exc!r}")
                    break

                if raw:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if line:
                        self.handle_line(line)

                if time.time() - last_keepalive > _KEEPALIVE_INTERVAL:
                    last_keepalive = time.time()
                    self.send_state()
        except Exception as exc:
            self._log(f"micro:bit error: {exc!r}")
        finally:
            try:
                if self._serial is not None:
                    self._serial.close()
            except Exception:
                pass
            self._serial = None

    def handle_line(self, line: str):
        """解析板子发来的一条指令。独立出来是为了能脱离硬件直接测。"""
        command, _, argument = line.partition(":")
        command = command.strip().upper()
        argument = argument.strip()

        if command == "PAUSE":
            self._call("toggle_pause")
        elif command == "NEXT":
            self._call("next_track")
        elif command == "PREV":
            self._call("prev_track")
        elif command == "STOP":
            self._call("stop")
        elif command == "VOLUP":
            self._call("adjust_volume", VOLUME_STEP)
        elif command == "VOLDOWN":
            self._call("adjust_volume", -VOLUME_STEP)
        elif command == "VOL" and argument.isdigit():
            self._call("set_volume", clamp_volume(argument))
        elif command == "QUERY":
            self.send_state()
        # 未知报文直接忽略：板端可能会吐调试信息，不必报错

    # ---------- 内部工具 ----------

    def _call(self, name: str, *args):
        handler = self.callbacks.get(name)
        if handler is None:
            return None
        try:
            return handler(*args)
        except Exception as exc:
            self._log(f"micro:bit callback '{name}' failed: {exc!r}")
            return None

    def _get_state(self) -> dict:
        state = dict(self._last_state)
        getter = self.callbacks.get("get_state")
        if getter is not None:
            try:
                state.update(getter() or {})
            except Exception as exc:
                self._log(f"micro:bit get_state failed: {exc!r}")
        return state

    def _log(self, message: str):
        logger = self.callbacks.get("log")
        if logger:
            try:
                logger(message)
            except Exception:
                pass
