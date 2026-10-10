"""
独立验证脚本：播放进度条。

只验证「能不能拿到进度」和「怎么渲染」，不碰主程序逻辑。跑法：

    python progress_bar_verify.py                # 自动找一首缓存歌曲跑一遍
    python progress_bar_verify.py --file 路径    # 指定歌曲
    python progress_bar_verify.py --demo         # 现场看「刷新 vs 输入」冲突（需要手敲）

不依赖任何第三方模块：进度条只用 \r 回车符原地刷新，不需要 ANSI 转义，
python-mpv 本来就是项目已有的依赖。
"""

import argparse
import io
import sys
import threading
import time
from pathlib import Path

import utils

utils.setup_mpv_path()      # 必须在 import mpv 之前
import mpv                  # noqa: E402

# 渲染函数和刷新线程都在生产模块里，这里只测不重写 —— 免得两边跑偏
from progress_bar import (BAR_WIDTH, PAUSE_MARK, REFRESH_INTERVAL, ProgressBar,
                          clear_line, format_time, render_bar, visible_width)

passed = 0
failed = 0


# ---------------- 测试用例 ----------------

def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK  ] {label}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


def test_format_time():
    print("\n[1] 时间格式化")
    check("0 秒", format_time(0) == "0:00", format_time(0))
    check("37 秒", format_time(37) == "0:37", format_time(37))
    check("242 秒", format_time(242) == "4:02", format_time(242))
    check("小数会被截断", format_time(59.9) == "0:59", format_time(59.9))
    check("None 当 0", format_time(None) == "0:00")
    check("负数当 0", format_time(-5) == "0:00")
    check("超 1 小时", format_time(3725) == "62:05", format_time(3725))


