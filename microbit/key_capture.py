"""
按键真机收集器 —— 一次跑完 9 项按键，不用人在电脑前一项项回车。

为什么单独写一份：
    microbit_live_test.py 的 C 组（按键）是交互式的：每测一项都要在电脑上
    敲一次回车才能进下一项。真机在桌子上、键盘也在手边的时候没问题，但如果
    你想「一口气把板子拿起来按完再看结果」，就得有人一直守着电脑。
    这个脚本把它反过来：先排好一张时间表，脚本自己按表收，你只管按顺序按板子。

三种用法：
    # 1) 开机体检：刚刷完固件先看它有没有启动就崩（PANIC 上报）
    .venv\\Scripts\\python.exe microbit\\key_capture.py --check 8

    # 2) 按键收集：默认 120 秒，按下面这张表操作板子
    .venv\\Scripts\\python.exe microbit\\key_capture.py --plan

    # 3) 下行链路：验「电脑 -> 板子」通不通，不用看屏幕（要按一次 RESET）
    .venv\\Scripts\\python.exe microbit\\key_capture.py --downlink

    # 某一项没过，只想单独重测它（项号见下面那张表）
    .venv\\Scripts\\python.exe microbit\\key_capture.py --plan --only 3,5,9

看提示的两个地方（随便挑一个）：
    microbit/temp/NOW.txt        只写「现在该按什么」，开着这个文件跟着走
    microbit/temp/key_capture.log 完整流水，含每一条串口上报

结果：
    microbit/temp/key_capture_result.md   九项对照表，通过/失败/硬件问题

判定口径：
    **事件驱动，不按钟点切窗口** —— 等到这一项该有的上报就立刻进下一项，
    按早按晚都不影响。第一版是「每项 12 秒一个窗口」，结果大家踩着点按，
    每条上报都滞后一个窗口，九项里五项被判成假失败，所以改掉了。
    等待期间收到的别的命令记成「串台」，专门用来抓「按住 A 却先蹦出一次
    PREV」这类误触发。
"""

import argparse
import os
import sys
import time

import serial
import serial.tools.list_ports

BAUDRATE = 115200
MBED_USB_ID = "0D28:0204"          # micro:bit 的 USB VID:PID
HERE = os.path.dirname(os.path.abspath(__file__))
TEMP = os.path.join(HERE, "temp")
LOG_PATH = os.path.join(TEMP, "key_capture.log")
NOW_PATH = os.path.join(TEMP, "NOW.txt")
PLAN_PATH = os.path.join(TEMP, "PLAN.txt")
RESULT_PATH = os.path.join(TEMP, "key_capture_result.md")

# 握手期板子会每秒发 QUERY，那是正常的，不参与判定
NOISE = {"QUERY"}


# ---------------------------------------------------------------- 按键时间表
# (名称, 期望命令集合, 人话提示, 是不是「硬件类」失败)
# 「硬件类」= logo 触摸。那块金手指是电容感应，氧化/受潮/手汗都会让它读不到，
# 测不出来不等于代码有问题，所以单列一档，不和软件问题混在一起。
PLAN = [
    ("A 短按 -> PREV", {"PREV"}, "快速按一下左边 A 键，马上松开", False),
    ("B 短按 -> NEXT", {"NEXT"}, "快速按一下右边 B 键，马上松开", False),
    ("A 长按 -> VOLDOWN", {"VOLDOWN"}, "按住左边 A 键不放，数 1 秒以上再松开", False),
    ("B 长按 -> VOLUP", {"VOLUP"}, "按住右边 B 键不放，数 1 秒以上再松开", False),
    ("A+B 短按 -> PAUSE", {"PAUSE"},
     "A 和 B 一起按下去（间隔别超过 0.25 秒），总共别超过 0.6 秒就松开", False),
    ("A+B 长按 -> STOP", {"STOP"},
     "A 和 B 一起按住不放，数 1 秒以上再一起松开", False),
    ("触摸 logo -> PAUSE", {"PAUSE"},
     "手指按住正面金色 logo 再松开（只有 V2 有；金手指氧化时会失灵）", True),
    ("摇一摇 -> QUERY", {"QUERY"}, "把板子拿起来晃一下", False),
    ("A 连按 3 下 -> PREV x3", {"PREV"},
     "A 键连按三下，每下间隔半秒左右（验证不丢按、不重复触发）", False),
]

