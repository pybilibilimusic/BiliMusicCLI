"""
micro:bit MicroPython 的最小模拟层。

用途：在**没有开发板**的情况下，把 `microbit_remote.py` 这段固件真跑一遍，
验证 import 是否成立、按键状态机是否正确、串口报文解析是否对、屏幕有没有被刷爆。

它只模拟固件实际用到的东西，不是完整的 MicroPython 实现：

| 模拟对象 | 能力 |
| --- | --- |
| `microbit.display` | `show()` / `scroll()` / `clear()`，全部记录到 `history` 里供断言 |
| `microbit.button_a/b` | `is_pressed()`，由测试脚本按虚拟时间驱动 |
| `microbit.uart` | `init()` / `write()` / `readline()` / `any()`，读写都留痕 |
| `microbit.accelerometer` | `was_gesture()`，可排定摇一摇的时刻 |
| `microbit.pin_logo` | `is_touched()`，只在 v2 模式下提供 |
| `sleep` / `running_time` | 共用同一个虚拟时钟，`sleep` 推进时间 |

虚拟时间很重要：固件里的去抖窗口（600ms 长按、250ms 组合键）在真实时间下要等，
在虚拟时钟里一瞬间就能跑完，测试才能稳定、可重复。
"""

import time as _real_time


class LoopStopped(Exception):
    """达到上限 tick 数时抛出，用来把固件里的 `while True` 收住。"""


class Clock:
    def __init__(self):
        self.now = 0

    def advance(self, ms):
        self.now += ms


class Image:
    """只保留能被比较的能力：固件用到的内置图案都是独立单例。"""

    _cache = {}

    def __init__(self, spec=""):
        self.spec = spec
        self.name = None

    @classmethod
    def builtin(cls, name):
        if name not in cls._cache:
            img = cls("<builtin:%s>" % name)
            img.name = name
            cls._cache[name] = img
        return cls._cache[name]

    def __repr__(self):
        return self.name or self.spec

    def __eq__(self, other):
        return self is other or self.spec == getattr(other, "spec", object())

    def __hash__(self):
        return id(self)


# 固件会用到的内置图案
Image.HAPPY = Image.builtin("HAPPY")
Image.NO = Image.builtin("NO")
Image.MUSIC_QUAVER = Image.builtin("MUSIC_QUAVER")


class Display:
    def __init__(self):
        self.history = []      # ('show', Image) / ('scroll', text, loop) / ('clear',)

    def show(self, image, **kwargs):
        self.history.append(("show", image))

    def scroll(self, text, **kwargs):
        loop = kwargs.get("loop", False)
        self.history.append(("scroll", text, loop))

    def clear(self):
        self.history.append(("clear",))

    # --- 给测试用的小工具 ---
    @property
    def scrolled_texts(self):
        return [h[1] for h in self.history if h[0] == "scroll"]

    @property
    def shown_images(self):
        return [h[1] for h in self.history if h[0] == "show"]

    @property
    def scroll_count(self):
        return len([h for h in self.history if h[0] == "scroll"])


class Button:
    def __init__(self, name, controller):
        self.name = name
        self._controller = controller

    def is_pressed(self):
        return self._controller.is_pressed(self.name)

    def was_pressed(self):
        return False


class Pin:
    def __init__(self, controller, name):
        self._controller = controller
        self.name = name

    def is_touched(self):
        return self._controller.is_touched(self.name)


class Accelerometer:
    def __init__(self, controller):
        self._controller = controller

    def was_gesture(self, name):
        # was_gesture 是「读清标志」，读到一次就消费掉
        if name in self._controller.pending_gestures:
            self._controller.pending_gestures.remove(name)
            return True
        return False


class UART:
    """串口。写出去的行全部记录在 sent 里，读进来的行由测试脚本注入。"""

    def __init__(self, controller):
        self._controller = controller
        self.initialized = False
        self.baudrate = None
        self.sent = []                 # 已发出的原始行（不含 \n）
        self._incoming = bytearray()

    def init(self, baudrate=9600, **kwargs):
        self.initialized = True
        self.baudrate = baudrate

    def write(self, payload):
        text = payload.decode() if isinstance(payload, (bytes, bytearray)) else payload
        for line in text.split("\n"):
            if line.strip():
                self.sent.append(line.strip())
        return len(payload)

    def any(self):
        return b"\n" in self._incoming

    def readline(self):
        if b"\n" not in self._incoming:
            return None
        index = self._incoming.index(b"\n")
        chunk = bytes(self._incoming[:index])
        del self._incoming[:index + 1]
        return chunk

    def feed(self, text):
        """测试用：模拟主机发来一行。"""
        if not text.endswith("\n"):
            text += "\n"
        self._incoming += text.encode("utf-8")


