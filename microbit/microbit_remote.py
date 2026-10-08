# =============================================================================
# micro:bit 遥控端参考实现（MicroPython）
#
# 这个文件**不是**给电脑上的 Python 跑的，要刷进 micro:bit 板子：
#   1. 在 https://python.microbit.org 粘贴进去，下载 .hex 拖进 MICROBIT 盘；或
#   2. 用 Mu Editor / Thonny 烧录到 micro:bit
#
# 按键映射（V1 / V2 一致，不再有版本差异）：
#   | 操作                 | 发出的指令 | 说明               |
#   | ---                  | ---        | ---                |
#   | A 短按               | PREV       | 上一首             |
#   | A 长按(>0.6s)        | VOLDOWN    | 音量 -10           |
#   | B 短按               | NEXT       | 下一首             |
#   | B 长按(>0.6s)        | VOLUP      | 音量 +10           |
#   | A+B 短按             | PAUSE      | 播放 / 暂停切换    |
#   | A+B 长按(>0.6s)      | STOP       | 停止               |
#   | 触摸 logo（仅 V2）   | PAUSE      | 和 A+B 短按等价（氧化失灵时就用 A+B） |
#   | 摇一摇               | QUERY      | 让主机重发全量状态 |
#
# 屏幕：滚动「歌名 已播/总长」；音量变化时静态闪「V + 数字」，不挂在滚动串上
# 串口 115200，每行 ASCII 加 \n 结尾
#
# logo 是电容感应不是按键，氧化 / 受潮会读不到，所以暂停没押在它身上
# （A+B 短按同样能暂停）。另两道兜底：开板就按着不会误触发暂停；
# 传感器卡在「一直被触摸」超过 LOGO_STUCK_MS 就自动停用 logo。
#
# ⚠️ 四个容易踩的坑：
#   1. uart.init() 之后 REPL 就断了，Mu / Thonny 打不开串口（重刷 .hex 即可）
#   2. 点阵字体只有 ASCII，中文歌名显示成方块是正常的（主机侧会转成拼音）
#   3. 屏幕相关的都不能用 sleep()，否则睡眠期间按键和串口没人管
#   4. 本文件必须 < 20151 字节（uflash 硬限制），否则 --regen 会失败
# =============================================================================

# 用 `*` 而不是逐个列出：micro:bit V1 / V2 成员不同，
# 通配符导入后再靠 NameError 判断引脚是否存在，就能自动适配版本。
from microbit import *

# micro:bit V2 才有 logo 触摸引脚；V1 上这个名字不存在，靠 NameError 自动降级
try:
    pin_logo
    HAS_LOGO = True
except NameError:
    HAS_LOGO = False

# music 是内置模块，个别精简固件里可能没有，缺了也只影响提示音
try:
    import music
except ImportError:
    music = None

# ---------------------------------------------------------------- 可调参数
BAUDRATE = 115200
LOOP_DELAY_MS = 50        # 主循环轮询间隔
LONG_PRESS_MS = 600       # 超过这个时长算长按（音量）
COMBO_WINDOW_MS = 250     # A / B 在这个窗口内先后按下，算「同时按」
SCREEN_PERIOD_MS = 6000   # 兜底：至少这么久才重滚一遍
SCROLL_DELAY_MS = 150     # display.scroll 的每步延时，要和它自己的默认值保持一致
TOUCH_DEBOUNCE_MS = 400   # logo 触摸去抖（手指按住会连续触发）
LOGO_STUCK_MS = 5000      # logo 连续报「被触摸」超过这么久，判定传感器卡死并停用
HANDSHAKE_TIMEOUT = 15    # 开机后最多发这么多次 QUERY 握手
GLYPH_BRIGHT = 4          # 自定义字模/图标的亮度；点阵满亮是 9，晚上看久了刺眼
DIGIT_MS = 420            # 音量播报时每一位停留多久