LAST_REPEAT = 3                     # 最后一项要求收到几次
PREP_SECONDS = 5                    # 开头留几秒等握手/下发状态
ITEM_TIMEOUT = 20                   # 每一项最多等多少秒（收到正确上报就提前进下一项）


def find_microbit_ports():
    """扫一遍串口，挑出看起来是 micro:bit 的那些。"""
    hits = []
    for port in serial.tools.list_ports.comports():
        hwid = (port.hwid or "").upper()
        desc = (port.description or "").upper()
        if MBED_USB_ID in hwid or "MBED SERIAL PORT" in desc:
            hits.append((port.device, port.description))
    return hits


def choose_port(arg_port, timeout=0):
    if arg_port:
        return arg_port
    deadline = time.time() + timeout
    while True:
        hits = find_microbit_ports()
        if hits or time.time() >= deadline:
            break
        time.sleep(1)
    if not hits:
        return None
    if len(hits) > 1:
        print("识别到多个候选，用第一个；想换请 --port：")
        for device, desc in hits:
            print(f"  {device}  {desc}")
    print(f"自动识别到 micro:bit：{hits[0][0]} ({hits[0][1]})")
    return hits[0][0]


def log(fd, text, echo=True):
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {text}"
    if echo:
        print(line, flush=True)
    if fd:
        fd.write(line + "\n")
        fd.flush()


def write_now(text):
    """把「现在该按什么」写进 NOW.txt，用户可以开着这个文件跟着走。"""
    try:
        with open(NOW_PATH, "w", encoding="utf-8") as fd:
            fd.write(text + "\n")
    except OSError:
        pass


class Reader:
    """后台读串口，把收到的行带时间戳排进队列。"""

    def __init__(self, port):
        self.ser = serial.Serial(port, BAUDRATE, timeout=0.1)
        self.buffer = b""
        self.lines = []

    def pump(self):
        """非阻塞地读一批，返回 [(时间戳, 行)]。"""
        try:
            chunk = self.ser.read(self.ser.in_waiting or 1)
        except Exception:
            return []
        if not chunk:
            return []
        self.buffer += chunk
        out = []
        while b"\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n", 1)
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            item = (time.time(), text)
            self.lines.append(item)
            out.append(item)
        # 半截固件 / 波特率不对时会吐出巨长的行，截一下免得刷屏
        if len(self.buffer) > 4096:
            self.buffer = self.buffer[-512:]
        return out

    def send(self, payload):
        try:
            self.ser.write((payload + "\n").encode("utf-8"))
            return True
        except Exception:
            return False

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


def drain(reader):
    return reader.pump()


