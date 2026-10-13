"""microbit_bridge_ble 的验证。

离线部分不需要板子：用一个假的 _write 抓下要发的报文，再直接喂指令测分发。
    .\\.venv\\Scripts\\python.exe microbit\\microbit_bridge_ble_verify.py

在线部分要板子（--live）：真连一次，等 12 秒看有没有收到指令。
    .\\.venv\\Scripts\\python.exe microbit\\microbit_bridge_ble_verify.py --live
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from microbit_bridge_ble import MicrobitBleBridge  # noqa: E402

results = []


def check(name, ok, extra=""):
    results.append((name, ok))
    print("  [%s] %s%s" % ("OK" if ok else "!!", name,
                           ("  -> %s" % (extra,)) if extra else ""))
    return ok


class Fake(MicrobitBleBridge):
    """把 _write 换成抓报文，不碰 BLE。"""

    def __init__(self):
        super().__init__(callbacks={"log": lambda m: None})
        self.sent = []
        self.fired = []
        self._linked = True          # 让 _write 认为已连上

    def _write(self, payload):
        self.sent.append(payload)
        return True

    def _call(self, name, *args):
        self.fired.append((name, args))
        return None


def t_offline():
    print("[1] 离线：报文格式")
    b = Fake()
    b.send_status("play")
    check("STATUS 报文", b.sent[-1] == "STATUS:play", b.sent[-1])

    b.send_volume(65)
    check("VOL 报文", b.sent[-1] == "VOL:65", b.sent[-1])

    b.send_title("晴天")
    check("TITLE 走 ASCII（板子点阵显示不了中文）",
          b.sent[-1].startswith("TITLE:") and all(ord(c) < 128
                                                  for c in b.sent[-1]),
          b.sent[-1])

    b.send_time(37, 242)
    check("TIME 报文", b.sent[-1] == "TIME:37/242", b.sent[-1])

    b.send_time(None, 242)
    check("缺一半时长显示 ?", b.sent[-1] == "TIME:?/242", b.sent[-1])

    b.send_volume(150)
    check("音量夹到 100", b.sent[-1] == "VOL:100", b.sent[-1])

    b.send_volume(-5)
    check("音量夹到 0", b.sent[-1] == "VOL:0", b.sent[-1])

    print("\n[2] 离线：指令分发")
    for cmd, expect in (("PREV", "prev_track"), ("NEXT", "next_track"),
                        ("PAUSE", "toggle_pause"), ("STOP", "stop"),
                        ("VOLUP", "adjust_volume"), ("VOLDOWN", "adjust_volume")):
        b = Fake()
        b.handle_line(cmd)
        ok = bool(b.fired) and b.fired[0][0] == expect
        check("%s -> %s" % (cmd, expect), ok, str(b.fired))

    b = Fake()
    b.handle_line("VOL:80")
    check("VOL:80 -> set_volume(80)",
          bool(b.fired) and b.fired[0] == ("set_volume", (80,)), str(b.fired))

    b = Fake()
    b.handle_line("BLAH:whatever")
    check("未知报文不炸也不乱调", b.fired == [], str(b.fired))

    print("\n[3] 离线：收包拼行（BLE 一次通知可能不是一整行）")
    b = Fake()
    b._on_notify(None, b"PREV\n")           # 一整行
    check("单行通知", b.fired and b.fired[0][0] == "prev_track", str(b.fired))
    b2 = Fake()
    b2._on_notify(None, b"NEX")             # 被切成两截
    b2._on_notify(None, b"T\n")
    check("分片能拼回来", b2.fired and b2.fired[0][0] == "next_track",
          str(b2.fired))


class FakeBleClient:
    """假的 bleak client：不碰蓝牙，能手动把链路「掐断」。"""

    def __init__(self, fail_writes=False):
        self.is_connected = True
        self.fail_writes = fail_writes
        self.written = []
        self.notify_char = None

    async def start_notify(self, char, callback):
        self.notify_char = char
        self.callback = callback
        return None

    async def write_gatt_char(self, char, data, response=False):
        if self.fail_writes:
            raise RuntimeError("模拟：链路断了，写不进去")
        self.written.append(bytes(data))


def _client_class(holder, fail_writes=False):
    class _Cls:
        def __init__(self, dev):
            self.c = FakeBleClient(fail_writes=fail_writes)
            holder["c"] = self.c

        async def __aenter__(self):
            return self.c

        async def __aexit__(self, *exc):
            self.c.is_connected = False
            return False
    return _Cls


def t_disconnect():
    """
    断线检测 —— 「谎报军情」的修复就靠这组。

    板子关机 / 拔电池 / 走远时**不会给主机发断开通知**，Windows 也要等
    蓝牙链路超时才把 is_connected 翻成 False。以前内层循环压根不问，
    _linked 就一直挂着 True，心跳于是永远说「已连板子」。
    """
    print("\n[4] 离线：断线检测（模拟板子被关掉）")
    import asyncio

    async def scenario_link_lost():
        b = MicrobitBleBridge(callbacks={"log": lambda m: None})
        holder = {}
        b._running = True                        # 让它真的进内层循环
        task = asyncio.create_task(
            b._connect_and_serve(_client_class(holder), None))
        await asyncio.sleep(0.35)
        before = b.connected
        holder["c"].is_connected = False         # 模拟板子关机
        raised = None
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except Exception as exc:
            raised = exc
        b._running = False
        return before, raised, b.connected, b._outbox.qsize()

    before, raised, after, pending = asyncio.run(scenario_link_lost())
    check("连着的时候 connected 为 True", before is True, str(before))
    check("链路断了会抛异常（外层收到才去重连）", raised is not None,
          repr(raised))
    check("断线后 connected 变 False —— 不再谎报军情", after is False,
          str(after))
    check("失联期间攒的报文会被清掉", pending == 0, str(pending))

    # 另一种断法：链路还「连着」但就是写不进去（板子卡住了）
    async def scenario_write_fails():
        b = MicrobitBleBridge(callbacks={"log": lambda m: None})
        b._running = True
        task = asyncio.create_task(
            b._connect_and_serve(_client_class({}, fail_writes=True), None))
        await asyncio.sleep(0.35)
        b._linked = True
        for i in range(8):
            b._write("TEST:%d" % i)
        raised = None
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except Exception as exc:
            raised = exc
        b._running = False
        return raised, b.connected

    raised, after = asyncio.run(scenario_write_fails())
    check("连着但写不进去 → 也会抛异常", raised is not None, repr(raised))
    check("写不进去之后 connected 变 False", after is False, str(after))


def t_live(seconds=12.0):
    print("\n[4] 在线：真连一次（%.0f 秒，期间按板子上的键）" % seconds)
    got = []

    def on_log(msg):
        print("       log: %s" % msg)

    b = MicrobitBleBridge(callbacks={
        "toggle_pause": lambda: got.append("PAUSE"),
        "next_track": lambda: got.append("NEXT"),
        "prev_track": lambda: got.append("PREV"),
        "stop": lambda: got.append("STOP"),
        "adjust_volume": lambda d: got.append("VOL%+d" % d),
        "set_volume": lambda v: got.append("VOL=%d" % v),
        "get_state": lambda: {"status": "play", "volume": 65,
                              "title": "Ting Feng - Test Song",
                              "time": (37, 242)},
        "log": on_log,
    })
    ok = b.start()
    check("连上板子", ok)
    if not ok:
        return
    b.send_state(compact=True)
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.3)
        if got:
            print("       收到：%s" % got)
            break
    b.stop()
    check("收到按键指令", bool(got), str(got))


def main():
    live = "--live" in sys.argv
    t_offline()
    t_disconnect()
    if live:
        t_live()
    print()
    passed = sum(1 for _, ok in results if ok)
    print("结果：%d/%d" % (passed, len(results)))
    if not live:
        print("（没跑在线部分，加 --live 连真板子）")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