# 组合键：短按暂停、长按停止。
# 以前是「V2 组合键发 STOP、V1 发 PAUSE」，把暂停整个押在 logo 触摸上——
# logo 那块金手指一氧化/沾汗就没法暂停了，所以改成按键也能暂停，V1/V2 一致。
COMBO_SHORT_CMD = "PAUSE"
COMBO_LONG_CMD = "STOP"

# ---------------------------------------------------------------- 自定义字模
# 点阵只有 5x5，数字用 3 列宽的字模居中画。
# 亮度统一用 GLYPH_BRIGHT 而不是 9：display.scroll() 没法调暗（MicroPython 没暴露
# 亮度接口），能调的只有自己画的图案，能压一点是一点。
_DIGIT_SHAPES = {
    "0": ("###", "#.#", "#.#", "#.#", "###"),
    "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"),
    "3": ("###", "..#", "###", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"),
    "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"),
    "7": ("###", "..#", "..#", "..#", "..#"),
    "8": ("###", "#.#", "###", "#.#", "###"),
    "9": ("###", "#.#", "###", "..#", "###"),
}

_SHAPE_V = ("#.#", "#.#", "#.#", "#.#", ".#.")   # 音量前缀，和冒号/斜杠区分开
_SHAPE_PAUSE = ("#.#", "#.#", "#.#", "#.#", "#.#")
_SHAPE_STOP = ("#.#.#", ".#.#.", "..#..", ".#.#.", "#.#.#")   # ✗
_SHAPE_NOTE = ("..###", "..#..", "..#..", "###..", "###..")   # 八分音符


def glyph_image(shape):
    """
    把 '#'/'.' 组成的图案转成 Image，亮度用 GLYPH_BRIGHT。

    不足 5 列的（数字是 3 列宽）居中补齐 —— Image 的每一行必须是 5 个字符，
    少一个字符 MicroPython 直接报错，而这个错误在电脑上跑测试是看不出来的。
    """
    rows = []
    for row in shape:
        line = ""
        for ch in row:
            line += str(GLYPH_BRIGHT) if ch == "#" else "0"
        pad = 5 - len(line)
        left = pad // 2
        line = "0" * left + line + "0" * (pad - left)
        rows.append(line)
    return Image(":".join(rows))


def volume_frames(value):
    """音量播报的画面序列：[V, 各位数字]。"""
    frames = [glyph_image(_SHAPE_V)]
    for ch in str(value):
        shape = _DIGIT_SHAPES.get(ch)
        if shape:
            frames.append(glyph_image(shape))
    return frames


ICON_PAUSE = glyph_image(_SHAPE_PAUSE)
ICON_STOP = glyph_image(_SHAPE_STOP)
ICON_NOTE = glyph_image(_SHAPE_NOTE)


# ---------------------------------------------------------------- 运行时状态
status = "stop"           # play / pause / stop
title = ""                # 当前歌名（主机已做过 ASCII 化）
volume = 0                # 0-100
position = "?"            # 已播秒数
duration = "?"            # 总秒数

_host_seen = False        # 是否已经收到过主机的报文
_handshake_left = HANDSHAKE_TIMEOUT
_last_query_at = 0
_last_scroll_at = 0
_last_touch_at = 0
_logo_down = False        # logo 是否处于「已按下未松开」，用于边沿触发
_logo_primed = False      # 开机后是否已采样过一次 logo 初始状态
_touch_since = 0          # 本次连续「被触摸」从什么时候开始

# 按键状态机
_a_down = False
_b_down = False
_a_down_at = 0
_b_down_at = 0
_a_long_fired = False
_b_long_fired = False
_press_queue = []         # [(键名, 按下时刻)]，用来识别组合键
_combo_at = None          # 组合键按下的起始时刻；两键都松手后才决定 PAUSE / STOP

# 音量播报。做成状态机而不是 sleep()，否则播报期间按键和串口全没人管
_volume_frames = []
_volume_index = 0
_volume_until = 0
_volume_changed = False   # 只有音量真的变了才播报（keepalive 会反复推同样的值）


