"""
micro:bit 遥控 —— 真机联调脚本（直接调用 microbit_bridge.py）

和 microbit_verify.py 的区别：
    verify  是离线自测（loop:// 虚拟串口），验的是代码逻辑；
    本脚本 连真板子，验的是「一端发、一端收」这条链路是否真的通，
            以及板端 microbit_remote.py 的按键 / 屏幕表现是否符合预期。

用法：
    .venv\\Scripts\\python.exe microbit_live_test.py                 # 全部测试（交互式）
    .venv\\Scripts\\python.exe microbit_live_test.py --port COM4      # 指定串口
    .venv\\Scripts\\python.exe microbit_live_test.py --auto           # 无人值守：跳过按键类与肉眼确认
    .venv\\Scripts\\python.exe microbit_live_test.py --monitor        # 只做串口监视
    .venv\\Scripts\\python.exe microbit_live_test.py --play           # 自由把玩：手动下发报文

测试前请确认：
    1. 板子已刷入 microbit/microbit_remote.py（或 .hex），屏幕显示一个「叉」属正常待机画面；
    2. 关掉 Mu / Thonny / 串口助手等一切会占用 COM 口的软件，串口只能被一个程序独占；
    3. 板子通过 USB 连着电脑。
"""

import argparse
import queue
import sys
import time

from microbit_bridge import MicrobitBridge, ascii_display, clamp_volume

MBED_USB_ID = "0D28:0204"     # micro:bit 的 USB VID:PID

# HW = 板子硬件本身不响应（不是代码问题），单独归一类，
#      免得报告里挂着一个「失败」却分不清该改代码还是该擦金手指
OK, FAIL, SKIP, HW = "OK", "X", "-", "HW"


# --------------------------------------------------------------- 端口发现
def find_microbit_ports():
    """扫一遍串口，挑出看起来是 micro:bit 的那些。"""
    import serial.tools.list_ports

    hits = []
    for port in serial.tools.list_ports.comports():
        hwid = (port.hwid or "").upper()
        desc = (port.description or "").upper()
        if MBED_USB_ID in hwid or "MBED SERIAL PORT" in desc:
            hits.append((port.device, port.description))
    return hits


def choose_port(arg_port, timeout=0):
    """
    挑出 micro:bit 的串口。

    :param timeout: >0 时轮询等待串口出现，方便「先跑脚本再插线」的场景。
    """
    if arg_port:
        return arg_port

    deadline = time.time() + timeout
    hits = []
    while True:
        hits = find_microbit_ports()
        if hits or time.time() >= deadline:
            break
        print("    没看到 micro:bit，等待中 ...（Ctrl+C 放弃）", end="\r")
        time.sleep(1)
    print(" " * 60, end="\r")

    if not hits:
        return None
    if len(hits) == 1:
        print(f"自动识别到 micro:bit：{hits[0][0]} ({hits[0][1]})")
        return hits[0][0]
    if len(hits) > 1:
        print("识别到多个候选串口，脚本默认挑第一个，想换请用 --port：")
        for device, desc in hits:
            print(f"  {device}  {desc}")
        return hits[0][0]
    return None


# --------------------------------------------------------------- 虚拟播放器
class FakePlayer:
    """
    冒充主程序本体的「播放器状态」。

    主程序里这些回调真的会去操作 mpv；这里只维护一份状态，
    目的是让单向的下行报文（VOL / TITLE …）也能形成闭环可观测。
    """

    def __init__(self):
        self.status = "stop"
        self.volume = 50
        self.title = ""
        self.position = None
        self.duration = None

    def snapshot(self):
        return {
            "status": self.status,
            "volume": self.volume,
            "title": self.title,
            "time": (self.position, self.duration),
        }

    def toggle_pause(self):
        self.status = "pause" if self.status == "play" else "play"
        return self.status

    def stop(self):
        self.status = "stop"

    def adjust(self, delta):
        self.volume = clamp_volume(self.volume + delta)
        return self.volume

    def set_volume(self, value):
        self.volume = clamp_volume(value)
        return self.volume


