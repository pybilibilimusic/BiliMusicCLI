"""
独立验证脚本：micro:bit 遥控桥接。

不需要真的插上 micro:bit 就能跑 —— 用 pyserial 自带的 loop:// 虚拟串口，
或者一个假串口对象，把串口层也一并验证到。

    python microbit_verify.py

验证内容：
  1. 协议报文格式（状态 / 音量 / 歌名 / 进度 / 全量状态）
  2. 板端指令分发（PAUSE / NEXT / PREV / STOP / VOLUP / VOLDOWN / VOL:n / QUERY）
  3. 未实现的回调被优雅忽略，不会抛异常
  4. 回调抛异常时被兜住，监听线程不会挂
  5. 中文标题的 ASCII 化处理（有/无 pypinyin 两种路径）
  6. 真实串口读写（loop:// 回环）
"""

import queue
import sys
import time

from microbit_bridge import (
    MicrobitBridge,
    VOLUME_STEP,
    ascii_display,
    clamp_volume,
)

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


class FakeSerial:
    """假串口：写进 written 队列，读则从 feed 里取，能模拟真实读写。"""

    def __init__(self, *args, **kwargs):
        self.written = queue.Queue()
        self.feed = queue.Queue()
        self.closed = False
        self.timeout = 0.05

    def write(self, data):
        if self.closed:
            raise OSError("serial closed")
        self.written.put(data)
        return len(data)

    def readline(self):
        try:
            line = self.feed.get(timeout=self.timeout)
        except queue.Empty:
            return b""
        if isinstance(line, str):
            line = line.encode()
        return line

    def close(self):
        self.closed = True

    # 测试辅助
    def push_command(self, command):
        self.feed.put(command.encode() if isinstance(command, str) else command)

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.written.get_nowait().decode())
            except queue.Empty:
                break
        return out


def make_bridge(fake=None, callbacks=None):
    """建一个可观测的 bridge：所有动作记进 events。"""
    events = []
    state = {
        "status": "stop",
        "volume": 50,
        "title": "晴天 - 周杰伦",
        "time": (10, 240),
    }
    base = {
        "toggle_pause": lambda: events.append(("pause",)),
        "next_track": lambda: events.append(("next",)),
        "prev_track": lambda: events.append(("prev",)),
        "stop": lambda: events.append(("stop",)),
        "adjust_volume": lambda delta: events.append(("adj_vol", delta)),
        "set_volume": lambda value: events.append(("set_vol", value)),
        "get_state": lambda: dict(state),
        "log": lambda msg: events.append(("log", msg)),
    }
    if callbacks:
        base.update(callbacks)

    fake = fake if fake is not None else FakeSerial()
    bridge = MicrobitBridge(
        port="TEST0",
        callbacks=base,
        serial_factory=lambda: fake,
    )
    return bridge, fake, events, state