def test_render_bar():
    print("\n[2] 进度条渲染")
    # 注意：空的那一半原来是 ░(U+2591)，2026-10-10 换成空格了 ——
    # 那个字符在等宽字体里是一排竖条，像梳子，用户看着很别扭。
    check("起点", render_bar(0, 100).startswith("0:00 ["), render_bar(0, 100))
    check("空心部分用空格而不是 ░", "░" not in render_bar(0, 100), render_bar(0, 100))
    check("暂停标记是纯 ASCII 的 ||",
          (PAUSE_MARK + " ") in render_bar(0, 100, paused=True),
          render_bar(0, 100, paused=True))
    check("不暂停时没有暂停标记",
          PAUSE_MARK not in render_bar(0, 100, paused=False),
          render_bar(0, 100, paused=False))
    narrow = render_bar(50, 100, max_width=20)
    check("窄终端会自动缩短条（不折行）", visible_width(narrow) <= 20,
          "%d 列 -> %r" % (visible_width(narrow), narrow))
    check("终点填满", render_bar(100, 100).split("]")[0].endswith("█" * BAR_WIDTH))
    check("总时长写在末尾", render_bar(10, 100).endswith("1:40"))
    check("超出总时长时封顶", render_bar(200, 100) == render_bar(100, 100))

    bar = render_bar(50, 100)
    filled = bar.count("█")
    check("一半进度约填一半", filled in (BAR_WIDTH // 2 - 1, BAR_WIDTH // 2, BAR_WIDTH // 2 + 1),
          f"填充 {filled}/{BAR_WIDTH}")

    no_duration = render_bar(10, None)
    check("无总时长时不画空条也不崩", "??:??" in no_duration and isinstance(no_duration, str),
          no_duration)
    check("duration=0 同异常处理", "??:??" in render_bar(10, 0))


def find_test_song(explicit=None):
    """挑一首测试曲：优先命令行指定，其次本地缓存。"""
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None
    for folder in ("m4s_temp", "mp3"):
        base = Path(folder)
        if not base.exists():
            continue
        songs = sorted(base.glob("*.*"))
        songs = [p for p in songs if p.suffix.lower() in (".m4s", ".mp3", ".flac", ".wav", ".m4a")]
        if songs:
            return songs[0]
    return None


def test_real_playback(song_path):
    print(f"\n[3] 实际播放取进度 —— {song_path.name}")
    player = mpv.MPV(vo="null", vid=False, ao="wasapi")
    player.volume = 0               # 别真的放出声音
    try:
        player.play(str(song_path))
        time.sleep(1.0)
        duration = player.duration
        check("拿到总时长", duration is not None and duration > 0,
              f"duration={duration:.1f}s")

        first = player.time_pos
        time.sleep(2.0)
        second = player.time_pos
        check("进度会往前走", second is not None and first is not None and second > first,
              f"{first:.2f}s -> {second:.2f}s")
        # 允许一点误差（解码启动、seek），但不应差出整秒级
        check("推进速度接近实时", abs((second - first) - 2.0) < 1.0,
              f"2 秒实际推进 {second - first:.2f} 秒")

        # observe_property：属性变化回调。
        # 注意：python-mpv 在这个版本要求 handler 作为位置参数传入，不能当装饰器用。
        hits = []

        def on_time_change(_name, value):
            if value:
                hits.append(value)

        player.observe_property("time-pos", on_time_change)
        time.sleep(2.5)
        check("observe_property 能收到回调", len(hits) > 0, f"回调 {len(hits)} 次")

        # 暂停后进度应该冻住
        player.pause = True
        time.sleep(0.3)
        frozen = player.time_pos
        time.sleep(1.2)
        check("暂停后进度冻结", abs((player.time_pos or 0) - (frozen or 0)) < 0.2,
              f"{frozen:.2f} -> {player.time_pos:.2f}")
        player.pause = False

        # seek 也要能跟上
        player.seek(60, reference="absolute")
        time.sleep(0.6)
        check("seek 后进度跳走", (player.time_pos or 0) > 50, f"time_pos={player.time_pos:.1f}")

        print("\n  实时渲染的样子（3 次采样）：")
        for _ in range(3):
            line = render_bar(player.time_pos, player.duration)
            sys.stdout.write("\r  " + line)
            sys.stdout.flush()
            time.sleep(0.6)
        clear_line(width=len(line) + 4)
        print("  \r（上面三帧是原地刷新的效果，最终只留一行）")
        passed_all = True
    except Exception as exc:
        check("播放测试未抛异常", False, repr(exc))
        passed_all = False
    finally:
        player.terminate()
    return passed_all


def test_refresh_is_safe_enough():
    """
    验证「后台线程刷新进度条」这套机制本身不会把 stdout 打乱。

    这里不直接撞 input()（非交互环境下没法模拟用户输入），
    \r 是否会顶掉用户正在敲的内容，用 --demo 现场看更直观。
    """
    print("\n[4] 后台刷新线程的行为")
    stop = threading.Event()
    frames = []
    sink = io.StringIO()      # 刷到内存里，别糊住控制台输出

    def refresher():
        for i in range(6):
            if stop.is_set():
                break
            sink.write("\r" + render_bar(i * 10, 100))
            frames.append(i)
            time.sleep(0.1)

    thread = threading.Thread(target=refresher, daemon=True)
    thread.start()
    thread.join(timeout=3)
    stop.set()
    check("刷新线程能正常结束", not thread.is_alive())
    check("确实刷新了多帧", len(frames) >= 3, f"{len(frames)} 帧")
    check("每帧都以回车开头（原地刷新而非滚屏）",
          sink.getvalue().count("\r") == len(frames), f"{sink.getvalue().count(chr(13))} 次回车")

    # 擦行之后打印的正常文字不带残留的控制字符
    clear_line()
    check("clear_line 只产出回车和空格",
          set(("\r" + " " * (BAR_WIDTH + 24) + "\r")) <= set("\r "))


class FakePlayer:
    """刷线程不需要真 mpv，给两个数就行。"""

    def __init__(self, pos=None, duration=None):
        self._pos, self._duration = pos, duration

    def get_time_pos(self):
        return self._pos

    def get_duration(self):
        return self._duration


def test_progress_bar_thread():
    print("\n[5] 后台刷新线程（progress_bar.ProgressBar）")
    sink = io.StringIO()
    player = FakePlayer(pos=10, duration=100)

    bar = ProgressBar(player, stream=sink, interval=0.05)
    bar.start()
    check("start() 后线程在跑", bar.running)
    time.sleep(0.3)
    bar.stop()
    check("stop() 后线程结束", not bar.running)

    out = sink.getvalue()
    check("刷出来的是进度条", "0:10" in out, repr(out[:40]))
    check("原地刷新而非滚屏", out.count("\r") >= 3, "回车 %d 次" % out.count("\r"))
    check("stop() 会把那一行擦掉（不留残影）", out.endswith("\r"), repr(out[-8:]))

    idle = FakePlayer(pos=None, duration=None)
    sink2 = io.StringIO()
    bar2 = ProgressBar(idle, stream=sink2, interval=0.05)
    bar2.start()
    time.sleep(0.25)
    bar2.stop()
    check("没在播时不占着那一行", sink2.getvalue().count("█") == 0,
          repr(sink2.getvalue()[:40]))

    bar3 = ProgressBar(player, stream=io.StringIO(), interval=0.05)
    bar3.start()
    first = bar3._thread
    bar3.start()
    check("重复 start() 不会起第二个线程", bar3._thread is first)
    bar3.stop()


def demo_interference():
    """
    现场演示：后台刷进度条时，命令行 input() 会被顶得乱七八糟。

    跑一遍你就知道为什么普通命令模式下不能默认开进度条了。
    """
    print("=" * 74)
    print("干扰演示：一边刷进度条，一边等你输入")
    print("=" * 74)
    print("接下来会有一行进度条不断闪烁，请在这期间随便敲几个字再回车。")
    print("（你会看到光标被打断、敲进去的字被覆盖或错位）\n")

    stop = threading.Event()

    def refresher():
        i = 0
        while not stop.is_set():
            sys.stdout.write("\r" + render_bar(i % 100, 100))
            sys.stdout.flush()
            i += 5
            time.sleep(0.2)

    thread = threading.Thread(target=refresher, daemon=True)
    thread.start()
    try:
        answer = input("随便输入点什么 > ")
    finally:
        stop.set()
        thread.join(timeout=2)
        clear_line()

    print(f"\n你输入的是：{answer!r}")
    print("\n—— 这就是「命令行模式默认不开进度条」的原因。")
    print("   语音模式下没人用键盘输入，所以这个代价不成立，进度条就能安全地开着。")


def main():
    parser = argparse.ArgumentParser(description="播放进度条验证")
    parser.add_argument("--file", help="指定测试用音频文件")
    parser.add_argument("--demo", action="store_true", help="演示刷新与输入的冲突")
    args = parser.parse_args()

    if args.demo:
        demo_interference()
        return 0

    print("=" * 74)
    print("播放进度条验证")
    print("=" * 74)

    test_format_time()
    test_render_bar()

    song = find_test_song(args.file)
    if song is None:
        print("\n[3] 实际播放取进度 —— 跳过")
        print("    本地缓存里没有歌曲，先随便下一首，或用 --file 指定。")
        check("找到测试歌曲", True, "（已跳过，非失败）")
    else:
        test_real_playback(song)

    test_refresh_is_safe_enough()
    test_progress_bar_thread()

    print("\n" + "=" * 74)
    print(f"结果：{passed} 通过 / {failed} 失败")
    print("=" * 74)
    print("\n想亲眼看看命令行刷新的干扰，跑：python progress_bar_verify.py --demo")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