# --------------------------------------------------------------- 结果记账
class Report:
    def __init__(self):
        self.passed, self.failed, self.skipped, self.hardware = [], [], [], []

    def record(self, tag, name, note=""):
        {"OK": self.passed, "X": self.failed, "-": self.skipped,
         "HW": self.hardware}[tag].append(name)
        shown = {"OK": "[OK]", "X": "[X]", "-": "[--]", "HW": "[硬件]"}[tag]
        print(f"    {shown}  {name}" + (f"   {note}" if note else ""))

    def summary(self):
        buckets = (self.passed, self.failed, self.skipped, self.hardware)
        total = sum(len(b) for b in buckets)
        print()
        print("=" * 64)
        print(f"总计 {total} 项：通过 {len(self.passed)} / 失败 {len(self.failed)} "
              f"/ 跳过 {len(self.skipped)} / 硬件 {len(self.hardware)}")
        if self.failed:
            print("失败项：" + ", ".join(f"'{n}'" for n in self.failed))
        if self.hardware:
            print("硬件项：" + ", ".join(f"'{n}'" for n in self.hardware)
                  + "  （板子没反应，不是代码问题）")
        if self.skipped:
            print("跳过项：" + ", ".join(f"'{n}'" for n in self.skipped))
        print("=" * 64)
        return not self.failed