# ---------------------------------------------------------------- 串口输出
def send(line):
    """往主机发一条指令。"""
    try:
        uart.write(line + "\n")
    except Exception:
        pass


def beep(notes):
    """按键提示音。没有蜂鸣器就静默跳过。"""
    if music is None:
        return
    try:
        music.play(notes, wait=False)
    except Exception:
        pass


# ---------------------------------------------------------------- 屏幕
def screen_text():
    """
    拼出要滚动的那一行：「歌名  已播/总长」。

    音量**刻意不放在这里**：一整条三十几个字符的滚动串，尾巴上的「V65」一闪就没了，
    根本来不及看。音量改成变化时单独静态闪数字，见 pump_screen()。
    """
    parts = []
    if title:
        parts.append(title)
    if position != "?" or duration != "?":
        parts.append(position + "/" + duration)
    return "  ".join(parts)


def scroll_duration(text):
    """
    估算一次完整滚动要多少毫秒。

    scroll 每个字符约 6 步（5 列字宽 + 1 列间隙），最后还要多滚屏幕本身那 5 列。
    **估算宁可比实际长** —— 估短了会在没滚完时就重启，屏幕上就是「滚一截又跳回去」。
    """
    return SCROLL_DELAY_MS * (len(text) * 6 + 5)


def screen_period():
    """
    多久重滚一遍。

    不能写死成 6 秒：一首歌的标题串起来有三十几个字符，滚完要半分钟，
    固定 6 秒重启的话你只能看到开头那一小段，观感就是一直在抽。
    """
    return max(SCREEN_PERIOD_MS, scroll_duration(screen_text()) + 1500)


def render():
    """按当前状态刷新屏幕。"""
    global _last_scroll_at
    _last_scroll_at = running_time()
    if status == "pause":
        display.show(ICON_PAUSE)
    elif status == "stop" and not title:
        display.show(ICON_STOP)
    elif title:
        # 注意这里用 loop=False：无限循环滚动会把后续的 show() 挡住，
        # 屏幕会永远卡在滚动状态。周期性重滚由主循环负责。
        display.scroll(screen_text(), wait=False, loop=False)
    elif status == "play":
        display.show(ICON_NOTE)
    else:
        display.show(ICON_STOP)


def start_volume_announce():
    """音量变了 -> 排队播报「V」+ 各位数字。真正显示由 pump_screen 推进。"""
    global _volume_frames, _volume_index, _volume_until
    _volume_frames = volume_frames(volume)
    _volume_index = 0
    _volume_until = 0


def pump_screen(now):
    """
    每轮推进一格屏幕状态。

    为什么不用 sleep() 把「V65」整段睡出来：睡眠期间 tick 停摆，
    按键轮询和串口读取全断，长按、组合键、切歌都会漏掉。
    """
    global _volume_frames, _volume_index, _volume_until

    if _volume_frames:
        if now >= _volume_until:
            if _volume_index >= len(_volume_frames):
                # 播完了，回到歌名滚动
                _volume_frames = []
                _volume_index = 0
                _volume_until = 0
                render()
            else:
                display.show(_volume_frames[_volume_index])
                _volume_index += 1
                _volume_until = now + DIGIT_MS
        return

    # 周期重滚一遍，间隔按滚动串长度算，保证上一轮滚完才轮到下一轮
    if now - _last_scroll_at >= screen_period():
        render()


