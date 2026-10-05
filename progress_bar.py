"""
播放进度条：纯渲染函数 + 后台刷新线程。

三个决定都不是拍脑袋定的，写在这里免得以后被改回去：

1. **只用 `\\r` 原地刷新，不用 ANSI 转义**。Windows 下 `chcp 65001` 之后 ANSI 不一定生效，
   而回车符是任何终端都认的。
2. **命令模式下默认关闭**。后台线程每 0.5 秒写一次 stdout，会把用户 `input()` 里
   正在敲的内容顶掉（`progress_bar_verify.py --demo` 可以现场看这个惨状）。
   语音模式没人敲键盘，这个代价不成立，所以在那里开。
3. **连着 micro:bit 时不刷命令行**。那种情况下进度应该走点阵屏（bridge 的 keepalive
   本来就带 TIME），两边同时刷只会互相干扰。
"""

import sys
import threading

BAR_WIDTH = 30
REFRESH_INTERVAL = 0.5      # 秒；再快就是白白刷屏


# ---------------- 纯函数（不依赖播放器，可直接断言） ----------------

def format_time(seconds):
    """秒 -> mm:ss；负数或 None 一律按 0 处理。"""
    if not seconds or seconds < 0:
        seconds = 0
    total = int(seconds)
    return "%d:%02d" % (total // 60, total % 60)


def render_bar(pos, duration, width=BAR_WIDTH):
    """
    渲染一行 `00:37 [██████░░░░░░] 04:22`。

    duration 拿不到（直播 / 元数据未就绪）时退化成只显示当前位置，不画空条。
    """
    pos = max(0.0, float(pos or 0))
    if not duration or duration <= 0:
        return "%s [%s] ??:??" % (format_time(pos), "?" * width)
    # 播到末尾时 mpv 偶尔会给出略超总时长的读数，显示上夹一下，免得出现 4:23/4:22
    pos = min(pos, duration)
    ratio = min(1.0, pos / duration)
    filled = int(width * ratio)
    return "%s [%s%s] %s" % (format_time(pos), "█" * filled,
                             "░" * (width - filled), format_time(duration))


def clear_line(width=BAR_WIDTH + 24):
    """擦掉当前行内容并把光标挪回行首，之后打印别的文字就不会糊在一起。"""
    sys.stdout.write("\r" + " " * width + "\r")
    sys.stdout.flush()


# ---------------- 后台刷新 ----------------

class ProgressBar:
    """
    后台每 REFRESH_INTERVAL 秒重画一行进度条。

        bar = ProgressBar(player)
        bar.start()
        ...
        bar.stop()          # 必须调，否则线程会一直往 stdout 写

    没有歌在播（mpv 拿不到 time_pos）时自动让位：擦掉自己，不占着那一行。
    """

    def __init__(self, player, stream=None, interval=REFRESH_INTERVAL, width=BAR_WIDTH):
        self.player = player
        self.stream = stream or sys.stdout
        self.interval = interval
        self.width = width
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
                          self.width)

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
        self._last_len = len(line)
        try:
            self.stream.write("\r" + line)
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
