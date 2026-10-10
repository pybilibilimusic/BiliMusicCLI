"""
下载进度条：纯渲染函数 + 后台刷新线程。

跟播放条共用 `progress_bar` 里的颜色 / 宽度处理（ANSI、终端宽度、显示宽度）。

三个决定写在这里免得以后被改回去：

1. **总大小拿不到时不画条**。服务器不给 Content-Length 就不知道分母，
   画一条进度条等于骗人，退化成只报「已下载 + 速度」。

2. **速度为 0 时 ETA 显示 `--:--`**。以前这里打印 `calculating...`
   （下载刚起步那一下速度就是算不出来），刷出来很难看。

3. **整行宽度必须塞得进终端**。这行字比控制台窗口宽就会折行，一折行
   `\\r` 就回不到行首，于是每刷一次堆一行 —— 这就是「进度条不整合在同一行」
   的真相。现在会量终端宽度，塞不下按优先级砍：
       进度条 -> 剩余时间 -> 已下载/总大小 -> 速度
"""

import threading
import sys

from progress_bar import (BAR_FILLED, BAR_EMPTY, RESET, BOLD, DIM, CYAN,   # noqa: F401
                          GREEN, color_ok, display_width, format_time,
                          strip_ansi, terminal_width, visible_width)

BAR_WIDTH = 30

KB = 1024.0
MB = KB * 1024.0
GB = MB * 1024.0


# ---------------- 纯函数：不依赖网络，可直接断言 ----------------

def format_size(num_bytes):
    """字节 -> 人类可读。None / NaN / 负数一律按 0 处理。"""
    try:
        value = float(num_bytes or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value != value or value < 0:
        value = 0.0
    if value >= GB:
        return "%.2f GB" % (value / GB)
    if value >= MB:
        return "%.2f MB" % (value / MB)
    if value >= KB:
        return "%.1f KB" % (value / KB)
    return "%d B" % int(value)


def format_eta(seconds):
    """剩余秒数 -> mm:ss（超过一小时给 h:mm:ss），拿不到给 --:--。"""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "--:--"
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return "--:--"
    value = int(value)
    if value >= 3600:
        return "%d:%02d:%02d" % (value // 3600, (value % 3600) // 60, value % 60)
    return "%02d:%02d" % (value // 60, value % 60)


def _bar(filled, width, color, done_color):
    f = BAR_FILLED * filled
    e = BAR_EMPTY * (width - filled)
    if not color:
        return "[" + f + e + "]"
    return "[" + done_color + f + RESET + e + "]"


def _render(done, total, speed, bar_width, color,
            show_size=True, show_speed=True, show_eta=True):
    """按给定的条宽渲染一行。宽度自适应是 render_download 的事。"""
    done = max(0.0, float(done or 0))
    speed = max(0.0, float(speed or 0))

    if not total or total <= 0:
        plain = "已下载 %s | %s/s" % (format_size(done), format_size(speed))
        return (DIM + plain + RESET) if color else plain

    total = float(total)
    done = min(done, total)
    ratio = done / total
    filled = max(0, min(bar_width, int(round(bar_width * ratio))))

    if done >= total:
        eta = "00:00"
    elif speed > 0.01:
        eta = format_eta((total - done) / speed)
    else:
        eta = "--:--"

    done_color = GREEN if done >= total else CYAN
    bar = _bar(filled, bar_width, color, done_color)
    pct = "%5.1f%%" % (ratio * 100)

    plain = bar + " " + pct
    colored = bar + " " + BOLD + pct + RESET
    if show_size:
        part = "  %s / %s" % (format_size(done), format_size(total))
        plain += part
        colored += part
    if show_speed:
        part = "  %s/s" % format_size(speed)
        plain += part
        colored += (CYAN + part + RESET) if color else part
    if show_eta:
        part = "  剩余 %s" % eta
        plain += part
        colored += (DIM + part + RESET) if color else part

    return colored if color else plain


def render_download(done, total=None, speed=0.0, width=BAR_WIDTH,
                    color=False, max_width=None):
    """
    渲染一行下载进度，自动适配终端宽度。

    塞不下时按这个顺序砍，能多留一个字段就多留一个：
      1. 先只缩短条
      2. 再砍「剩余时间」
      3. 再砍「已下载 / 总大小」
      4. 最后只剩条 + 百分比
    """
    if max_width is None:
        return _render(done, total, speed, width, color)

    ladders = [
        dict(),
        dict(show_eta=False),
        dict(show_eta=False, show_size=False),
        dict(show_eta=False, show_size=False, show_speed=False),
    ]
    for flags in ladders:
        for bw in range(width, 8, -1):
            text = _render(done, total, speed, bw, color, **flags)
            if visible_width(text) <= max_width:
                return text
    return _render(done, total, speed, 8, color,
                   show_eta=False, show_size=False, show_speed=False)


def clear_line(text, stream=None):
    """
    擦掉刚画的那一行。传带颜色的也行，会先剥掉再量宽度。

    stream 一定要传 —— 之前这里硬编码写 sys.stdout，结果测试用 StringIO
    当输出的时候，「擦行」全擦到真控制台上去了，sink 里一点没变。
    """
    out = stream if stream is not None else sys.stdout
    out.write("\r" + " " * (visible_width(text) + 2) + "\r")
    out.flush()


# ---------------- 后台刷新 ----------------

class DownloadBar:
    """
    后台定时重画一行下载进度。

        bar = DownloadBar(get_done, get_total, get_speed)
        bar.start()
        ...
        bar.stop()          # 必须调，否则线程会一直往 stdout 写

    注意 `stop()` 一定要放在 finally 里 —— 下载中途抛异常时如果线程没停，
    它会一直往终端写进度条，把后面的报错信息全冲掉。
    """

    def __init__(self, get_done, get_total=None, get_speed=None,
                 stream=None, interval=0.1, width=BAR_WIDTH,
                 color=None, max_width=None):
        self.get_done = get_done
        self.get_total = get_total
        self.get_speed = get_speed
        self.stream = stream or sys.stdout
        self.interval = interval
        self.width = width
        self.max_width = max_width
        self.color = color_ok(self.stream) if color is None else bool(color)
        self._stop = threading.Event()
        self._thread = None
        self._last = ""

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="download-bar")
        self._thread.start()
        return self

    def stop(self, erase=True):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self.interval * 5 + 0.5)
        if erase:
            self.erase()

    @property
    def running(self):
        """跟 progress_bar.ProgressBar 保持一致：这是属性不是方法。"""
        return self._thread is not None and self._thread.is_alive()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def frame(self):
        total = self.get_total() if self.get_total else None
        speed = self.get_speed() if self.get_speed else 0.0
        return render_download(self.get_done(), total, speed, self.width,
                               self.color, self.max_width)

    def erase(self):
        if self._last:
            clear_line(self._last, self.stream)
            self._last = ""

    def _draw(self, line):
        pad = visible_width(self._last) - visible_width(line)
        self.stream.write("\r" + line + (" " * pad if pad > 0 else ""))
        self.stream.flush()
        self._last = line

    def _run(self):
        while not self._stop.is_set():
            self._draw(self.frame())
            if self._stop.wait(self.interval):
                break
        self.erase()