# ---------------------------------------------------------------- 串口输入
def parse_line(line):
    """
    处理主机发来的一行报文。返回 True 表示屏幕内容受影响、需要重绘。

    注意：TIME 报文**不**触发重绘 —— 它每几秒就来一次，
    如果每次都重启滚动，歌名永远滚不完整。
    """
    global status, title, volume, position, duration, _host_seen, _volume_changed

    if ":" in line:
        key = line[:line.find(":")]
        value = line[line.find(":") + 1:].strip()
    else:
        key = line
        value = ""

    if key == "STATUS":
        _host_seen = True
        if value == status:
            # keepalive 每 5 秒重发同样的状态；当成变化会把滚动掐回起点（看着像抽风）
            return False
        status = value
        return True
    if key == "VOL":
        try:
            new_volume = int(value)
        except ValueError:
            return False
        _host_seen = True
        if new_volume == volume:
            return False
        volume = new_volume
        _volume_changed = True
        return True
    if key == "TITLE":
        _host_seen = True
        if value == title:
            return False
        title = value
        return True
    if key == "TIME":
        _host_seen = True
        # "37/242" 这种格式；缺一半时另一半保持 ?
        if "/" in value:
            position, duration = value[:value.find("/")], value[value.find("/") + 1:]
        return False
    if key == "STATE":
        # "play|65|37/242"，一次性给全。同理：值没变就不要重绘
        _host_seen = True
        changed = False
        fields = value.split("|")
        if fields[0] and fields[0] != status:
            status = fields[0]
            changed = True
        if len(fields) >= 2 and fields[1]:
            try:
                new_volume = int(fields[1])
                if new_volume != volume:
                    volume = new_volume
                    _volume_changed = True
                    changed = True
            except ValueError:
                pass
        if len(fields) >= 3 and "/" in fields[2]:
            t = fields[2]
            position, duration = t[:t.find("/")], t[t.find("/") + 1:]
        return changed
    return False


def read_serial():
    """把串口缓冲区里的行读完，必要时返回 True 表示要重绘。"""
    need_render = False
    while uart.any():
        try:
            raw = uart.readline()
        except Exception:
            break
        if not raw:
            break
        try:
            line = raw.decode("utf-8").strip()
        except Exception:
            continue          # 脏数据，扔掉
        if line and parse_line(line):
            need_render = True
    return need_render


# ---------------------------------------------------------------- 按键
def poll_buttons():
    """
    每轮调用一次，返回一条待发指令，没有就返回 None。

    用「按下事件队列」识别单键短按：按下先进队，等到松手（或超过 COMBO_WINDOW_MS
    还没有第二个键加入）才判定。这样既不会吞掉单键，
    也不会像共用去抖时间戳那样让组合键永远轮不到。

    组合键（A+B）单独处理：两键都在按住时**挂起**，等两键都松手才按按住时长决定
    发 PAUSE 还是 STOP。判定时机必须在单键长按之前，否则按住 A+B 会先触发 VOLDOWN。
    """
    global _a_down, _b_down, _a_down_at, _b_down_at
    global _a_long_fired, _b_long_fired, _press_queue, _combo_at

    now = running_time()
    a = button_a.is_pressed()
    b = button_b.is_pressed()

    # --- 按下沿：入队 ---
    if a and not _a_down:
        _a_down = True
        _a_down_at = now
        _a_long_fired = False
        _press_queue.append(("a", now))
    if b and not _b_down:
        _b_down = True
        _b_down_at = now
        _b_long_fired = False
        _press_queue.append(("b", now))

    # --- 抬起沿 ---
    if not a and _a_down:
        _a_down = False
    if not b and _b_down:
        _b_down = False

    # --- 组合键挂起：两键都在按住才算，且必须排在长按之前 ---
    if _combo_at is None and _a_down and _b_down:
        span = _a_down_at - _b_down_at
        if span <= COMBO_WINDOW_MS and -span <= COMBO_WINDOW_MS:
            _combo_at = min(_a_down_at, _b_down_at)
            _press_queue = []

    if _combo_at is not None:
        # 挂着组合时把长按标记吃掉，避免 A+B 按住超时被当成两次音量键
        _a_long_fired = True
        _b_long_fired = True
        if not _a_down and not _b_down:
            held = now - _combo_at
            _combo_at = None
            _press_queue = []
            return COMBO_LONG_CMD if held >= LONG_PRESS_MS else COMBO_SHORT_CMD
        return None

    # --- 长按：到点立刻触发，并把该键的短按机会勾销 ---
    if a and _a_down and not _a_long_fired and now - _a_down_at >= LONG_PRESS_MS:
        _a_long_fired = True
        _press_queue = [e for e in _press_queue if e[0] != "a"]
        return "VOLDOWN"
    if b and _b_down and not _b_long_fired and now - _b_down_at >= LONG_PRESS_MS:
        _b_long_fired = True
        _press_queue = [e for e in _press_queue if e[0] != "b"]
        return "VOLUP"

    # --- 单键：必须等「已经松手」才判定，否则按住不放会先触发短按再触发长按 ---
    if _press_queue:
        pending, at = _press_queue[0]
        key_down = _a_down if pending == "a" else _b_down
        if key_down:
            return None                       # 还按着：留给长按或组合去处理
        others_down = _a_down or _b_down
        # 松手且没别的键压着 -> 直接认定短按；否则等组合窗口过去再定
        if not others_down or now - at > COMBO_WINDOW_MS:
            _press_queue.pop(0)
            return "PREV" if pending == "a" else "NEXT"

    return None


