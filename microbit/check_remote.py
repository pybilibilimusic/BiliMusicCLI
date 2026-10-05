"""
micro:bit 板端固件的离线校验（**不需要开发板**）。

跑法：
    python microbit/check_remote.py

它做三件事：

1. **静态检查** —— micro:bit 的 MicroPython 是精简版，很多电脑 Python 的东西它没有。
   这里用 AST 直接扫源码，把「写的时候看不出来、刷进板子才炸」的用法拦下来：
   - 只允许 import 固件自带的模块
   - 不许用 f-string（老固件不支持）
   - 不许 bytes.decode(errors=...)（MicroPython 常常没有这个关键字参数）

2. **用模拟层真跑一遍固件** —— 借助 `stub_microbit.py` 提供的假 display / 按钮 / 串口，
   在虚拟时钟上按键、灌报文，检查状态机有没有按照预期发指令。

3. **协议一致性** —— 扫出固件会发出的所有指令，确认主机侧 `microbit_bridge` 认得，
   否则刷进板子按键无反应。
"""

import ast
import os
import re
import sys
import inspect

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIRMWARE = os.path.join(HERE, "microbit_remote.py")

for path in (HERE, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from stub_microbit import MicrobitSim, LoopStopped, Image  # noqa: E402

PASSED = 0
FAILED = 0


def check(label, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print("  [OK]   %s" % label)
    else:
        FAILED += 1
        suffix = ("  -> %s" % detail) if detail else ""
        print("  [FAIL] %s%s" % (label, suffix))


# ============================================================ 1. 静态检查
def test_static():
    print("\n[1] 静态：MicroPython 兼容性")

    check("固件文件存在", os.path.exists(FIRMWARE), FIRMWARE)
    with open(FIRMWARE, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)

    # --- import 白名单 ---
    # micro:bit 固件里确实存在的模块才算数
    allowed = {"microbit", "music", "radio", "speech", "audio", "math",
               "random", "utime", "time", "machine", "gc", "struct",
               "collections", "array", "sys", "neopixel", "_thread"}
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    # 之前 `import display` / `import uart` 就栽在这里：
    # display 和 uart 是 microbit 模块的成员，不是独立模块
    forbidden_modules = {"display", "uart", "re", "json", "os", "socket",
                         "requests", "pathlib", "typing", "sqlite3"}
    bad = sorted(set(imported) & forbidden_modules)
    check("没有 import micro:bit 上不存在的模块", not bad,
          "非法 import: %s" % bad)

    unknown = sorted(set(imported) - allowed - forbidden_modules)
    check("所有 import 都在固件白名单内", not unknown, "未知模块: %s" % unknown)

    # --- f-string ---
    fstrings = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]
    check("没有用 f-string（老固件不支持）", not fstrings,
          "共 %d 处" % len(fstrings))

    # --- bytes.decode(errors=...) ---
    risky_decode = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "decode":
                for kw in node.keywords:
                    if kw.arg == "errors":
                        risky_decode.append(node.lineno)
    check("decode() 没带 errors= 参数（MicroPython 常不支持）",
          not risky_decode, "行号: %s" % risky_decode)

    # --- 固件必须能在只有 ubinascii 级库的环境下跑：用通配符导入 + NameError 适配 V1/V2 ---
    check("源码里有 'from microbit import *'", "from microbit import *" in source)


# ============================================================ 2. 启动握手
def test_boot(v2):
    name = "V2" if v2 else "V1"
    print("\n[2] %s 模式：能否启动" % name)

    sim = MicrobitSim(v2=v2, max_ticks=120)
    ok = sim.run_firmware(FIRMWARE)
    check("%s 固件能跑满循环（没抛异常）" % name, ok)

    if v2:
        check("检测到 pin_logo -> HAS_LOGO=True", sim.state("HAS_LOGO") is True)
    else:
        check("没有 pin_logo -> HAS_LOGO=False", sim.state("HAS_LOGO") is False)

    # 暂停不能只押在 logo 上：logo 那块金手指氧化/沾汗就失灵，A+B 短按必须也能暂停
    check("A+B 短按 = PAUSE（V1/V2 一致）", sim.state("COMBO_SHORT_CMD") == "PAUSE",
          str(sim.state("COMBO_SHORT_CMD")))
    check("A+B 长按 = STOP（V1/V2 一致）", sim.state("COMBO_LONG_CMD") == "STOP",
          str(sim.state("COMBO_LONG_CMD")))

    check("串口已初始化且波特率 115200", sim.uart.initialized and sim.uart.baudrate == 115200,
          str(sim.uart.baudrate))
    check("开机画面显示过图案", sim.display.shown_images,
          str(sim.display.shown_images))

    # 主机还没回应时应该持续探握手，收到报文后停下
    queried = [m for m in sim.uart.sent if m == "QUERY"]
    check("未收到主机报文时会重发 QUERY 握手", len(queried) >= 2,
          "发了 %d 次" % len(queried))
    return sim


def test_handshake_stops():
    print("\n[3] 握手：收到主机报文后应停止刷 QUERY")

    sim = MicrobitSim(v2=True, max_ticks=160)
    sim.inject_serial("STATUS:play\n", at_ms=1200)
    sim.run_firmware(FIRMWARE)

    # 找出 1200ms 之后还在发的 QUERY —— 应该没有
    timeline = []
    sim2 = MicrobitSim(v2=True, max_ticks=160)
    sim2.inject_serial("STATUS:play\n", at_ms=1200)
    sim2.run_firmware(FIRMWARE)

    count_all = len([m for m in sim2.uart.sent if m == "QUERY"])
    check("收到 STATUS 后不再持续刷 QUERY", count_all <= 2,
          "共发了 %d 次 QUERY" % count_all)
    check("状态从 stop 变成 play", sim2.state("status") == "play",
          str(sim2.state("status")))


# ============================================================ 3. 串口解析
def test_parsing():
    print("\n[4] 串口：主机报文解析")

    sim = MicrobitSim(v2=True, max_ticks=200)
    sim.inject_serial("STATUS:play\n", at_ms=1000)
    sim.inject_serial("TITLE:Jay Chou - Qing Tian\n", at_ms=1100)
    sim.inject_serial("VOL:65\n", at_ms=1200)
    sim.inject_serial("TIME:37/242\n", at_ms=1300)
    sim.run_firmware(FIRMWARE)

    check("STATUS 解析正确", sim.state("status") == "play", str(sim.state("status")))
    check("TITLE 解析正确", sim.state("title") == "Jay Chou - Qing Tian",
          repr(sim.state("title")))
    check("VOL 解析成整数", sim.state("volume") == 65, str(sim.state("volume")))
    check("TIME 拆出已播/总长",
          (sim.state("position"), sim.state("duration")) == ("37", "242"),
          "%s/%s" % (sim.state("position"), sim.state("duration")))

    print("\n[5] 屏幕：TIME 不该反复打断歌名滚动（旧版的 bug）")
    # 5 秒内连发 3 条 TIME，滚动次数必须只由 TITLE 触发的那一次贡献
    sim2 = MicrobitSim(v2=True, max_ticks=100)     # 100 tick × 50ms ≈ 5s < 6s 周期
    sim2.inject_serial("STATUS:play\n", at_ms=600)
    sim2.inject_serial("TITLE:Windy Hill\n", at_ms=700)
    sim2.inject_serial("TIME:10/180\n", at_ms=1000)
    sim2.inject_serial("TIME:20/180\n", at_ms=2000)
    sim2.inject_serial("TIME:30/180\n", at_ms=3000)
    sim2.run_firmware(FIRMWARE)

    check("有歌名时会滚动", sim2.display.scroll_count >= 1,
          "滚动 %d 次" % sim2.display.scroll_count)
    check("连发 TIME 不会重启动滚动", sim2.display.scroll_count == 1,
          "滚动 %d 次（期望 1）" % sim2.display.scroll_count)
    check("滚动内容是「歌名 + 进度」，不再拖着音量那条看不清的尾巴",
          any("Windy Hill" in t and "V" not in t for t in sim2.display.scrolled_texts),
          str(sim2.display.scrolled_texts[:2]))
    check("滚动没有用 loop=True（否则会卡住后续 show）",
          all(loop is False for kind, _, loop in
              [(h[0], h[1], h[2]) for h in sim2.display.history if h[0] == "scroll"]))

    print("\n[6] 屏幕：暂停/停止状态")
    sim3 = MicrobitSim(v2=True, max_ticks=60)
    sim3.inject_serial("STATUS:pause\n", at_ms=800)
    sim3.run_firmware(FIRMWARE)
    images = sim3.display.shown_images
    check("暂停时显示暂停图标（不是内置图案）",
          any(getattr(img, "name", None) is None for img in images[-3:]),
          str(images[-3:]))

    sim4 = MicrobitSim(v2=True, max_ticks=60)
    sim4.inject_serial("STATUS:stop\n", at_ms=800)
    sim4.run_firmware(FIRMWARE)
    stop_icon = sim4.state("ICON_STOP")
    check("停止且无歌时显示 ✗", stop_icon in sim4.display.shown_images,
          str(sim4.display.shown_images[-3:]))
    check("✗ 用的是自定义暗色字模（不是满亮 9 的内置图案）",
          stop_icon is not None and "9" not in getattr(stop_icon, "spec", ""),
          str(stop_icon))


# ============================================================ 4. 按键
def press_and_collect(v2, presses, max_ticks=140, extra=None, keep_query=False):
    """
    跑一次固件，返回发出去的指令列表。

    默认过滤掉 QUERY —— 握手逻辑每隔一段时间就会发一条，
    不过滤的话会淹没掉按键产生的指令。需要数 QUERY 时传 keep_query=True。
    """
    sim = MicrobitSim(v2=v2, max_ticks=max_ticks)
    if extra:
        extra(sim)
    for start, release, button in presses:
        sim.press(button, start, release)
    sim.run_firmware(FIRMWARE)
    if keep_query:
        return list(sim.uart.sent), sim
    return [m for m in sim.uart.sent if m != "QUERY"], sim


def test_buttons():
    print("\n[7] 按键：短按 / 长按 / 组合")

    # --- A 短按 -> PREV ---
    sent, _ = press_and_collect(True, [(1000, 1150, "a")])
    check("A 短按发 PREV", sent == ["PREV"], str(sent))

    # --- B 短按 -> NEXT ---
    sent, _ = press_and_collect(True, [(1000, 1150, "b")])
    check("B 短按发 NEXT", sent == ["NEXT"], str(sent))

    # --- A 长按 -> VOLDOWN，且松手不再补发 PREV ---
    sent, sim = press_and_collect(True, [(1000, 2000, "a")], max_ticks=160)
    check("A 长按只发 VOLDOWN（不重复触发 PREV）", sent == ["VOLDOWN"], str(sent))

    # --- B 长按 -> VOLUP ---
    sent, _ = press_and_collect(True, [(1000, 2000, "b")], max_ticks=160)
    check("B 长按只发 VOLUP", sent == ["VOLUP"], str(sent))

    # --- 按住 300ms 松手仍在短按区间 -> PREV，不触发音量 ---
    sent, _ = press_and_collect(True, [(1000, 1350, "a")], max_ticks=120)
    check("按住 0.35s 松手算短按 PREV（不碰音量）", sent == ["PREV"], str(sent))

    # --- 组合键短按 -> PAUSE（V1/V2 一致，不再把暂停押在 logo 上）---
    # 注意 a 先按下、b 稍后（100ms 内），也算组合
    sent, _ = press_and_collect(True, [(1000, 1200, "a"), (1080, 1200, "b")])
    check("V2 组合 A+B 短按发 PAUSE", sent == ["PAUSE"], str(sent))

    sent, _ = press_and_collect(False, [(1000, 1200, "a"), (1080, 1200, "b")])
    check("V1 组合 A+B 短按同样发 PAUSE", sent == ["PAUSE"], str(sent))

    # --- 组合键按住不到 0.6s -> PAUSE ---
    sent, _ = press_and_collect(True, [(1000, 1400, "a"), (1000, 1400, "b")])
    check("按住 A+B 0.4s 松手算短按 PAUSE", sent == ["PAUSE"], str(sent))

    # --- 间隔超过组合窗口 -> 两次独立短按 ---
    sent, _ = press_and_collect(True, [(1000, 1150, "a"), (1600, 1750, "b")])
    check("间隔够远时不会误判成组合", sent == ["PREV", "NEXT"], str(sent))

    # --- 两键一起按住超过 0.6s -> STOP，且全程不该被长按补一刀音量 ---
    sent, _ = press_and_collect(True, [(1000, 2500, "a"), (1000, 2500, "b")],
                                max_ticks=180)
    check("V2 按住 A+B 超过 0.6s 只发 STOP（不夹杂音量）", sent == ["STOP"], str(sent))

    sent, _ = press_and_collect(False, [(1000, 2500, "a"), (1000, 2500, "b")],
                                max_ticks=180)
    check("V1 按住 A+B 超过 0.6s 同样只发 STOP", sent == ["STOP"], str(sent))


def test_logo_and_shake():
    print("\n[8] logo 触摸 / 摇一摇")

    def touch_once(sim):
        sim.touch(1000, 1400)

    sent, _ = press_and_collect(True, [], extra=touch_once)
    check("V2 触摸 logo 发 PAUSE", sent.count("PAUSE") >= 1, str(sent))

    def touch_long(sim):
        sim.touch(1000, 5000)      # 手指一直按着 4 秒

    sent, _ = press_and_collect(True, [], extra=touch_long, max_ticks=90)
    check("logo 按住不放只触发一次（边沿触发）",
          sent.count("PAUSE") == 1, "发了 %d 次 PAUSE" % sent.count("PAUSE"))

    def touch_twice(sim):
        sim.touch(1000, 1400)
        sim.touch(3000, 3400)

    sent, _ = press_and_collect(True, [], extra=touch_twice, max_ticks=90)
    check("松手后再摸一次能再触发", sent.count("PAUSE") == 2,
          "发了 %d 次 PAUSE" % sent.count("PAUSE"))

    sent, _ = press_and_collect(False, [], extra=lambda s: s.touch(1000, 1400))
    check("V1 没有 logo，触摸不会误触发 PAUSE", "PAUSE" not in sent, str(sent))

    def shake_it(sim):
        sim.shake(1200)

    # QUERY 会被握手逻辑反复发，所以不能只看「有没有」，要比数量：
    # 同样参数跑两次，唯一差别是 shake，看 QUERY 数量是否多出来
    base, _ = press_and_collect(True, [], keep_query=True)
    shaken, _ = press_and_collect(True, [], extra=shake_it, keep_query=True)
    check("摇一摇会多发一条 QUERY", len(shaken) > len(base),
          "无手势 %d 条 / 摇一摇 %d 条" % (len(base), len(shaken)))


# ============================================================ 5. 协议一致性
def test_protocol_compat():
    print("\n[9] 协议：固件发出的指令主机是否认得")

    import microbit_bridge

    with open(FIRMWARE, "r", encoding="utf-8") as handle:
        source = handle.read()

    # 固件源码里所有全大写的字符串。
    # 注意这里混着两个方向：板→主机的「指令」，和主机→板的「报文前缀」。
    all_caps = set(re.findall(r'"([A-Z][A-Z0-9]{2,})"', source))

    # 主机侧支持的指令：直接读 handle_line 的源码
    handler_src = inspect.getsource(microbit_bridge.MicrobitBridge.handle_line)
    host_cmds = set(re.findall(r'command == "([A-Z]+)"', handler_src))

    # 主机往外发的报文前缀，形如 self._write(f"STATUS:{status}")
    with open(microbit_bridge.__file__, "r", encoding="utf-8") as handle:
        bridge_src = handle.read()
    outbound = set(re.findall(r'f"([A-Z]+):', bridge_src))

    # 剔除接收方向的报文前缀后，剩下的才是固件真正往外发的指令
    firmware_tx = all_caps - outbound
    # 固件能解析的报文
    firmware_rx = set(re.findall(r'key == "([A-Z]+)"', source))

    check("提取到了固件指令", bool(firmware_tx), str(sorted(firmware_tx)))
    unknown = firmware_tx - host_cmds
    check("固件发出的每条指令主机都认得", not unknown,
          "主机不认得: %s（host 支持 %s）" % (sorted(unknown), sorted(host_cmds)))

    unsupported = outbound - firmware_rx
    check("主机发出的每种报文固件都能解析", not unsupported,
          "固件不认: %s（固件能认 %s）" % (sorted(unsupported), sorted(firmware_rx)))

    # 固件额外支持的 STATE 是预留格式（一条报文给全状态），主机目前用分量发送独占
    extra = firmware_rx - outbound
    if extra:
        print("  [    ] 固件额外支持但未启用的报文：%s" % sorted(extra))

    # 反过来：固件应该至少覆盖核心控制
    core = {"PAUSE", "NEXT", "PREV", "STOP"}
    missing = core - firmware_tx
    check("核心控制指令齐全", not missing, "固件没实现: %s" % sorted(missing))


def test_keepalive_no_redraw():
    """
    [10] 回归：主机的 keepalive 不该让屏幕「一抽一抽」。

    真实症状：歌名滚到一半就跳回开头重新滚，每几秒一次。
    根因是桥每 5 秒把同样的 STATUS/VOL/TITLE 重发一遍，而固件只要收到就判定
    「内容变了」并重绘，于是滚动被反复掐回起点。
    修法是「值没变就不重绘」，并且把周期重滚的间隔改成按滚动时长算，
    否则标题一长，6 秒的固定周期永远够不上滚完一趟。
    """
    print("\n[10] 屏幕：keepalive 不打断滚动")

    sim = MicrobitSim(v2=True, max_ticks=200)      # 200 tick × 50ms = 10s
    sim.inject_serial("STATUS:play\n", at_ms=600)
    sim.inject_serial("TITLE:Windy Hill\n", at_ms=700)
    sim.inject_serial("VOL:50\n", at_ms=750)
    # 5 秒后主机重推一轮完全相同的状态（这就是 bridge 的 keepalive）
    sim.inject_serial("STATUS:play\n", at_ms=5600)
    sim.inject_serial("VOL:50\n", at_ms=5650)
    sim.inject_serial("TITLE:Windy Hill\n", at_ms=5700)
    sim.inject_serial("TIME:88/180\n", at_ms=5750)
    sim.run_firmware(FIRMWARE)

    # 期望 2 次：TITLE 到达时一次 + 首次同步音量时一次（那两台都是值真的变了）。
    # 修复前 keepalive 的 STATUS/VOL/TITLE 会被各当作一次变化，总数是 5 次。
    check("重复的 keepalive 报文不会重启滚动", sim.display.scroll_count == 2,
          "滚动 %d 次（期望 2）" % sim.display.scroll_count)

    # 真的变了就得重画
    sim2 = MicrobitSim(v2=True, max_ticks=200)
    sim2.inject_serial("STATUS:play\n", at_ms=600)
    sim2.inject_serial("TITLE:Windy Hill\n", at_ms=700)
    sim2.inject_serial("STATUS:pause\n", at_ms=3000)
    sim2.run_firmware(FIRMWARE)
    check("状态真的变化时会重绘（切到暂停图标）",
          any(getattr(img, "name", None) is None for img in sim2.display.shown_images[-3:]),
          str(sim2.display.shown_images[-3:]))

    sim3 = MicrobitSim(v2=True, max_ticks=200)
    sim3.inject_serial("STATUS:play\n", at_ms=600)
    sim3.inject_serial("TITLE:Yang Cheng Lin - Yu Ai\n", at_ms=700)
    sim3.inject_serial("VOL:50\n", at_ms=750)
    sim3.run_firmware(FIRMWARE)
    text = sim3.firmware_globals["screen_text"]()
    period = sim3.firmware_globals["screen_period"]()
    duration = sim3.firmware_globals["scroll_duration"](text)
    check("重滚间隔不短于一次完整滚动的耗时",
          period >= duration,
          "标题 %r 滚完约 %.1fs，周期 %.1fs" % (text, duration / 1000.0, period / 1000.0))
    check("长标题下的周期比旧的固定 6 秒更长",
          period > 6000, "周期 %dms" % period)


def test_volume_announce():
    """音量从滚动串里摘出来，改成变化时静态闪数字。"""
    print("\n[11] 音量播报：静态闪数字，不再挂在滚动尾巴上")

    sim = MicrobitSim(v2=True, max_ticks=120)
    sim.inject_serial("STATUS:play\n", at_ms=600)
    sim.inject_serial("TITLE:Windy Hill\n", at_ms=700)
    sim.inject_serial("VOL:65\n", at_ms=3000)
    sim.run_firmware(FIRMWARE)

    build = sim.firmware_globals.get("volume_frames")
    wanted = [getattr(f, "spec", "") for f in build(65)] if build else []
    shown = [getattr(img, "spec", "") for img in sim.display.shown_images]

    check("音量 65 会拼出 V / 6 / 5 三帧", len(wanted) == 3, str(wanted))
    check("这三帧都真的显示过", wanted and all(w in shown for w in wanted),
          "%s / 实际 %s" % (wanted, shown[-5:]))
    check("播报用的是暗色字模（不是满亮 9）",
          wanted and all("9" not in w for w in wanted), str(wanted))
    check("歌名滚动里不再夹着音量",
          all("V" not in t for t in sim.display.scrolled_texts),
          str(sim.display.scrolled_texts))
    # 播报完要能回到歌名：TITLE 触发一次 + 播报结束一次
    check("播报结束后回到歌名滚动", sim.display.scroll_count == 2,
          "滚动 %d 次" % sim.display.scroll_count)

    # keepalive 反复推同一个音量，不该反复播报
    sim2 = MicrobitSim(v2=True, max_ticks=120)
    sim2.inject_serial("STATUS:play\n", at_ms=600)
    sim2.inject_serial("VOL:50\n", at_ms=700)
    sim2.inject_serial("VOL:50\n", at_ms=2500)
    sim2.inject_serial("VOL:50\n", at_ms=4000)
    sim2.run_firmware(FIRMWARE)
    shown2 = [getattr(img, "spec", "") for img in sim2.display.shown_images]
    check("重复的 VOL 不会反复播报",
          wanted and shown2.count(wanted[0]) <= 1,
          "V 帧出现了 %d 次" % shown2.count(wanted[0]) if wanted else "无基准帧")


def main():
    print("=" * 72)
    print("micro:bit 板端固件离线校验 —— 无需开发板")
    print("固件：%s" % os.path.relpath(FIRMWARE, ROOT))
    print("=" * 72)

    test_static()
    test_boot(v2=True)
    test_boot(v2=False)
    test_handshake_stops()
    test_parsing()
    test_buttons()
    test_logo_and_shake()
    test_keepalive_no_redraw()
    test_volume_announce()
    test_protocol_compat()

    print("\n" + "=" * 72)
    print("结果：%d 通过 / %d 失败 / 共 %d" % (PASSED, FAILED, PASSED + FAILED))
    print("=" * 72)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