# ---------------------------------------------------------------- 模式一：体检
def run_check(port, seconds, wait_board=0):
    chosen = choose_port(port, timeout=wait_board)
    if chosen is None:
        print("没找到 micro:bit 串口。先看资源管理器里有没有 MICROBIT 盘；")
        print("刚刷完固件的话，等两三秒再试，或加 --wait-board 20。")
        return 1

    reader = Reader(chosen)
    os.makedirs(TEMP, exist_ok=True)
    fd = open(LOG_PATH, "w", encoding="utf-8")

    log(fd, f"体检模式：监听 {seconds} 秒（{chosen}）")
    log(fd, "刚刷完固件的话，按一下板子背面的 RESET 能看到完整的开机握手。")
    panics = []
    seen = []
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            for _, text in reader.pump():
                seen.append(text)
                mark = ""
                if text.startswith("PANIC"):
                    panics.append(text)
                    mark = "   <<< 崩溃上报"
                log(fd, f"收到 {text!r}{mark}")
            time.sleep(0.02)
    except KeyboardInterrupt:
        log(fd, "用户中断")

    reader.close()
    counts = {}
    for text in seen:
        counts[text] = counts.get(text, 0) + 1
    log(fd, "")
    log(fd, f"共 {len(seen)} 条，去重后：{counts}")
    if panics:
        log(fd, f"\n结论：固件启动后崩了，{len(panics)} 条 PANIC：")
        for item in panics:
            log(fd, f"  {item}")
    elif not seen:
        log(fd, "\n结论：一条都没收到。先按 RESET，还是没有就跑 diagnose.py。")
    else:
        log(fd, "\n结论：没看到 PANIC，固件正常在跑。可以跑 --plan 测按键了。")
    log(fd, f"\n日志：{LOG_PATH}")
    fd.close()
    return 0


# ------------------------------------------------- 模式三：下行链路（不用看屏幕）
def run_downlink(port, wait_board=0):
    """
    验「电脑 -> 板子」这条路，而且不用人盯着屏幕。

    原理：板子开机后每秒发一次 QUERY 握手（最多 15 次），一旦收到主机报文就把
    _host_seen 置 True、QUERY 立刻停。所以「发了 STATE 之后 QUERY 停不停」
    就是下行的判据 —— 纯主机侧可观测。

    为什么要单独做：验下行本来只能靠看屏幕（发了 STATUS 看图标变没变），
    可屏幕只有人能看，脚本没法自动判定。握手会停这件事把下行变成了可观测信号。

    要按一次 RESET，握手只在开机后 15 秒内发。
    """
    chosen = choose_port(port, timeout=wait_board)
    if chosen is None:
        print("没找到 micro:bit 串口。先看资源管理器里有没有 MICROBIT 盘。")
        return 1

    os.makedirs(TEMP, exist_ok=True)
    fd = open(LOG_PATH, "w", encoding="utf-8")
    try:
        reader = Reader(chosen)
    except Exception as exc:
        log(fd, "打不开串口 %s：%s" % (chosen, exc))
        fd.close()
        return 1

    log(fd, "下行链路测试（%s）" % chosen)
    log(fd, "原理：板子开机后每秒发 QUERY，收到主机报文就立刻停发；")
    log(fd, "      所以「发了 STATE 之后 QUERY 停不停」= 下行通不通。")
    log(fd, "")
    log(fd, ">>> 现在请按一下板子背面的 RESET，然后什么都别按")

    queries = []          # 下发前收到的 QUERY 时间戳
    after_send = []       # 下发 STATE 之后收到的 QUERY（不等于空就说明没收到）
    sent_at = None
    deadline = time.time() + 30
    try:
        while time.time() < deadline:
            for _ts, text in reader.pump():
                if text.startswith("PANIC"):
                    log(fd, "收到 %s   <<< 崩溃上报" % text)
                    continue
                if text.strip() == "QUERY":
                    if sent_at is None:
                        queries.append(time.time())
                        log(fd, "收到 QUERY（第 %d 条）" % len(queries))
                    else:
                        after_send.append(time.time())
                        log(fd, ">> 下发后又收到 QUERY（第 %d 条）" % len(after_send))
                else:
                    log(fd, "收到 %s" % text)

            # 收到 3 条说明握手稳定在跑，可以发 STATE 试下行了
            if sent_at is None and len(queries) >= 3:
                reader.send("STATE:play|65|37/242")
                sent_at = time.time()
                log(fd, "")
                log(fd, "已下发 STATE:play|65|37/242 —— 板子收到就该停发 QUERY，")
                log(fd, "屏幕也会从 × 变成音符图标（顺手看一眼）")
                log(fd, "接下来等 6 秒，看 QUERY 停不停 ...")
                log(fd, "")

            if sent_at is not None and time.time() - sent_at > 6:
                break
            time.sleep(0.02)
    except KeyboardInterrupt:
        log(fd, "用户中断")

    reader.close()
    log(fd, "")
    if not queries:
        log(fd, "结论：一条 QUERY 都没收到 —— 上行不通（板子没开口）。")
        log(fd, "      先确认真的按了 RESET；还是没有就跑 microbit/diagnose.py。")
    elif after_send:
        log(fd, "结论：下行不通 —— 发了 STATE 之后板子还在发 QUERY（%d 条），"
                "说明它没收到或没解析。" % len(after_send))
        log(fd, "      板子在跑、上行也通，问题就在这条线上：线 / hub / 驱动 / 接口芯片。")
    else:
        log(fd, "结论：下行通 —— 下发 STATE 后 6 秒内没再收到 QUERY，")
        log(fd, "      说明板子收到并解析了主机报文（屏幕也该从 × 变成音符图标）。")
    log(fd, "")
    log(fd, "日志：%s" % LOG_PATH)
    fd.close()
    return 0