def poll_gestures():
    """logo 触摸 / 摇一摇。"""
    global _last_touch_at, _logo_down, _logo_primed, _touch_since, HAS_LOGO

    now = running_time()

    if HAS_LOGO:
        try:
            touched = pin_logo.is_touched()
        except Exception:
            touched = False

        # 开机第一次轮询只采样：按复位键时手指常在 logo 上，不采样会白送一次暂停
        if not _logo_primed:
            _logo_primed = True
            _logo_down = bool(touched)
            _touch_since = now if touched else 0
            return None

        if touched:
            if _touch_since == 0:
                _touch_since = now
            elif now - _touch_since > LOGO_STUCK_MS:
                # 连续几秒都报「被触摸」= 传感器卡死。再等下去 logo 会永久停在
                # 已按下状态、再也出不来 PAUSE，不如停用，把暂停交给 A+B 短按
                HAS_LOGO = False
                _logo_down = False
                return None
        else:
            _touch_since = 0

        # 边沿触发：按着只算一次，松手才允许再触发（否则按住会来回切换播放/暂停）
        if touched and not _logo_down and now - _last_touch_at > TOUCH_DEBOUNCE_MS:
            _logo_down = True
            _last_touch_at = now
            return "PAUSE"
        if not touched:
            _logo_down = False

    try:
        if accelerometer.was_gesture("shake"):
            return "QUERY"
    except Exception:
        pass

    return None


# ---------------------------------------------------------------- 主循环
def boot():
    """上电画面 + 串口初始化。"""
    uart.init(baudrate=BAUDRATE)
    display.show(Image.HAPPY)
    sleep(500)
    render()


def tick():
    """主循环的一轮。返回 True 表示这一轮有东西要发。"""
    global _handshake_left, _last_query_at, _volume_changed

    now = running_time()

    need_render = read_serial()

    command = poll_buttons()
    if command is None:
        command = poll_gestures()

    if command is not None:
        send(command)
        beep(["c5:1"] if command in ("PAUSE", "NEXT", "PREV") else ["c4:1"])

    # 音量真的变了 -> 先静态播报音量；否则该重绘就重绘
    if _volume_changed:
        _volume_changed = False
        start_volume_announce()
    elif need_render:
        render()

    # 滚动和音量播报都是异步的，每轮推进一格
    pump_screen(now)

    # 开机握手：主机程序可能还没跑起来，多发几次 QUERY 直到它回应
    if not _host_seen and _handshake_left > 0:
        if now - _last_query_at >= 1000:
            _last_query_at = now
            _handshake_left -= 1
            send("QUERY")

    return command is not None


def main():
    boot()
    while True:
        tick()
        sleep(LOOP_DELAY_MS)


try:
    main()
except Exception as exc:        # 真机崩了只有哭脸+数字，把原因从串口报出去
    if type(exc).__name__ != "LoopStopped":     # 离线模拟层结束主循环的哨兵，不是崩溃
        send("PANIC:%s:%s" % (type(exc).__name__, exc))
    raise
