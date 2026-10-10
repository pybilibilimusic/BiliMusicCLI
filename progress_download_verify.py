"""
独立验证脚本：下载进度条。

只验证「怎么渲染」和「刷新线程的行为」，不碰网络、不需要真的下载文件。
跑法：

    python progress_download_verify.py

覆盖的点：
  - 单位换算和 ETA 的边界（None / 负数 / NaN / 超一小时）
  - 总大小未知时不画条（画一条不知道分母的进度条等于骗人）
  - 速度为 0 时 ETA 是 --:--，不是 calculating...
  - 下载量超过总大小时夹到 100%
  - **终端宽度自适应**：窄终端下整行必须塞得下，否则会折行，
    一折行 \\r 就回不到行首，每刷一次堆一行
  - 带颜色和不带颜色的显示宽度必须一致，不然擦行擦不干净
  - 后台刷新线程能正常起停、stop() 会把那一行擦掉
"""

import io
import sys
import threading
import time

from progress_download import (BAR_WIDTH, DownloadBar, format_eta,
                               format_size, render_download)
from progress_bar import visible_width, strip_ansi

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK  ] {label}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


KB = 1024.0
MB = KB * 1024.0
GB = MB * 1024.0


def test_format_size():
    print("\n[1] 体积格式化")
    check("B", format_size(512) == "512 B", format_size(512))
    check("KB", format_size(2048) == "2.0 KB", format_size(2048))
    check("MB", format_size(2 * MB) == "2.00 MB", format_size(2 * MB))
    check("GB", "GB" in format_size(3 * GB), format_size(3 * GB))
    check("负数不炸", format_size(-100) == "0 B", format_size(-100))
    check("None 不炸", format_size(None) == "0 B", format_size(None))


def test_format_eta():
    print("\n[2] 剩余时间格式化")
    check("None -> --:--", format_eta(None) == "--:--")
    check("负数 -> --:--", format_eta(-3) == "--:--")
    check("5 秒", format_eta(5) == "00:05", format_eta(5))
    check("65 秒进位", format_eta(65) == "01:05", format_eta(65))
    check("超一小时", format_eta(3725) == "1:02:05", format_eta(3725))
    check("NaN 不炸", format_eta(float("nan")) == "--:--")


def test_render():
    print("\n[3] 渲染")
    line = render_download(50, 100, 2 * MB)
    check("已知总大小要画条", line.startswith("["), line)
    check("进度 50%% 时条填一半", line.count("█") == 15, line)

    line = render_download(2048, None, 1 * MB)
    check("未知总大小不画条", "[" not in line, line)
    check("未知总大小仍报速度", "1.00 MB/s" in line, line)

    line = render_download(0, 100, 0)
    check("速度 0 时 ETA 是 --:--（不是 calculating）",
          "剩余 --:--" in line and "calculating" not in line, line)

    line = render_download(150, 100, 8 * MB)
    check("超量下载夹到 100.0%", "100.0%" in line, line)
    check("夹住之后条是满的", line.count("█") == BAR_WIDTH, line)

    # 注意参数顺序是 (done, total)：这里是「总大小为 0」，不是「已完成 0」
    line = render_download(50, 0)
    check("total 为 0 同异常处理（不画条）", "[" not in line, line)


def test_width_fit():
    print("\n[4] 终端宽度自适应（折行会让 \\r 失效，这个必须守住）")
    for w in (100, 60, 40, 20):
        line = render_download(12.5 * MB, 26.4 * MB, 5 * MB,
                               width=30, max_width=w)
        used = visible_width(line)
        check("%d 列塞得下" % w, used <= w,
              "%d 列 -> %r" % (used, strip_ansi(line)))

    # 带颜色时宽度必须和不带颜色时一样，否则擦行擦不干净
    colored = render_download(12.5 * MB, 26.4 * MB, 5 * MB, color=True)
    plain = render_download(12.5 * MB, 26.4 * MB, 5 * MB, color=False)
    check("带颜色和不带颜色宽度一致",
          visible_width(colored) == visible_width(plain),
          "%d vs %d" % (visible_width(colored), visible_width(plain)))
    check("剥掉颜色码后内容相同", strip_ansi(colored) == plain)


def test_thread():
    print("\n[5] 后台刷新线程")
    state = {"done": 0.0}
    lock = threading.Lock()
    started = time.time()

    def get_done():
        with lock:
            return state["done"]

    def get_total():
        return 10.0 * MB

    def get_speed():
        with lock:
            return state["done"] / max(0.001, time.time() - started)

    sink = io.StringIO()
    bar = DownloadBar(get_done, get_total, get_speed, stream=sink,
                      interval=0.05, color=False, max_width=60)
    bar.start()
    check("start() 后线程在跑", bar.running)
    for _ in range(20):
        with lock:
            state["done"] += 0.5 * MB
        time.sleep(0.02)
    bar.stop()
    check("stop() 后线程结束", not bar.running)

    out = sink.getvalue()
    check("刷出来的是进度条", "[" in out and "%" in out, repr(out[:50]))
    check("原地刷新而非滚屏", out.count("\r") >= 3, "回车 %d 次" % out.count("\r"))
    check("stop() 会把那一行擦掉（不留残影）", out.endswith("\r"), repr(out[-8:]))

    # 反复 start() 不应该起第二个线程
    bar2 = DownloadBar(get_done, get_total, get_speed, stream=io.StringIO(),
                       interval=0.05)
    bar2.start()
    first = bar2._thread
    bar2.start()
    check("重复 start() 不会起第二个线程", bar2._thread is first)
    bar2.stop()

    # finally 里没 stop 会一直刷，这里确认 stop 之后确实不再写
    sink3 = io.StringIO()
    bar3 = DownloadBar(get_done, get_total, get_speed, stream=sink3, interval=0.05)
    bar3.start()
    bar3.stop()
    frozen = sink3.getvalue()
    time.sleep(0.2)
    check("stop() 之后不再往终端写", sink3.getvalue() == frozen)


def main():
    print("=" * 74)
    print("下载进度条验证")
    print("=" * 74)
    test_format_size()
    test_format_eta()
    test_render()
    test_width_fit()
    test_thread()
    print("\n" + "=" * 74)
    print(f"结果：{passed} 通过 / {failed} 失败")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