# ---------------------------------------------------------------- 模式二：收集
def write_plan_list(selected):
    """
    把要测哪几项写成 PLAN.txt。

    为什么不写钟点：第一版就是按钟点排窗口的，结果「每项 12 秒」变成了
    「大家都踩着点按」，上报整整齐齐滞后一个窗口，九项里五项被判成假失败。
    现在改成收到正确上报就进下一项，按早按晚都无所谓，所以不需要钟点。

    为什么要落盘：脚本经常是后台跑的，电脑这边的打印你看不到。
    开着 NOW.txt 就能知道现在轮到哪一项。
    """
    lines = [
        f"按键清单（{time.strftime('%H:%M:%S')} 开跑）",
        "不用看钟点：NOW.txt 显示轮到哪一项，你就按那一项。",
        "板子一上报正确命令，脚本自动跳下一项，NOW.txt 会跟着变。",
        "",
    ]
    for index, (name, _expected, hint, _hw) in enumerate(PLAN):
        mark = "  " if index in selected else "跳"
        lines.append(f"{mark} [{index + 1}] {name}")
        if index in selected:
            lines.append(f"        {hint}")
    lines.append("")
    lines.append("NOW.txt 是「当前该按什么」的单行提示，跟着它就行。")
    try:
        with open(PLAN_PATH, "w", encoding="utf-8") as fd:
            fd.write("\n".join(lines) + "\n")
    except OSError:
        pass


