"""
进度条：播放条，以及它和下载条共用的颜色 / 宽度处理。

这个模块里几处写法都是踩过坑才定下来的，改之前先看看为什么：

1. **只用 `\\r` 原地刷新，不用 ANSI 转义移动光标**。回车符是任何终端都认的，
   而 ANSI 光标控制在 Windows 下 `chcp 65001` 之后不一定生效。
   （ANSI 只用来上色，不用来挪光标。）

2. **命令模式下播放条默认关闭**。后台线程每 0.5 秒写一次 stdout，会把用户
   `input()` 里正在敲的内容顶掉（`progress_bar_verify.py --demo` 可以现场看这个惨状）。
   语音模式没人敲键盘，这个代价不成立，所以在那里开。
   连着 micro:bit 时不刷命令行 —— 那种情况进度走点阵屏，两边同时刷会互相干扰。

3. **整行宽度必须塞得进终端**（2026-10-10 加）。之前那行比控制台窗口宽被折行了，
   `\\r` 只能回到最后一行的行首，折行之后就回不去，于是每刷一次堆一行。
   现在会先量终端宽度，塞不下就缩短条。

4. **宽度用 `unicodedata.east_asian_width` 算，不能用 `ord(ch) > 127` 糊弄**。
   后者会把 `█`(U+2588) 也算成两格 —— 可它在终端里就是一格，算出来偏宽，
   条会被砍得过短。（原来的 `░` 同理，而且它在等宽字体里是一排竖条很难看，已换成空格。）

5. **装饰性符号别用**（2026-10-10）。比如暂停标记原本想用 `⏸`(U+23F8)，
   那属于「杂项符号」区，很多终端字体没这个字形，实测渲染不出来。
   一律用纯 ASCII：暂停用 `||`。
"""

import os
import re
import shutil
import sys
import threading
import unicodedata

BAR_WIDTH = 30
REFRESH_INTERVAL = 0.5      # 秒；再快就是白白刷屏

# 进度条的字符。绝大多数终端都认 █；碰上更保守的终端把它们换成 = / - 就行。
BAR_FILLED = "█"
BAR_EMPTY = " "

# 暂停标记：纯 ASCII，别用 ⏸(U+23F8) 那类杂项符号，很多终端渲染不出来
PAUSE_MARK = "||"

# ---------------- 颜色 ----------------

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CYAN = "\x1b[96m"
GREEN = "\x1b[92m"
YELLOW = "\x1b[93m"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text):
    """把颜色码去掉，留下纯文字 —— 量宽度、擦行都得用它。"""
    return _ANSI_RE.sub("", text)


def enable_ansi():
    """
    让 Windows 的 cmd.exe 认 ANSI 颜色。

    `os.system("")` 是个流传很广的小技巧：跑一个空命令，Windows 会顺手把
    控制台模式里的 ENABLE_VIRTUAL_TERMINAL_PROCESSING 打开。
    下面再用 ctypes 正经设一遍，双保险。
    """
    if os.name != "nt":
        return True
    try:
        os.system("")
    except Exception:
        pass
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            k32.SetConsoleMode(handle, mode.value | 0x0004)
            return True
    except Exception:
        pass
    return False


def color_ok(stream):
    """该不该上色：不是 tty（管道、重定向）就别上，免得满屏乱码。"""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return stream.isatty()
    except Exception:
        return False


def terminal_width(default=80):
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def display_width(text):
    """
    终端显示宽度：东亚宽字符占两格，其余占一格。

    注意 █ 这类方块和制表符的 east_asian_width 是 'A'（歧义），按一格算 ——
    实测它们在终端里就是一格。用 ord(ch) > 127 判会把它们算成两格，条会被砍短。
    """
    total = 0
    for ch in text:
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def visible_width(text):
    """带颜色的字符串的显示宽度（先剥掉颜色码再量）。"""
    return display_width(strip_ansi(text))


# ---------------- 播放条 ----------------