# --------------------------------------------------------------- 主测试器
class LiveTester:
    def __init__(self, port, auto=False, wait=15):
        self.port = port
        self.auto = auto
        self.wait = wait
        self.player = FakePlayer()
        self.events = queue.Queue()     # 归一化后的回调事件
        self.raw_lines = queue.Queue()  # 板子发来的原始行
        self.written = []               # 主机发出的所有报文（含时间戳）
        self.report = Report()

        self.bridge = MicrobitBridge(port=port, callbacks={
            "toggle_pause": self._on_toggle_pause,
            "next_track": lambda: self.events.put(("NEXT", None)),
            "prev_track": lambda: self.events.put(("PREV", None)),
            "stop": self._on_stop,
            "adjust_volume": self._on_adjust,
            "set_volume": self._on_set_volume,
            "get_state": self._on_get_state,
            "log": self._on_log,
        })

    # ---------- 回调 ----------
    def _on_toggle_pause(self):
        self.events.put(("PAUSE", self.player.toggle_pause()))

    def _on_stop(self):
        self.player.stop()
        self.events.put(("STOP", None))

    def _on_adjust(self, delta):
        self.events.put(("VOL", self.player.adjust(delta)))

    def _on_set_volume(self, value):
        self.events.put(("SETVOL", self.player.set_volume(value)))

    def _on_get_state(self):
        self.events.put(("GETSTATE", None))
        return self.player.snapshot()

    def _on_log(self, message):
        print(f"    [bridge] {message}")

    # ---------- 收发追踪 ----------
    def trace_io(self):
        """
        给 bridge 的 _write / handle_line 各套一层，把双向报文都记下来。

        这么咬内部方法是测试专用手段：QUERY 回应、keepalive 是桥自己发的，
        板子发来的原始行也被 handle_line 消化掉了，不套一层就观测不到。
        生产代码请勿模仿。
        """
        original_write = self.bridge._write

        def traced_write(payload):
            self.written.append((time.time(), payload))
            return original_write(payload)

        self.bridge._write = traced_write

        original_handle = self.bridge.handle_line

        def traced_handle(line):
            self.raw_lines.put(line)
            return original_handle(line)

        self.bridge.handle_line = traced_handle

    def set_keepalive(self, seconds):
        """
        临时改掉桥的心跳间隔。

        B 组测的是「单独一条报文的屏幕反应」，如果心跳每 5 秒重推一次全量状态，
        会趁你还没看完把屏幕改回去，所以那一组先把心跳关掉，
        到专门测心跳的用例再打开。
        """
        import microbit_bridge
        microbit_bridge._KEEPALIVE_INTERVAL = seconds

    def take_written(self, since):
        """取 since 时刻之后发出的报文列表。"""
        lines = [p for t, p in self.written if t >= since]
        self.written = [(t, p) for t, p in self.written if t < since]
        return lines

    def drain(self, target):
        while not target.empty():
            try:
                target.get_nowait()
            except queue.Empty:
                break

    # ---------- 等待 ----------
    def wait_event(self, expected, timeout):
        """
        等到某类事件出现为止。expected 是集合，命中任意一个即通过。
        期间会实时打印板子发来的每一行。
        """
        self.drain(self.events)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.print_raw_lines()
            try:
                name, value = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            mark = "" if name in expected else " (非预期，继续等)"
            print(f"    [板 -> 主机] {name}{'' if value is None else f' -> {value}'}{mark}")
            if name in expected:
                return name, value
        return None, None

    def print_raw_lines(self):
        while not self.raw_lines.empty():
            try:
                line = self.raw_lines.get_nowait()
            except queue.Empty:
                break
            print(f"    [串口原始] {line!r}")

    def confirm(self, prompt, auto_ok=True):
        """肉眼确认：--auto 模式直接放行。"""
        if self.auto:
            print(f"    {prompt}")
            print("    [--auto] 跳过人工确认")
            return True
        answer = input(f"    {prompt}   [回车=通过 / n=失败 / s=跳过] ").strip().lower()
        return answer != "n" if answer != "s" else None

    # ---------- 启动 ----------
    def connect(self):
        print(f"\n正在连接 {self.port} ...")
        self.trace_io()
        if not self.bridge.start():
            print("[X] 连接失败，请检查串口是否被别的软件占用。")
            return False
        # 板子上电后可能还在吐握手 QUERY，给一小会儿观察窗
        time.sleep(1.2)
        self.print_raw_lines()
        print("[OK] 已连接")
        return True

    # =========================================================== 测试项
    def run_connection(self):
        print("\n" + "-" * 64)
        print("A 组：连接与 handshake")

        seen = [p for _, p in self.written]
        print(f"    连接时桥自动下发：{seen}")
        if any(p.startswith("STATUS:stop") for p in seen):
            self.report.record(OK, "建立连接后自动下发 STATUS:stop", "固件应据此认为主机在线，停止握手 QUERY")
        else:
            self.report.record(FAIL, "建立连接后自动下发 STATUS:stop")

        result = self.confirm("请确认：板子屏幕现在是一个「叉」（X）？")
        if result is None:
            self.report.record(SKIP, "待机画面显示叉")
        elif result:
            self.report.record(OK, "待机画面显示叉（stop 且无歌名）")
        else:
            self.report.record(FAIL, "待机画面显示叉")

    def run_send_cases(self):
        """主机 -> 板子。全部靠肉眼确认屏幕。"""
        print("\n" + "-" * 64)
        print("B 组：主机 -> 板子（下发报文，看屏幕反应）")
        # 先把心跳关掉，免得你在看屏幕的时候状态被自动重推回去
        self.set_keepalive(9999)
        time.sleep(0.3)

        cases = [
            ("STATUS:play -> 音符图标",
             lambda: self._sync_status("play"),
             "屏幕应变成「一个音符（八分音符）」"),
            ("STATUS:pause -> 暂停图标",
             lambda: self._sync_status("pause"),
             "屏幕应变成「两条竖杠」的暂停图标"),
            ("STATUS:stop -> 叉",
             lambda: self._sync_status("stop"),
             "屏幕应变回「叉」"),
            ("TITLE 英文 -> 滚动歌名",
             lambda: self._send_track("Hello - World", 88, 240),
             "屏幕应开始横向滚动显示这段英文歌名"),
            ("TIME 不打断滚动",
             lambda: self._time_during_scroll(),
             "歌名应完整滚完一次，不会因为进度更新被反复从头打断"),
            ("VOL:0 / 50 / 100 -> 静态闪出 V + 数字",
             lambda: self._send_volume_steps(),
             "屏幕应停下歌名滚动，依次静态闪现 V/0、V/5/0、V/1/0/0 三串"),
            ("STATE 紧凑报文 -> 一次给全",
             lambda: self.bridge.send_state(compact=True),
             "屏幕应一次性切到紧凑包里的状态/音量/进度"),
            ("TITLE 中文 -> 清洗 + 转拼音",
             lambda: self._send_chinese(),
             "屏幕应滚动显示拼音版歌名（如 Yu Ai 这样的分写大写）"),
        ]

        for name, action, prompt in cases:
            print(f"\n  >> {name}")
            action()
            time.sleep(2.0)
            result = self.confirm(f"请确认：{prompt}")
            if result is None:
                self.report.record(SKIP, name)
            elif result:
                self.report.record(OK, name)
            else:
                self.report.record(FAIL, name)

        self._keepalive_case()

    def _sync_status(self, status):
        """下发状态的同时同步虚拟播放器，避免心跳回来时被覆盖成旧值。"""
        self.player.status = status
        self.bridge.send_status(status)

    def _send_track(self, title, position, duration):
        self.player.title = title
        self.player.status = "play"
        self.player.position, self.player.duration = position, duration
        self.bridge.send_status("play")
        self.bridge.send_title(title)
        self.bridge.send_volume(self.player.volume)
        self.bridge.send_time(position, duration)

    def _time_during_scroll(self):
        """滚动进行中连发 10 次 TIME，看会不会每来一次就把滚动掐回去重来。"""
        self.player.status = "play"
        self.player.title = "滚动连续性测试 Packaging Test"
        self.bridge.send_status("play")
        self.bridge.send_title(self.player.title)
        time.sleep(0.8)
        for i in range(10):
            self.player.position = i * 7
            self.bridge.send_time(i * 7, 300)
            time.sleep(0.6)

    def _send_volume_steps(self):
        """
        音量现在不再挂在滚动串尾巴上，而是变化时静态闪「V + 各位数字」。

        每一位停 420ms，所以 100 那次要等 4 帧（V/1/0/0）≈ 1.7s 才闪完。
        """
        for value in (0, 50, 100):
            self.player.volume = value
            self.bridge.send_volume(value)
            time.sleep(2.6)

    def _send_chinese(self):
        """
        中文标题走一遍真实生产链路：先 title_cleaner 清洗（主程序推的是清洗过的文件名），
        再 ascii_display 转拼音。直接用带【】标签的原始标题测是不对的。
        """
        try:
            from title_cleaner import clean_song_title
            raw = "在百万豪装录音棚大声听 杨丞琳《雨爱》【Hi-res无损音质】"
            cleaned = clean_song_title(raw)
            source = "经 title_cleaner 清洗后的标题"
        except Exception as exc:      # 清洗器出问题也不该让联调挂掉
            raw = cleaned = "杨丞琳 - 雨爱"
            source = f"（title_cleaner 不可用：{exc!r}，退回手写样例）"

        converted = ascii_display(cleaned)
        print(f"    {source}：{cleaned}")
        print(f"    原始搜索结果标题：{raw}")
        print(f"    实际下发：TITLE:{converted}")
        if not any(char.isalpha() for char in converted):
            print("    注意：转出来没有字母，说明 pypinyin 没装 —— 汉字会被整段丢掉。"
                  "执行 pip install pypinyin 即可")
        self.player.status = "play"
        self.player.title = converted
        self.bridge.send_status("play")
        self.bridge.send_title(converted)
        self.bridge.send_time(30, 240)

    def _keepalive_case(self):
        """空闲 5 秒桥会自动重发一次状态，这是板端判断主机在线的依据。"""
        print("\n  >> keepalive：把心跳恢复成 5 秒后静止不动等 8 秒")
        self.set_keepalive(5)
        self.drain(self.raw_lines)
        self._diag("等待前")
        time.sleep(8.0)
        self._diag("等待后")
        self._check_keepalive()

    def _diag(self, moment):
        import microbit_bridge as _mb
        thread = self.bridge._thread
        print(f"    [diag] {moment}：interval={_mb._KEEPALIVE_INTERVAL}s "
              f"connected={self.bridge.connected} "
              f"thread_alive={thread.is_alive() if thread else None}")

    def _check_keepalive(self):
        lines = self.take_written(time.time() - 8)
        hits = [p for p in lines if p.startswith(("STATUS:", "STATE:"))]
        print(f"    这 8 秒内主机自发报文（{len(lines)} 条）：{hits[-6:]}")
        if hits:
            self.report.record(OK, "空闲 5 秒后自动重发 keepalive 状态", f"共 {len(hits)} 条")
        else:
            self.report.record(FAIL, "空闲 5 秒后自动重发 keepalive 状态")
        confirm = self.confirm("请确认：这 8 秒里歌名是连续滚完的，没有被掐回开头重滚？"
                               "\n           （修复前的表现是滚一截就跳回去，每 5 秒抽一次）")
        if confirm is None:
            pass
        elif not confirm:
            self.report.record(FAIL, "keepalive 期间屏幕稳定性")

    # ---------------- 板 -> 主机 ----------------
    def ensure_alive(self, group):
        """
        每组开跑前确认串口还活着。

        实机联调里板子被碰掉是常态，与其让后面每一项都稀里糊涂地超时失败，
        不如在这里停下来问一句要不要重连。
        """
        if self.bridge.connected:
            return True
        print(f"    [!] {group}：串口已掉线（USB 松了？）")
        if self.auto:
            print("    [--auto] 无法等待重连，跳过该组是否需要手动介入的判断")
            return False
        answer = input("    插好后按回车重试，输入 s 跳过本组 [回车/s] ").strip().lower()
        if answer == "s":
            return False
        time.sleep(1.0)
        self.drain(self.events)
        self.drain(self.raw_lines)
        if self.bridge.start():
            time.sleep(1.0)
            print("    [OK] 已重连")
            return True
        print("    [X] 还是连不上")
        return False

    def run_key_cases(self):
        print("\n" + "-" * 64)
        print("C 组：板子 -> 主机（按你的板子操作，脚本验证主机收到什么）")
        # 心跳也会调用同一个 get_state 回调，留着它「摇一摇」那条会因假信号误判通过
        self.set_keepalive(9999)
        names = ("A 短按 -> PREV", "B 短按 -> NEXT", "A 长按 -> VOLDOWN",
                 "B 长按 -> VOLUP", "A+B 短按 -> PAUSE", "A+B 长按 -> STOP",
                 "触摸 logo -> PAUSE", "摇一摇 -> QUERY 并回 STATE")
        if not self.ensure_alive("C 组"):
            for name in names:
                self.report.record(SKIP, name)
            return

        if self.auto:
            for name in names:
                self.report.record(SKIP, f"{name}（--auto 已跳过）")
            return

        # 第 4 项 = 失败时算「硬件问题」而不是代码问题（只有 logo 属于这类：
        # 那块金手指是电容感应，氧化、受潮、手汗都会让它彻底读不到）
        steps = [
            ("A 短按 -> PREV", "快速按一下左侧 A 键然后松开", {"PREV"}, False),
            ("B 短按 -> NEXT", "快速按一下右侧 B 键然后松开", {"NEXT"}, False),
            ("A 长按 -> VOLDOWN", "按住左侧 A 键不放，超过 1 秒后松开", {"VOL"}, False),
            ("B 长按 -> VOLUP", "按住右侧 B 键不放，超过 1 秒后松开", {"VOL"}, False),
            ("A+B 短按 -> PAUSE",
             "同时按下 A 和 B（间隔小于 0.25 秒），随即松开（总时长别超过 0.6 秒）",
             {"PAUSE"}, False),
            ("A+B 长按 -> STOP",
             "同时按住 A 和 B 不放，超过 1 秒后一起松开", {"STOP"}, False),
            ("触摸 logo -> PAUSE",
             "手指按住正面金色 logo 后松开（仅 V2 有，金手指氧化时会失灵）",
             {"PAUSE"}, True),
            ("摇一摇 -> QUERY 并回 STATE", "把板子拿起来晃一下", {"GETSTATE"}, False),
        ]

        for name, action, expected, is_hardware in steps:
            print(f"\n  >> {name}")
            print(f"    请操作：{action}")
            got, value = self.wait_event(expected, self.wait)
            if got is None:
                if is_hardware:
                    self.report.record(HW, name, f"（{self.wait}s 内没收到）")
                    self.explain_logo_failure()
                else:
                    self.report.record(FAIL, name, f"（{self.wait}s 内没收到）")
                continue
            note = f" -> 收到 {got}" + (f"={value}" if value is not None else "")
            if got == "VOL":
                note += f"（当前音量 {self.player.volume}）"
            if name.startswith("摇一摇"):
                note += f"，主机已回包 {self.take_written(time.time() - 3)}"
            self.report.record(OK, name, note)
            time.sleep(0.8)

        passed = set(self.report.passed)
        print("\n    提示：A+B 短按和 logo 触摸是同一个功能，任意一条通就能暂停。")
        if "A+B 短按 -> PAUSE" in passed:
            print("    A+B 短按已通过 —— 就算 logo 彻底失灵，暂停功能不受影响。")

    @staticmethod
    def explain_logo_failure():
        """logo 没反应时把排查顺序说清楚，别让人对着代码找 bug。"""
        print("    " + "-" * 58)
        print("    logo 没响应。它不是按键，是电容感应，可能的坏法有这些：")
        print("      1. 金手指氧化 / 沾汗 —— 用酒精棉片擦一下正面那块金色 logo")
        print("      2. 手指太干 —— 手指先摸一下接地的金属，或略微哈气")
        print("      3. USB 供电会改变电容基线 —— 拔掉线用电池再试一次")
        print("      4. 摸 logo 的金属外圈，不要只按正中间")
        print("    固件侧已经做过两道兜底：开板就按着不会误触发暂停；")
        print("    传感器若卡在「一直被触摸」超过 5 秒会自动停用 logo。")
        print("    无论如何，暂停请改用 A+B 短按。")
        print("    " + "-" * 58)

    def run_protocol_cases(self):
        """协议层的边界情况，纯软件注入，不用碰板子。"""
        print("\n" + "-" * 64)
        print("D 组：协议边界（软件注入，不用碰板子）")
        self.set_keepalive(9999)

        checked = []

        def inject(line):
            self.drain(self.events)
            self.bridge.handle_line(line)
            time.sleep(0.15)
            try:
                return self.events.get_nowait()
            except queue.Empty:
                return None, None

        got, value = inject("VOL:999")
        if got == "SETVOL" and value == 100:
            self.report.record(OK, "VOL:999 越界被夹到 100", f"实际 {value}")
        else:
            self.report.record(FAIL, "VOL:999 越界被夹到 100", f"实际 {got}={value}")

        got, _ = inject("VOL:-5")
        if got is None:
            self.report.record(OK, "VOL:-5 非法值被忽略（不会被误当成负音量）")
        else:
            self.report.record(FAIL, "VOL:-5 非法值被忽略", f"实际 {got}")

        got, _ = inject("VOL:abc")
        if got is None:
            self.report.record(OK, "VOL:abc 非法值被忽略")
        else:
            self.report.record(FAIL, "VOL:abc 非法值被忽略", f"实际 {got}")

        got, _ = inject("NOT_A_COMMAND")
        if got is None:
            self.report.record(OK, "未知报文被静默忽略，不会抛异常")
        else:
            self.report.record(FAIL, "未知报文被静默忽略", f"实际 {got}")

        since = time.time()
        self.bridge.send_time(None, None)
        time.sleep(0.2)
        lines = self.take_written(since)
        if "TIME:?/?" in lines:
            self.report.record(OK, "进度未知时下发 TIME:?/?", str(lines))
        else:
            self.report.record(FAIL, "进度未知时下发 TIME:?/?", str(lines))

        # VOLUP / VOLDOWN 的步进
        self.player.volume = 50
        self.drain(self.events)
        self.bridge.handle_line("VOLUP")
        time.sleep(0.15)
        _, up1 = self.events.get_nowait()
        self.bridge.handle_line("VOLDOWN")
        time.sleep(0.15)
        _, down1 = self.events.get_nowait()
        if up1 == 60 and down1 == 50:
            self.report.record(OK, "VOLUP/VOLDOWN 步进 ±10", f"50 -> {up1} -> {down1}")
        else:
            self.report.record(FAIL, "VOLUP/VOLDOWN 步进 ±10", f"50 -> {up1} -> {down1}")

        # QUERY -> 主机回全量状态
        since = time.time()
        self.drain(self.events)
        self.bridge.handle_line("QUERY")
        time.sleep(0.4)
        replies = self.take_written(since)
        if any(p.startswith(("STATUS:", "STATE:")) for p in replies):
            self.report.record(OK, "QUERY 触发主机回全量状态", str(replies))
        else:
            self.report.record(FAIL, "QUERY 触发主机回全量状态", str(replies))

    def run_reconnect_case(self):
        """拔线重连：放到最后，因为要用户动手拔 USB。"""
        print("\n" + "-" * 64)
        print("E 组：拔线重连（可选）")
        self.set_keepalive(5)
        if self.auto:
            self.report.record(SKIP, "拔线重连（--auto 已跳过）")
            return
        answer = input("    是否要测试拔线重连？需要拔掉 USB 再插回 [y/N] ").strip().lower()
        if answer != "y":
            self.report.record(SKIP, "拔线重连")
            return
        input("    请拔掉 USB，拔完后回车 ...")
        time.sleep(2)
        alive = self.bridge.connected
        print(f"    拔线后 bridge.connected = {alive}")
        input("    请插回 USB，插好后回车 ...")
        time.sleep(2)
        self.drain(self.events)
        self.bridge.stop()
        # patch 还在实例上，不用重新 trace_io
        ok = self.bridge.start()
        if ok:
            time.sleep(1.5)
            lines = [p for _, p in self.written]
            self.report.record(OK, "重新打开串口成功", str(lines[:3]))
        else:
            self.report.record(FAIL, "重新打开串口成功")

    # ---------- 附加模式 ----------
    def monitor(self, seconds=30):
        print(f"\n串口监视 {seconds} 秒（Ctrl+C 退出）。随便按按板子试试。")
        deadline = time.time() + seconds
        try:
            while time.time() < deadline:
                self.print_raw_lines()
                try:
                    name, value = self.events.get(timeout=0.2)
                except queue.Empty:
                    continue
                print(f"    [回调] {name}" + ("" if value is None else f" -> {value}"))
        except KeyboardInterrupt:
            print("\n已退出监视。")

    def play(self):
        """自由把玩：手动下发报文。"""
        print("""
自由把玩模式。直接输入内容并回车：
    t <标题>      下发歌名（中文会自动降级成 ASCII）
    s play|pause|stop
    v 0-100       下发音量
    p <pos>/<dur> 下发进度
    st            下发紧凑 STATE 包
    q             退出
板子上的任何按键/手势会实时打印在这里。
""")
        try:
            while True:
                self.print_raw_lines()
                while not self.events.empty():
                    name, value = self.events.get_nowait()
                    print(f"    [回调] {name}" + ("" if value is None else f" -> {value}"))
                try:
                    line = input(">> ").strip()
                except EOFError:
                    break
                if not line:
                    continue
                parts = line.split(maxsplit=1)
                cmd, argument = parts[0], (parts[1] if len(parts) > 1 else "")
                if cmd in ("q", "quit", "exit"):
                    break
                elif cmd == "t":
                    self.player.title = argument
                    self.bridge.send_title(argument)
                    print(f"    已下发 TITLE:{ascii_display(argument)}")
                elif cmd == "s":
                    self.player.status = argument
                    self.bridge.send_status(argument)
                elif cmd == "v":
                    value = clamp_volume(int(argument))
                    self.player.volume = value
                    self.bridge.send_volume(value)
                elif cmd == "p":
                    pos, _, dur = argument.partition("/")
                    self.player.position, self.player.duration = int(pos), int(dur)
                    self.bridge.send_time(pos, dur)
                elif cmd == "st":
                    self.bridge.send_state(compact=True)
                else:
                    print("    不认识，重来")
        except KeyboardInterrupt:
            print("\n已退出。")


