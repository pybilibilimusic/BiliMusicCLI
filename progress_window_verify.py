"""
独立验证脚本：进度条独立终端窗口（progress_window.py）。

不碰 main_CUI.py、不需要 micro:bit、不需要网络（只用 127.0.0.1 的 UDP）。

    python progress_window_verify.py                    # 离线断言
    python progress_window_verify.py --demo             # 另开一个终端窗口看 8 秒动画
    python progress_window_verify.py --demo --no-window # 不弹窗，当前终端逐帧打印
"""

import argparse
import io
import socket
import sys
import threading
import time

import progress_bar
import progress_window as pw

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  [OK  ] %s%s" % (label, ("  " + str(extra)) if extra else ""))
    else:
        failed += 1
        print("  [FAIL] %s%s" % (label, ("  -> " + str(extra)) if extra else ""))


# ---------------- 1. 协议 ----------------

def test_protocol():
    print("\n[1] 消息协议（UDP 载荷）")
    msg = pw.make_progress(37, 242, paused=True, title="晴天")
    back = pw.decode_message(pw.encode_message(msg))
    check("往返后字段一致", back == msg, back)
    check("中文不转义成 \\u", b"\\u" not in pw.encode_message(
        pw.make_lyric("夜空中最亮的星")))
    check("坏包不抛异常", pw.decode_message(b"not json") == {"type": ""})
    check("非字典 JSON 不抛异常", pw.decode_message(b"[1,2]") == {"type": ""})
    check("bye 类型正确", pw.decode_message(
        pw.encode_message(pw.make_bye()))["type"] == pw.MSG_BYE)
    check("四种消息类型齐全", set(pw.MESSAGE_TYPES) ==
          {pw.MSG_PROGRESS, pw.MSG_DOWNLOAD, pw.MSG_LYRIC, pw.MSG_BYE})


# ---------------- 2. 三档策略（F5） ----------------

def test_policy():
    print("\n[2] 三档策略（命令关 / 语音开 / 连板子关）")
    check("命令模式默认关", not pw.ProgressPolicy(mode="command").enabled())
    check("语音模式自动开", pw.ProgressPolicy(mode="voice").enabled())
    check("连着 micro:bit 时关（语音模式也关）",
          not pw.ProgressPolicy(mode="voice", microbit_connected=True).enabled())
    check("force 能手动开命令模式",
          pw.ProgressPolicy(mode="command", force=True).enabled())
    check("但 force 也压不过 micro:bit",
          not pw.ProgressPolicy(mode="command", force=True,
                                microbit_connected=True).enabled())


# ---------------- 3. 渲染分派与 F 节约束 ----------------

def test_render():
    print("\n[3] 渲染分派（复用现有 render_*，不重写宽度逻辑）")
    w = 60
    line = pw.render_message(pw.make_progress(37, 242), w)
    check("播放进度有内容", bool(line), line)
    check("播放进度不超宽", progress_bar.visible_width(line) <= w,
          progress_bar.visible_width(line))
    check("进度里画了条", "[" in line and "]" in line, line)

    dl = pw.render_message(pw.make_download(2 * 1024 * 1024, 6 * 1024 * 1024,
                                            512 * 1024), w)
    check("下载进度有内容", bool(dl), dl)
    check("下载进度不超宽", progress_bar.visible_width(dl) <= w,
          progress_bar.visible_width(dl))

    check("未知类型渲染成空串", pw.render_message({"type": "nope"}, w) == "")
    check("空消息不炸", pw.render_message({}, w) == "")

    print("\n[4] F4 暂停标记必须是 ASCII '||'")
    plain = pw.render_message(pw.make_progress(37, 242, paused=True), w)
    check("不上色分支有 ||", "||" in plain, plain)
    check("没暂停时不出现 ||", "||" not in pw.render_message(
        pw.make_progress(37, 242, paused=False), w))
    colored = progress_bar.render_bar(37, 242, color=True, paused=True)
    check("彩色分支也有 ||（两条分支都要加）", "||" in colored, colored[:60])
    check("标记是纯 ASCII", all(ord(c) < 128 for c in progress_bar.PAUSE_MARK),
          progress_bar.PAUSE_MARK)

    print("\n[5] F3/F9 宽度与裁剪")
    check("clip_line 中文按两格算", pw.clip_line("一二三四五", 6) == "一二三",
          pw.clip_line("一二三四五", 6))
    check("clip_line 宽度为 0 返回空", pw.clip_line("abc", 0) == "")
    for width in (80, 50, 30, 20):
        text = pw.render_message(
            pw.make_download(3 * 1024 * 1024, 9 * 1024 * 1024, 1024 * 1024),
            width)
        check("下载条在 %d 列不折行" % width,
              progress_bar.visible_width(text) <= width,
              "%d 列" % progress_bar.visible_width(text))
    import inspect
    import progress_download
    src = inspect.getsource(progress_download.render_download)
    check("裁剪阶梯顺序是 条→剩余时间→大小→速度",
          "ladders" in src and src.index("show_eta=False") <
          src.index("show_size=False") < src.index("show_speed=False"),
          "复用现有实现，本模块未重写")


# ---------------- 4. paint 写传入 stream（B7） ----------------

def test_paint():
    print("\n[6] paint 写进传入的 stream（不硬编码 sys.stdout）")
    sink = io.StringIO()
    width = pw.paint(sink, "0:37 [████      ] 4:22", 60, 0)
    check("stream 里收到了内容", sink.getvalue() != "", repr(sink.getvalue()))
    check("用了 \\r 原地刷新", "\r" in sink.getvalue())
    width2 = pw.paint(sink, "short", 60, width)
    check("短内容会补空格擦掉旧尾巴", " " * (width - 5) in sink.getvalue(),
          "旧宽 %d -> 新宽 %d" % (width, width2))