def test_protocol_output():
    print("\n[1] 协议报文格式")
    fake = FakeSerial()
    bridge = MicrobitBridge(port="TEST0", callbacks={}, serial_factory=lambda: fake)
    bridge._serial = fake

    bridge.send_status("play")
    bridge.send_volume(65)
    bridge.send_title("Qing Tian - Jay Chou")
    bridge.send_time(37, 242)
    lines = fake.drain()
    check("四条报文都写出来", len(lines) == 4, f"实际 {len(lines)} 行")
    check("STATUS 报文", lines[0] == "STATUS:play\n", repr(lines[0]))
    check("VOL 报文", lines[1] == "VOL:65\n", repr(lines[1]))
    check("TITLE 报文", lines[2] == "TITLE:Qing Tian - Jay Chou\n", repr(lines[2]))
    check("TIME 报文", lines[3] == "TIME:37/242\n", repr(lines[3]))

    fake_full = FakeSerial()
    b2 = MicrobitBridge(port="T", callbacks={"get_state": lambda: {
        "status": "pause", "volume": 30, "title": "A - B", "time": (5, 100)
    }}, serial_factory=lambda: fake_full)
    b2._serial = fake_full
    b2.send_state()
    lines = fake_full.drain()
    joined = "".join(lines)
    check("send_state 推出四行", len(lines) == 4, f"实际 {len(lines)} 行")
    check("STATE 含 STATUS/VOL/TITLE/TIME",
          all(key in joined for key in ("STATUS:", "VOL:", "TITLE:", "TIME:")),
          joined.replace("\n", " | "))

    # compact 模式：一行给全，省串口带宽（板端 parse_line 认这个格式）
    fake_c = FakeSerial()
    b2c = MicrobitBridge(port="T", callbacks={"get_state": lambda: {
        "status": "play", "volume": 65, "title": "A - B", "time": (37, 242)
    }}, serial_factory=lambda: fake_c)
    b2c._serial = fake_c
    b2c.send_state(compact=True)
    lines_c = fake_c.drain()
    check("compact 模式只发一行", len(lines_c) == 1, f"实际 {len(lines_c)} 行")
    check("compact 格式为 STATE:状态|音量|进度/总长",
          lines_c and lines_c[0] == "STATE:play|65|37/242\n",
          lines_c[0].strip() if lines_c else "(空)")
    fake_n = FakeSerial()      # 不提供 get_state，模拟「什么都还没有」
    bn = MicrobitBridge(port="T", callbacks={}, serial_factory=lambda: fake_n)
    bn._serial = fake_n
    bn.send_state(compact=True)
    check("拿不到进度时用 ? 占位",
          fake_n.drain()[0] == "STATE:stop|0|?/?\n", "(见上行)")

    # 音量越界应被夹住
    v = FakeSerial()
    b3 = MicrobitBridge(port="T", callbacks={}, serial_factory=lambda: v)
    b3._serial = v
    b3.send_volume(150)
    b3.send_volume(-20)
    outs = v.drain()
    check("音量上限夹到 100", outs[0] == "VOL:100\n", outs[0].strip())
    check("音量下限夹到 0", outs[1] == "VOL:0\n", outs[1].strip())

    # 时长未知时的占位
    v2 = FakeSerial()
    b4 = MicrobitBridge(port="T", callbacks={}, serial_factory=lambda: v2)
    b4._serial = v2
    b4.send_time(None, None)
    check("未知进度用 ? 占位", v2.drain()[0] == "TIME:?/?\n")


def test_command_dispatch():
    print("\n[2] 板端指令分发")
    cases = [
        ("PAUSE", ("pause",)),
        ("NEXT", ("next",)),
        ("PREV", ("prev",)),
        ("STOP", ("stop",)),
        ("pause", ("pause",)),          # 小写也要认
        (" Next ", ("next",)),          # 首尾空白要能容忍
    ]
    for command, expected in cases:
        bridge, fake, events, _ = make_bridge()
        bridge.handle_line(command)
        check(f"{command.strip():<8} -> {expected[0]}", expected in events, str(events))

    bridge, _, events, _ = make_bridge()
    bridge.handle_line("VOLUP")
    check("VOLUP -> +10", ("adj_vol", VOLUME_STEP) in events)
    bridge.handle_line("VOLDOWN")
    check("VOLDOWN -> -10", ("adj_vol", -VOLUME_STEP) in events)
    bridge.handle_line("VOL:77")
    check("VOL:77 -> 设为 77", ("set_vol", 77) in events)
    bridge.handle_line("VOL:999")
    check("VOL:999 -> 夹到 100", ("set_vol", 100) in events)
    bridge.handle_line("VOL:abc")
    check("VOL:abc 非法值被忽略", ("set_vol", 0) not in events)

    bridge, fake, events, state = make_bridge()
    bridge._serial = fake
    bridge.handle_line("QUERY")
    sent = "".join(fake.drain())
    check("QUERY 回发全量状态", "STATUS:stop" in sent and "VOL:50" in sent, sent.replace("\n", " | "))

    bridge, _, events, _ = make_bridge()
    before = len(events)
    bridge.handle_line("WHATEVER")
    bridge.handle_line("")
    check("未知报文被忽略不报错", len(events) == before)


def test_missing_and_failing_callbacks():
    print("\n[3] 回调缺失 / 抛异常的容错")
    fake = FakeSerial()
    # 一个回调都不给
    bridge = MicrobitBridge(port="T", callbacks={}, serial_factory=lambda: fake)
    bridge._serial = fake
    try:
        for command in ("PAUSE", "NEXT", "PREV", "STOP", "VOLUP", "QUERY"):
            bridge.handle_line(command)
        check("缺回调时不抛异常", True)
    except Exception as exc:
        check("缺回调时不抛异常", False, repr(exc))

    # 回调故意炸掉：异常应被兜住，且不影响后续指令
    seen = []

    def boom(*args):
        raise RuntimeError("boom")

    bridge, _, events, _ = make_bridge(callbacks={
        "toggle_pause": boom,
        "next_track": lambda: seen.append("next"),
    })
    bridge._serial = FakeSerial()
    bridge.handle_line("PAUSE")
    logs = [e for e in events if e[0] == "log"]
    check("回调异常被捕获并记日志", bool(logs), str(logs[:1]))
    bridge.handle_line("NEXT")
    check("异常后仍能处理下一条", "next" in seen, str(seen))