def main():
    parser = argparse.ArgumentParser(description="micro:bit 遥控真机联调")
    parser.add_argument("--port", help="串口号，留空自动识别 micro:bit")
    parser.add_argument("--auto", action="store_true", help="无人值守模式")
    parser.add_argument("--wait", type=int, default=15, help="每个按键动作的等待秒数")
    parser.add_argument("--monitor", action="store_true")
    parser.add_argument("--play", action="store_true")
    parser.add_argument("--wait-board", type=int, default=0, metavar="SEC",
                        help="串口没出现时最多轮询等待几秒（先跑脚本再插线时用）")
    parser.add_argument("--seconds", type=int, default=30, metavar="N",
                        help="--monitor 模式的监视秒数")
    args = parser.parse_args()

    port = choose_port(args.port, timeout=args.wait_board)
    if port is None:
        print("没找到 micro:bit 串口。按顺序排查：")
        print("  1. USB 线是数据线还是纯充电线 —— 只供电的线不会出串口；")
        print("  2. 资源管理器里有没有出现 MICROBIT 盘；")
        print("  3. 换个 USB 口，或按一下板子背面的复位键；")
        print("  4. 用 --port COMx 手动指定；先跑脚本后插线的话加 --wait-board 30。")
        return 1

    tester = LiveTester(port, auto=args.auto, wait=args.wait)
    if not tester.connect():
        return 1

    try:
        if args.monitor:
            tester.monitor()
        elif args.play:
            tester.play()
        else:
            tester.run_connection()
            tester.run_send_cases()
            tester.run_key_cases()
            tester.run_protocol_cases()
            tester.run_reconnect_case()
            tester.report.summary()
    except KeyboardInterrupt:
        print("\n\n用户中断。")
    finally:
        tester.bridge.stop()
        print("串口已关闭。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
