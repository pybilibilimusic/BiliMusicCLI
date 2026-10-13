"""
micro:bit 教室遥控 —— 与音乐遥控**共用同一份板端固件**，只是解释不同。

板子只发字符串，语义全在电脑这一侧解释，所以不用刷固件：
    A 短按    PREV     -> 上一页（PageUp）
    B 短按    NEXT     -> 下一页（PageDown）
    A 长按    VOLDOWN  -> 音量 -
    B 长按    VOLUP    -> 音量 +
    A+B 短按  PAUSE    -> Esc
    A+B 长按  STOP     -> Alt+Tab
    摇一摇    QUERY    -> 播放/暂停（第一个 QUERY 当握手丢掉）

用法：
    python pc_remote.py --dry-run     离线自检：不连板子、不按键，只看映射对不对
    python pc_remote.py --monitor     连板子、打印每条报文，但**不真按键盘**
    python pc_remote.py               正式跑（会真的按键！）

⚠️ 三个设计上的坑，都在这里处理掉了：
1. QUERY 有双重身份：既是开机握手（每秒一发，最多 15 次）也是摇一摇。
   判定办法是"跟上一发间隔够不够远" —— 握手是连发的，摇晃是偶尔一次。
   而且**无论判成什么都一定要回一次 STATE**，否则板子一直停在"等主机说话"。
2. Alt+Tab 必须"按住 Alt -> 敲 Tab -> 松开 Alt"，中途崩了 Alt 会卡住，
   之后所有按键都变成菜单快捷键。所以用了 try/finally 兜底 + 退出时统一释放。
3. Esc 在 PPT 全屏放映时是"结束放映"，不是"关弹窗"。已确认接受这个语义。
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "microbit"))

# --- 虚拟键码
VK_PRIOR = 0x21              # PageUp
VK_NEXT = 0x22               # PageDown
VK_ESCAPE = 0x1B
VK_TAB = 0x09
VK_MENU = 0x12               # Alt
VK_VOLUME_UP = 0xAF
VK_VOLUME_DOWN = 0xAE
VK_MEDIA_PLAY_PAUSE = 0xB3

KEY_NAMES = {
    VK_PRIOR: "PageUp", VK_NEXT: "PageDown", VK_ESCAPE: "Esc",
    VK_TAB: "Tab", VK_MENU: "Alt", VK_VOLUME_UP: "VolUp",
    VK_VOLUME_DOWN: "VolDown", VK_MEDIA_PLAY_PAUSE: "PlayPause",
}

DOWN, UP, TAP = "down", "up", "tap"      # TAP = 按下再松开

# 一次长按只发一条 VOLUP/VOLDOWN（板端不连发），Windows 的音量键一步约 2%，
# 所以一次按 5 下 ≈ 10%，不然从 30 调到 70 得按二十次。
VOLUME_TAPS = 5
VOLUME_STEP = 10             # 回显用的幅度，跟桥里的一致
QUERY_GAP = 2.5              # 秒。握手是每秒一发，摇一摇是偶尔一次，靠这个分开


def _tap(vk, times=1):
    return [(TAP, vk)] * times


def _alt_tab():
    """切上一个窗口。按住 Alt 敲一下 Tab 再松开 = 快速切换，不会把切换面板留在屏幕上。"""
    return [(DOWN, VK_MENU), (TAP, VK_TAB), (UP, VK_MENU)]


# 报文 -> (回显文本, 键序列)。回显会传到板子屏幕上滚一行（只支持 ASCII）。
ACTIONS = {
    "PREV": ("PAGE UP", lambda: _tap(VK_PRIOR)),
    "NEXT": ("PAGE DOWN", lambda: _tap(VK_NEXT)),
    "PAUSE": ("ESC", lambda: _tap(VK_ESCAPE)),
    "STOP": ("ALT TAB", _alt_tab),
    "QUERY": ("PLAY PAUSE", lambda: _tap(VK_MEDIA_PLAY_PAUSE)),
}


def check_actions():
    """静态自检：每个动作都得把自己按下的修饰键松开。"""
    problems = []
    for command, (echo, build) in ACTIONS.items():
        steps = build()
        held = []
        for kind, vk in steps:
            if kind == DOWN:
                held.append(vk)
            elif kind == UP:
                if vk in held:
                    held.remove(vk)
                else:
                    problems.append("%s 松开了没按下的键 %s" % (command, KEY_NAMES.get(vk, vk)))
        if held:
            problems.append("%s 结束时还按着 %s" % (command, KEY_NAMES.get(held[0], held[0])))
        if not echo.isascii():
            problems.append("%s 的回显不是 ASCII，板子画不出来" % command)
    return problems


# ---------- 按键发送 ----------

class KeySender:
    """真的往系统里送键。enabled=False 时只记录不发送（monitor 模式）。"""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.log = []

    def _event(self, vk, flags):
        self.log.append((KEY_NAMES.get(vk, hex(vk)), "up" if flags else "down"))
        if not self.enabled:
            return
        import ctypes
        ctypes.windll.user32.keybd_event(vk, 0, flags, 0)

    def run(self, steps):
        held = []
        try:
            for kind, vk in steps:
                if kind == DOWN:
                    held.append(vk)
                    self._event(vk, 0)
                elif kind == UP:
                    self._event(vk, 2)
                    if vk in held:
                        held.remove(vk)
                else:                       # TAP
                    self._event(vk, 0)
                    self._event(vk, 2)
        finally:
            # 中途出任何岔子都要把修饰键松开，否则 Alt 卡住，
            # 之后所有按键都变成菜单快捷键，看着像电脑中邪了
            for vk in reversed(held):
                self._event(vk, 2)

    def release_all(self):
        for vk in (VK_MENU, VK_TAB, VK_ESCAPE):
            self._event(vk, 2)


class FakeBridge:
    """离线自检用：只记录要发给板子的报文，不真发。"""

    def __init__(self):
        self.lines = []

    def send_title(self, title):
        self.lines.append("TITLE:%s" % title)

    def send_volume(self, volume):
        self.lines.append("VOL:%d" % volume)

    def send_status(self, status):
        self.lines.append("STATUS:%s" % status)

    def send_state(self, compact=False):
        self.lines.append("STATE:...")


def read_system_volume():
    """读到真实系统音量就读（需要 pycaw），读不到返回 None。"""
    try:
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        device = AudioUtilities.GetSpeakers()
        interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return int(round(interface.QueryInterface(
            IAudioEndpointVolume).GetMasterVolumeLevelScalar() * 100))
    except Exception:
        return None


# ---------- 遥控主体 ----------

class ClassroomRemote:
    """把板子报文翻译成键盘动作，并把结果回显到板子屏幕。"""

    def __init__(self, bridge, sender, monitor=False):
        self.bridge = bridge
        self.sender = sender
        self.monitor = monitor
        self._last_query_at = None      # None = 还没收到过 QUERY
        self._volume = read_system_volume()
        self._counter = 0

    # --- QUERY 判定：握手 vs 摇一摇 ---
    def _is_real_shake(self):
        now = time.time()
        if self._last_query_at is None:
            self._last_query_at = now
            return False                # 第一个一定是握手，丢掉
        gap = now - self._last_query_at
        self._last_query_at = now
        return gap >= QUERY_GAP         # 连发 = 还在握手；隔得远 = 真的摇了

    def on_query(self):
        """QUERY 只有「回状态」是必须的，动作要看判定结果。"""
        if not self._is_real_shake():
            return
        self.fire("QUERY")

    # --- 动作 ---
    def fire(self, command):
        entry = ACTIONS.get(command)
        if entry is None:
            return
        echo, build = entry
        self._counter += 1
        self.sender.run(build())
        # 带上序号：同样的文本板子不会重滚，没有序号连按两次就看不出反应
        self.bridge.send_title("%s %d" % (echo, self._counter))
        print("  %-6s -> %-10s %s" % (command, echo, self._describe()))

    def adjust_volume(self, delta):
        vk = VK_VOLUME_UP if delta > 0 else VK_VOLUME_DOWN
        self.sender.run(_tap(vk, VOLUME_TAPS))
        self._counter += 1
        print("  %-6s -> %s%+d" % ("VOLUP" if delta > 0 else "VOLDOWN",
                                   "音量", delta))
        if self._volume is None:
            # 读不到真实音量就只报方向，别编一个数字出来骗人
            self.bridge.send_title("VOL %s%d %d" % ("+" if delta > 0 else "-",
                                                    abs(delta), self._counter))
            return
        self._volume = max(0, min(100, self._volume + delta))
        self.bridge.send_volume(self._volume)
        print("          回显 VOL:%d" % self._volume)

    def _describe(self):
        return " ".join("%s(%s)" % (name, act) for name, act in self.sender.log[-4:])

    def get_state(self):
        """keepalive 用的状态。教室场景没有歌名，给个固定的就绪态就行。"""
        return {"status": "play", "volume": self._volume or 0, "title": "READY"}


class ClassroomBridge:   # 延迟导入，避免 dry-run 也要装 bleak
    pass


def build_bridge(remote_holder, scan_seconds=10.0):
    """连板子。QUERY 需要特殊处理（既要回状态又要判定动作），所以包一层。"""
    from microbit_bridge_ble import MicrobitBleBridge

    class _Bridge(MicrobitBleBridge):
        def handle_line(self, line):
            command, _, _arg = line.partition(":")
            command = command.strip().upper()
            if command == "QUERY":
                # 握手应答必须回，不然板子屏幕一直停在"等主机说话"
                self.send_state()
                remote = remote_holder.get("remote")
                if remote is not None:
                    remote.on_query()
                return
            super().handle_line(line)

    return _Bridge(callbacks={}, scan_seconds=scan_seconds)


def run_dry():
    """离线自检：不连板子、不按键，只验证映射和 QUERY 判定。"""
    print("=" * 66)
    print("离线自检（不连板子、不按任何键）")
    print("=" * 66)

    ok = True
    problems = check_actions()
    ok &= _check("每个动作都松开了自己按下的修饰键", not problems,
                 "; ".join(problems))

    sender = KeySender(enabled=False)
    remote = ClassroomRemote(bridge=FakeBridge(), sender=sender, monitor=True)

    cases = [
        ("PREV", "PageUp", "PAGE UP"),
        ("NEXT", "PageDown", "PAGE DOWN"),
        ("PAUSE", "Esc", "ESC"),
        ("STOP", "Alt", "ALT TAB"),
        ("QUERY", "PlayPause", "PLAY PAUSE"),
    ]
    print("\n--- 动作 ---")
    for command, expect_key, expect_echo in cases:
        sender.log = []
        remote.fire(command)
        used = [name for name, _act in sender.log]
        ok &= _check("%-6s 用到了 %s" % (command, expect_key), expect_key in used,
                     ",".join(used))
        ok &= _check("%-6s 回显 %r" % (command, expect_echo),
                     remote.bridge.lines[-1] == "TITLE:%s %d" % (expect_echo, remote._counter),
                     remote.bridge.lines[-1])

    # STOP 的序列必须是 按住 Alt -> Tab -> 松开 Alt，顺序错了就会把切换面板留在屏幕上
    stop_steps = ACTIONS["STOP"][1]()
    ok &= _check("STOP 首尾是 按下Alt / 松开Alt",
                 stop_steps[0] == (DOWN, VK_MENU) and stop_steps[-1] == (UP, VK_MENU),
                 str(stop_steps))

    print("\n--- 音量 ---")
    sender.log = []
    remote.adjust_volume(10)
    ok &= _check("VOLUP 连按 %d 次音量+" % VOLUME_TAPS,
                 sender.log.count(("VolUp", "down")) == VOLUME_TAPS,
                 str(sender.log.count(("VolUp", "down"))))
    sender.log = []
    remote.adjust_volume(-10)
    ok &= _check("VOLDOWN 连按 %d 次音量-" % VOLUME_TAPS,
                 sender.log.count(("VolDown", "down")) == VOLUME_TAPS,
                 str(sender.log.count(("VolDown", "down"))))

    print("\n--- QUERY 判定（握手 vs 摇一摇）---")
    probe = ClassroomRemote(bridge=None, sender=KeySender(enabled=False))
    first = probe._is_real_shake()
    ok &= _check("第一个 QUERY 被丢掉（当握手）", first is False, str(first))
    ok &= _check("1 秒后的连发仍算握手", probe._is_real_shake() is False, "")
    time.sleep(QUERY_GAP + 0.1)
    ok &= _check("隔 %.1f 秒的算真摇晃" % QUERY_GAP, probe._is_real_shake() is True, "")

    print("\n" + "=" * 66)
    print("结果：%s" % ("全过" if ok else "有失败项"))
    print("=" * 66)
    return 0 if ok else 1


def _check(name, passed, extra=""):
    tail = ("  -> %s" % extra) if (extra and not passed) else ""
    print("  [%s] %s%s" % ("OK" if passed else "FAIL", name, tail))
    return passed


def run_live(monitor):
    holder = {}
    sender = KeySender(enabled=not monitor)
    remote = ClassroomRemote(bridge=None, sender=sender, monitor=monitor)

    bridge = build_bridge(holder)
    remote.bridge = bridge
    bridge.callbacks.update({
        "prev_track": lambda: remote.fire("PREV"),
        "next_track": lambda: remote.fire("NEXT"),
        "toggle_pause": lambda: remote.fire("PAUSE"),       # A+B 短按 -> Esc
        "stop": lambda: remote.fire("STOP"),                # A+B 长按 -> Alt+Tab
        "adjust_volume": lambda delta: remote.adjust_volume(delta),
        "get_state": lambda: remote.get_state(),
        "log": lambda message: print("  [桥] %s" % message),
    })
    holder["remote"] = remote

    print("=" * 66)
    print("教室遥控 %s" % ("（monitor：只看不按键）" if monitor else "（会真按键盘！）"))
    print("=" * 66)
    if remote._volume is None:
        print("提示：读不到系统音量（没装 pycaw），音量回显只报方向、不报数字。")
    print("正在找板子 ...")

    if not bridge.start():
        print("没连上。按一下板子背面的 RESET 再试。")
        return 1

    print("连上了。板子屏幕应该出对勾。Ctrl+C 退出。\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n退出中 ...")
    finally:
        bridge.stop()
        sender.release_all()      # 保险：把可能还按着的 Alt 松开
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="micro:bit 教室遥控")
    parser.add_argument("--dry-run", action="store_true",
                        help="离线自检，不连板子不按键")
    parser.add_argument("--monitor", action="store_true",
                        help="连板子但只打印，不真的按键")
    args = parser.parse_args()

    if args.dry_run:
        sys.exit(run_dry())
    if sys.platform != "win32" and not args.monitor:
        print("按键模拟只在 Windows 上可用；其它系统请用 --dry-run 或 --monitor")
        sys.exit(1)
    sys.exit(run_live(monitor=args.monitor))