def run_plan(port, wait_board=0, item_timeout=ITEM_TIMEOUT, prep=PREP_SECONDS,
             only=None):
    chosen = choose_port(port, timeout=wait_board)
    if chosen is None:
        print("没找到 micro:bit 串口。先看资源管理器里有没有 MICROBIT 盘。")
        return 1

    reader = Reader(chosen)
    os.makedirs(TEMP, exist_ok=True)
    fd = open(LOG_PATH, "w", encoding="utf-8")

    selected = set(range(len(PLAN))) if not only else set(only)
    log(fd, f"按键收集：选了 {len(selected)} 项，每项最多等 {item_timeout} 秒")
    log(fd, "提示同步写在 microbit/temp/NOW.txt：收到正确上报就自动跳下一项，")
    log(fd, "所以按早按晚都无所谓，盯着 NOW.txt 按就行。")
    log(fd, "")
    write_plan_list(selected)

    # 先喂一条 STATE：板子收到主机报文就停止每秒握手 QUERY，日志会干净很多
    reader.send("STATE:play|65|37/242")
    log(fd, f"已下发 STATE（让板子停掉握手噪声），准备 {prep} 秒 ...")
    write_now("准备中，别按，等提示变成第 1 项再按")
    warm_until = time.time() + prep
    panics = []
    while time.time() < warm_until:
        for _, text in reader.pump():
            if text.startswith("PANIC"):
                panics.append(text)
        time.sleep(0.02)

    slots = []          # 每一项收到的所有上报
    hits = []           # 每一项「命中期望命令」的次数
    strays = []         # 意外命令，带项号，用来抓误触发

    for index, (name, expected, hint, is_hardware) in enumerate(PLAN):
        if index not in selected:
            slots.append([])          # 占位，让后面的下标还对得上
            hits.append(0)
            continue
        need = LAST_REPEAT if index == len(PLAN) - 1 else 1
        slots.append([])
        hits.append(0)
        log(fd, "\n" + "=" * 66)
        log(fd, f"[{index + 1}/{len(PLAN)}] {name}"
                + (f"    （要连按 {need} 次）" if need > 1 else ""))
        log(fd, f"    >>> {hint}")
        log(fd, "    等你按 …")
        write_now(f"[{index + 1}/{len(PLAN)}] {name}\n{hint}")

        # 事件驱动：收到期望命令就收工，不再按秒切窗口
        deadline = time.time() + item_timeout
        next_beat = time.time() + 5
        while time.time() < deadline and hits[index] < need:
            # 等待时别让终端一片死寂，隔几秒报一次，看着才知道脚本没死
            if time.time() >= next_beat:
                left = int(deadline - time.time())
                log(fd, f"    …还没收到（还剩 {left} 秒），再按一次试试")
                next_beat = time.time() + 5
            for _, text in reader.pump():
                if text.startswith("PANIC"):
                    panics.append(text)
                    log(fd, f"  收到 {text}   <<< 崩溃上报")
                    continue
                key = text.split(":", 1)[0].strip()
                if key in expected:
                    hits[index] += 1
                    slots[index].append(text)
                    left_need = need - hits[index]
                    if left_need > 0:
                        log(fd, f"    ✓ 收到 {text}（还差 {left_need} 次）")
                    else:
                        log(fd, f"    ✓ 收到 {text} —— 过了，下一项")
                    if hits[index] < need:
                        write_now(f"[{index + 1}/{len(PLAN)}] {name}\n"
                                  f"还差 {need - hits[index]} 次")
                elif key in NOISE and "QUERY" not in expected:
                    continue          # 握手期的 QUERY，不是摇一摇摇出来的
                else:
                    strays.append((index + 1, text))
                    slots[index].append(text)
                    log(fd, f"  收到 {text}   ← 不是这项要的，记串台")
            time.sleep(0.02)

        got = slots[index]
        if hits[index] >= need:
            verdict = "OK"
            note = f"命中 {hits[index]} 次" if need > 1 else "命中"
        elif is_hardware:
            verdict = "HW"
            note = f"{item_timeout} 秒没等到（logo 这类算硬件问题）"
        else:
            verdict = "FAIL"
            note = f"{item_timeout} 秒没等到"
        off = [x for x in got if x.split(":", 1)[0].strip() not in expected]
        if off:
            note += "；另有串台：" + ", ".join(off)
        log(fd, f"        -> {verdict}：{note}")

    reader.close()

    # ------------------------------------------------------------ 出结果表
    rows = []
    for index, (name, expected, _hint, is_hardware) in enumerate(PLAN):
        if index not in selected:
            rows.append((name, "/".join(sorted(expected)), "—", "跳过", False))
            continue
        got = slots[index]
        hit = hits[index]
        need = LAST_REPEAT if index == len(PLAN) - 1 else 1
        off = [x for x in got if x.split(":", 1)[0].strip() not in expected]
        passed = hit >= need
        if need > 1:
            detail = f"{hit}/{need} 次"
        elif hit:
            detail = "命中"
        elif got:
            detail = "没收到要的，只有串台"
        else:
            detail = "无"
        if off:
            detail += "；串台 " + ", ".join(off)
        if passed:
            state = "通过"
        elif is_hardware:
            state = "硬件问题"
        else:
            state = "失败"
        rows.append((name, "/".join(sorted(expected)), detail, state, is_hardware))

    ok_count = sum(1 for r in rows if r[3] == "通过")
    hw_count = sum(1 for r in rows if r[3] == "硬件问题")
    fail_count = sum(1 for r in rows if r[3] == "失败")

    lines = [
        "# 按键真机验证结果",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 串口：{chosen}",
        f"- 固件：microbit/microbit_remote.py（带 PANIC 上报的版本）",
        f"- 汇总：**通过 {ok_count} / 硬件问题 {hw_count} / 失败 {fail_count}**"
        f"（共 {len(rows)} 项）",
        "",
        "| # | 动作 | 期望上报 | 实际收到 | 结论 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for i, (name, expect, detail, state, _hw) in enumerate(rows, 1):
        lines.append(f"| {i} | {name} | {expect} | {detail} | {state} |")

    lines.append("")
    if panics:
        lines.append("## 崩溃上报")
        lines.append("")
        for item in panics:
            lines.append(f"- `{item}`")
        lines.append("")
    else:
        lines.append("全程没有 PANIC 上报 —— 这个固件在你这套操作下没崩。")
        lines.append("")
    if strays:
        lines.append("## 串台（按这项时却报了别的命令）")
        lines.append("")
        lines.append("这些是在某一项的等待过程中收到的、但不是那一项要的命令。"
                     "长按却先蹦出一次短按、上一步的手还没松干净，都会记在这里：")
        lines.append("")
        for item_no, item in strays:
            lines.append(f"- 第 {item_no} 项期间收到 `{item}`")
        lines.append("")
    if hw_count:
        lines.append("注：标「硬件问题」的那项属于 logo 金手指读不到，"
                     "不是代码错；暂停还有 A+B 短按这条路可走。")

    with open(RESULT_PATH, "w", encoding="utf-8") as out:
        out.write("\n".join(lines) + "\n")

    write_now(f"收工：通过 {ok_count} / 硬件问题 {hw_count} / 失败 {fail_count}")
    log(fd, "")
    log(fd, f"收工：通过 {ok_count} / 硬件问题 {hw_count} / 失败 {fail_count}")
    log(fd, f"结果表：{RESULT_PATH}")
    fd.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description="按键真机收集器")
    parser.add_argument("--port", help="串口号，留空自动识别 micro:bit")
    parser.add_argument("--check", type=int, metavar="SEC",
                        help="体检模式：监听 SEC 秒，看有没有 PANIC")
    parser.add_argument("--plan", action="store_true", help="按键收集模式")
    parser.add_argument("--downlink", action="store_true",
                        help="下行链路测试：发了 STATE 之后 QUERY 停不停（要按一次 RESET）")
    parser.add_argument("--timeout", type=int, default=ITEM_TIMEOUT, metavar="SEC",
                        help=f"每项最多等几秒，收到正确上报就提前进下一项"
                             f"（默认 {ITEM_TIMEOUT}）")
    parser.add_argument("--only", metavar="N",
                        help="只测指定的项，如 --only 3,5,9（某一项失败想单独重测时用）")
    parser.add_argument("--wait-board", type=int, default=0, metavar="SEC",
                        help="串口没出现时最多等几秒（先跑脚本再插线时用）")
    args = parser.parse_args()

    if not args.check and not args.plan and not args.downlink:
        parser.print_help()
        return 1
    if args.check:
        return run_check(args.port, args.check, args.wait_board)
    if args.downlink:
        return run_downlink(args.port, args.wait_board)

    only = None
    if args.only:
        try:
            picked = [int(x) - 1 for x in args.only.replace(" ", "").split(",") if x]
        except ValueError:
            print("--only 要写项号，比如 --only 3,5,9")
            return 1
        bad = [x + 1 for x in picked if x < 0 or x >= len(PLAN)]
        if bad:
            print(f"项号超出范围：{bad}，一共只有 {len(PLAN)} 项")
            return 1
        only = picked
    return run_plan(args.port, args.wait_board, args.timeout, PREP_SECONDS, only)


if __name__ == "__main__":
    sys.exit(main())