def test_ascii_display():
    print("\n[4] 歌名 ASCII 化")
    check("纯 ASCII 原样返回", ascii_display("Windy Hill") == "Windy Hill")
    check("换行被剔除", ascii_display("a\nb") == "a b")
    out = ascii_display("晴天 - 周杰伦")
    check("中文标题输出仍是合法串口文本", "\n" not in out and "\r" not in out, f"-> {out!r}")

    try:
        import pypinyin  # noqa: F401
        converted = ascii_display("晴天")
        check("pypinyin 可用时转拼音", any(ch.isalpha() for ch in converted), f"-> {converted!r}")
        # 单个方块字 $5$ 个点阵，连写成 qingtian 就没法读了，必须是分开的大写音节
        check("汉字之间用空格分隔且首字母大写", converted == "Qing Tian", f"-> {converted!r}")
        check("非 ASCII 的非汉字（假名）被剔除",
              ascii_display("デート约会").isascii(), f"-> {ascii_display('デート约会')!r}")
    except ImportError:
        stripped = ascii_display("晴天")
        check("降级路径：非 ASCII 被剔除且不崩", isinstance(stripped, str))
        print(f"  [    ] 未装 pypinyin，中文标题当前会退化成 {stripped!r}；")
        print("         想要板子上显示拼音，执行 pip install pypinyin 即可（纯可选依赖）")

    check("日语假名不残留换行/控制符",
          all(ord(ch) >= 32 for ch in ascii_display("デート")))


def test_clamp_volume():
    print("\n[5] 音量夹取")
    check("150 -> 100", clamp_volume(150) == 100)
    check("-5 -> 0", clamp_volume(-5) == 0)
    check("65 -> 65", clamp_volume(65) == 65)


def test_real_serial_loopback():
    """
    用 pyserial 自带的 loop:// 虚拟串口验证真实串口链路。

    注意这里**刻意不启动监听线程**：写进 loop:// 的数据会被回送到同一个读缓冲区，
    如果线程也在读，两边会抢数据，属于测试用例自身的竞争，不是代码问题。
    """
    print("\n[6] 真实串口读写（loop:// 回环）")
    try:
        import serial
        real = serial.serial_for_url("loop://", timeout=1)
    except Exception as exc:
        check("loop:// 可用", False, repr(exc))
        return

    # a) 裸链路健康度
    real.write(b"PING\n")
    check("串口本身可读写", real.readline().strip() == b"PING")

    # b) bridge 写出的报文确实落到了串口
    bridge = MicrobitBridge(
        port="loop://",
        callbacks={"get_state": lambda: {"status": "play", "volume": 42,
                                         "title": "Test Song", "time": (1, 2)}},
        serial_factory=lambda: real,
    )
    bridge._serial = real          # 不 start()，避免监听线程与回环读数互相抢占
    bridge.send_status("play")
    bridge.send_title("Test Song")
    bridge.send_volume(42)

    real.timeout = 0.3
    received = [real.readline().decode(errors="ignore").strip() for _ in range(4)]
    joined = [r for r in received if r]
    check("回环读到 STATUS", "STATUS:play" in joined, str(joined))
    check("回环读到 TITLE", "TITLE:Test Song" in joined, str(joined))
    check("回环读到 VOL", "VOL:42" in joined, str(joined))

    # c) 写失败要优雅降级：串口关掉后不应抛异常
    bridge.stop()
    check("串口关闭后 _write 返回 False", bridge._write("STATUS:play") is False)


def main():
    print("=" * 78)
    print("micro:bit 遥控桥接验证")
    print("=" * 78)
    test_protocol_output()
    test_command_dispatch()
    test_missing_and_failing_callbacks()
    test_ascii_display()
    test_clamp_volume()
    test_real_serial_loopback()

    print("\n" + "=" * 78)
    print(f"结果：{passed} 通过 / {failed} 失败")
    print("=" * 78)

    if failed == 0:
        print("\n提示：本脚本验证的是协议与串口链路。刷板之前可以先用")
        print("      `python microbit/check_remote.py` 离线跑一遍固件（不需要开发板）。")
        print("      真正只能上机确认的只剩下按键手感、点阵滚动速度、拔线重连这几项。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