class Music:
    def __init__(self):
        self.played = []

    def play(self, notes, **kwargs):
        self.played.append(notes)
        return None

    def stop(self):
        return None


class MicrobitSim:
    """
    一次模拟运行。

    用法：
        sim = MicrobitSim(v2=True, max_ticks=300)
        sim.inject_serial("STATUS:play\\n", at_ms=100)
        sim.press("a", start_ms=500, release_ms=600)
        sim.run_firmware("microbit_remote.py")
        print(sim.uart.sent)
    """

    def __init__(self, v2=True, max_ticks=300, loop_delay=50):
        self.v2 = v2
        self.max_ticks = max_ticks
        self.loop_delay = loop_delay
        self.clock = Clock()
        self.display = Display()
        self.uart = UART(self)
        self.music = Music()
        self.button_a = Button("a", self)
        self.button_b = Button("b", self)
        self.accelerometer = Accelerometer(self)
        self.pin_logo = Pin(self, "logo")
        self.pending_gestures = []

        self._pressed = set()
        self._touched = set()
        self._events = []          # [(at_ms, callable)]
        self._ticks = 0
        self.firmware_globals = {}

    # ---------- 测试脚本的排定接口 ----------

    def at(self, ms, action):
        """在虚拟时刻 ms 执行一次 action(sim)。"""
        self._events.append((ms, action))
        self._events.sort(key=lambda e: e[0])

    def press(self, button, start_ms, release_ms=None):
        self.at(start_ms, lambda sim: sim._pressed.add(button))
        if release_ms is not None:
            self.at(release_ms, lambda sim: sim._pressed.discard(button))

    def touch(self, start_ms, release_ms=None):
        self.at(start_ms, lambda sim: sim._touched.add("logo"))
        if release_ms is not None:
            self.at(release_ms, lambda sim: sim._touched.discard("logo"))

    def shake(self, at_ms):
        self.at(at_ms, lambda sim: sim.pending_gestures.append("shake"))

    def inject_serial(self, line, at_ms):
        self.at(at_ms, lambda sim: sim.uart.feed(line))

    # ---------- 给模拟对象回调 ----------

    def is_pressed(self, name):
        return name in self._pressed

    def is_touched(self, name):
        return name in self._touched

    # ---------- 模块组装 ----------

    def build_microbit_module(self):
        """拼出一个假的 microbit 模块对象。"""
        import types

        module = types.ModuleType("microbit")
        module.display = self.display
        module.button_a = self.button_a
        module.button_b = self.button_b
        module.accelerometer = self.accelerometer
        module.uart = self.uart
        module.Image = Image
        module.sleep = self.sleep
        module.running_time = self.running_time
        module.pin0 = Pin(self, "0")
        module.pin1 = Pin(self, "1")
        module.pin2 = Pin(self, "2")
        module.reset = lambda: None
        module.panic = lambda *a: None
        module.temperature = lambda: 25
        if self.v2:
            module.pin_logo = self.pin_logo       # V1 上这个名字不存在
        return module

    def sleep(self, ms):
        self._flush_events(advance_to=self.clock.now)
        self.clock.advance(ms)
        self._ticks += 1
        if self._ticks >= self.max_ticks:
            raise LoopStopped()

    def running_time(self):
        return self.clock.now

    def _flush_events(self, advance_to):
        while self._events and self._events[0][0] <= advance_to:
            _, action = self._events.pop(0)
            action(self)

    # ---------- 执行固件 ----------

    def run_firmware(self, path):
        """
        真正把固件源码跑起来。

        固件的 `main()` 里是 `while True`，靠 sleep 打到 max_ticks 抛 LoopStopped 收住。
        返回 True / False 表示是否耗到了上限（True 通常说明循环没崩、正常跑满了）。
        """
        import sys

        sys.modules["microbit"] = self.build_microbit_module()
        sys.modules["music"] = self.music

        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()

        self.firmware_globals = {"__name__": "__main__", "__file__": path}
        try:
            exec(compile(source, path, "exec"), self.firmware_globals)
            hit_limit = False
        except LoopStopped:
            hit_limit = True

        # 排定但还没触发的事件不影响结论
        return hit_limit

    def state(self, name):
        return self.firmware_globals.get(name)