# ---------------- 5. 歌词扩展位 ----------------

def test_lyric():
    print("\n[7] 歌词扩展位（架构能加，本轮不实现显示）")
    pane = pw.LyricPane(enabled=True)
    pane.update(pw.make_lyric("夜空中最亮的星", index=3))
    check("LyricPane 能存歌词", "夜空" in pane.line and pane.index == 3)
    check("LyricPane 能渲染", "[词]" in pane.render(80), pane.render(80))
    check("默认分派不画歌词", pw.render_message(
        pw.make_lyric("测试"), 80) == "")
    check("打开 include_lyric 就能画", "[词]" in pw.render_message(
        pw.make_lyric("测试"), 80, include_lyric=True))
    check("歌词消息能走协议", pw.decode_message(pw.encode_message(
        pw.make_lyric("测试")))["type"] == pw.MSG_LYRIC)


# ---------------- 6. 真实 UDP 往返 ----------------

def test_udp():
    print("\n[8] 真实 UDP 往返（127.0.0.1，无窗口）")
    port = pw._free_udp_port()
    sink = io.StringIO()
    ready = threading.Event()

    class Server(pw._WindowServer):
        def run(self, seconds=None):
            ready.set()
            super().run(seconds=seconds)

    server = Server(port, stream=sink)
    t = threading.Thread(target=lambda: server.run(seconds=2.0), daemon=True)
    t.start()
    ready.wait(1.0)
    time.sleep(0.05)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i in range(5):
        sock.sendto(pw.encode_message(
            pw.make_progress(i * 10, 100, title="测试")),
            (pw.DEFAULT_HOST, port))
        time.sleep(0.02)
    time.sleep(0.2)
    sock.sendto(pw.encode_message(pw.make_bye()), (pw.DEFAULT_HOST, port))
    t.join(timeout=2.0)
    sock.close()

    check("窗口进程收到了消息并渲染", "[" in sink.getvalue(),
          repr(sink.getvalue()[:50]))
    check("收到 bye 后干净退出", not t.is_alive())


# ---------------- 7. 客户端（不弹窗） ----------------

def test_client():
    print("\n[9] 主进程侧客户端")
    win = pw.ProgressWindow(pw.ProgressPolicy(mode="command"), spawn=False)
    check("策略关闭时 start() 返回 False", win.start() is False)
    win2 = pw.ProgressWindow(pw.ProgressPolicy(mode="voice"), spawn=False)
    check("语音模式 start() 成功", win2.start() is True)
    check("分配了 UDP 端口", isinstance(win2.port, int) and win2.port > 0,
          win2.port)
    check("能发播放进度", win2.progress(37, 242) is True)
    check("能发下载进度", win2.download(1024, 4096, 512) is True)
    check("能发歌词（扩展位）", win2.lyric("测试") is True)
    win2.stop()
    check("stop() 后 socket 已关", win2.sock is None)


# ---------------- demo ----------------

def demo(no_window=False):
    print("=" * 66)
    print("进度条独立窗口 demo")
    print("=" * 66)

    if no_window:
        print("（--no-window：不弹窗，把窗口进程收到的帧打印在下面）\n")
        sink = io.StringIO()
        server = pw._WindowServer(pw._free_udp_port(), stream=sink)
        # 直接驱动渲染，不走 UDP，便于观察每一帧
        server._draw()
        for i in range(0, 101, 20):
            line = pw.render_message(pw.make_progress(i, 100), 60)
            print("  播放  %s" % line)
        for i in range(0, 101, 25):
            line = pw.render_message(
                pw.make_download(int(6 * 1024 * 1024 * i / 100),
                                 6 * 1024 * 1024, 512 * 1024), 60)
            print("  下载  %s" % line)
        print("\n  暂停  %s" % pw.render_message(
            pw.make_progress(37, 100, paused=True), 60))
        print("  歌词  %s" % pw.render_message(
            pw.make_lyric("夜空中最亮的星"), 60, include_lyric=True))
        print("\n（歌词那行只在 include_lyric=True 时出现，窗口默认不画）")
        return 0

    print("正在另开一个终端窗口，跑 8 秒进度动画 ...\n")
    win = pw.ProgressWindow(pw.ProgressPolicy(mode="voice"))
    if not win.start():
        print("策略没放行，demo 没启动")
        return 1
    try:
        for i in range(0, 101, 2):
            win.progress(i, 100, paused=(30 <= i <= 34))
            time.sleep(0.04)
        for i in range(0, 101, 2):
            win.download(int(6 * 1024 * 1024 * i / 100),
                         6 * 1024 * 1024, 1.2 * 1024 * 1024)
            time.sleep(0.04)
        win.lyric("夜空中最亮的星")
        time.sleep(1.5)
    finally:
        win.stop()
    print("demo 结束，窗口应该已经自己关掉了。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="进度条独立窗口验证")
    ap.add_argument("--demo", action="store_true", help="看动画 demo")
    ap.add_argument("--no-window", action="store_true",
                    help="demo 不另开窗口，在当前终端逐帧打印")
    args = ap.parse_args()

    if args.demo:
        return demo(no_window=args.no_window)

    print("进度条独立窗口 progress_window.py —— 离线验证")
    test_protocol()
    test_policy()
    test_render()
    test_paint()
    test_lyric()
    test_udp()
    test_client()

    print("\n" + "=" * 46)
    print("通过 %d / %d" % (passed, passed + failed))
    if failed:
        print("失败 %d 项" % failed)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