def format_time(seconds):
    """秒 -> mm:ss；负数或 None 一律按 0 处理。"""
    if not seconds or seconds < 0:
        seconds = 0
    total = int(seconds)
    return "%d:%02d" % (total // 60, total % 60)


def render_bar(pos, duration, width=BAR_WIDTH, color=False, max_width=None,
               paused=False):
    """
    渲染一行播放进度：`0:37 [██████████          ] 4:22`

    duration 拿不到（直播 / 元数据未就绪）时退化成只显示当前位置，不画空条。
    max_width 给了就会自适应：塞不下就缩短条，最短 5 格。
    """
    pos = max(0.0, float(pos or 0))

    if not duration or duration <= 0:
        return "%s [%s] ??:??" % (format_time(pos), "?" * width)

    duration = float(duration)
    # 播到末尾时 mpv 偶尔会给出略超总时长的读数，显示上夹一下。
    # 注意：时间字符串必须夹完了再格式化，否则 200/100 会显示成 3:20/1:40。
    pos = min(pos, duration)
    ratio = min(1.0, pos / duration)
    t = format_time(pos)
    d = format_time(duration)

    def build(bw):
        filled = max(0, min(bw, int(round(bw * ratio))))
        plain_bar = "[" + BAR_FILLED * filled + BAR_EMPTY * (bw - filled) + "]"
        if not color:
            return (PAUSE_MARK + " " if paused else "") + "%s %s %s" % (t, plain_bar, d)
        bar_c = "[" + CYAN + BAR_FILLED * filled + RESET + BAR_EMPTY * (bw - filled) + "]"
        head = (YELLOW + PAUSE_MARK + " " + RESET) if paused else ""
        return head + CYAN + t + RESET + " " + bar_c + " " + DIM + d + RESET

    if max_width is None:
        return build(width)
    for bw in range(width, 4, -1):
        text = build(bw)
        if visible_width(text) <= max_width:
            return text
    return build(5)


def clear_line(width=None, text=None):
    """
    擦掉当前行内容并把光标挪回行首，之后打印别的文字就不会糊在一起。

    width 直接给列数；给了 text 就按它的显示宽度算（传带颜色的也行）。
    """
    if width is None:
        width = BAR_WIDTH + 24
    if text is not None:
        width = max(width, visible_width(text))
    sys.stdout.write("\r" + " " * width + "\r")
    sys.stdout.flush()


class ProgressBar:
    """
    后台每 REFRESH_INTERVAL 秒重画一行播放进度。

        bar = ProgressBar(player)
        bar.start()
        ...
        bar.stop()          # 必须调，否则线程会一直往 stdout 写

    没有歌在播（mpv 拿不到 time_pos）时自动让位：擦掉自己，不占着那一行。
    """

    def __init__(self, player, stream=None, interval=REFRESH_INTERVAL,
                 width=BAR_WIDTH, color=None, max_width=None):
        self.player = player
        self.stream = stream or sys.stdout
        self.interval = interval
        self.width = width
        self.max_width = max_width
        self.color = color_ok(self.stream) if color is None else bool(color)
        self._stop = threading.Event()
        self._thread = None
        self._last_len = 0

    # ---------- 生命周期 ----------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="progress-bar")
        self._thread.start()
        return self

    def stop(self, erase=True):
        """停掉线程。erase=True 时顺手把屏幕上那一行擦干净。"""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self.interval * 3 + 0.5)
        if erase:
            self.erase()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ---------- 绘制 ----------

    def frame(self):
        return render_bar(self.player.get_time_pos(), self.player.get_duration(),
                          self.width, self.color, self.max_width)

    def erase(self):
        """把最后一次画的内容擦掉，光标回到行首。"""
        width = max(self._last_len, self.width + 24)
        try:
            self.stream.write("\r" + " " * width + "\r")
            self.stream.flush()
        except Exception:
            pass
        self._last_len = 0

    def _draw(self, line):
        pad = max(0, self._last_len - visible_width(line))
        self._last_len = visible_width(line)
        try:
            self.stream.write("\r" + line + " " * pad)
            self.stream.flush()
        except Exception:
            # 管道被关掉之类的，别让线程炸在主循环里
            return False
        return True

    def _run(self):
        visible = False
        while not self._stop.is_set():
            pos = self.player.get_time_pos()
            duration = self.player.get_duration()
            if pos is None and duration is None:
                # 没在播：擦掉自己，把那一行还给别人
                if visible:
                    self.erase()
                    visible = False
            else:
                if not self._draw(self.frame()):
                    return
                visible = True
            if self._stop.wait(self.interval):
                break
        if visible:
            self.erase()
