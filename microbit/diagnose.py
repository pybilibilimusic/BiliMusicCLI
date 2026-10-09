"""
micro:bit 链路一键诊断 —— 板子「不收消息 / 没反应」时先跑这个。

为什么单独做这么一个东西：
    板子没反应时，故障可能在五层中的任意一层：USB 没识别 / 串口被占 /
    固件压根没跑 / 掉进 REPL / 协议对不上。靠肉眼一条条试很慢，
    这个脚本按层递进测一遍，最后直接给结论。

用法：
    python microbit/diagnose.py                 # 自动找板子，全跑一遍
    python microbit/diagnose.py --port COM4     # 指定串口
    python microbit/diagnose.py --no-send       # 别往板子发东西（它在跑别的程序时）
    python microbit/diagnose.py --listen 15     # 监听时间拉长到 15 秒

五步分别看什么：
    [1] 板子在不在 USB 上、COM 号跟 config.ini 对不对得上
    [2] 上次刷写成功了吗 —— MICROBIT 盘上有 FAIL.TXT 就说明根本没刷进去，
        板子上跑的是半截坏固件。这一步要最先看：它骗起人来特别像
        「程序没跑」，会把人引到查波特率、怀疑硬件上去
    [3] 原样监听：板子会不会自己开口（注意主固件只在开机发一阵 QUERY，
        之后没按键就是静默，这是正常的 —— 按 RESET 最能抓到）
    [4] REPL 探测：发个空行看有没有 >>> 回显（有 = 固件没跑，板子在等命令）
    [5] 下发协议报文：让你看屏幕有没有反应（区分「没收到」和「收到了不显示」）

⚠️ 跑之前把 Mu / Thonny / 串口助手 / main_CUI 都关掉 —— 串口一次只能一个人占。
"""

import argparse
import os
import string
import sys
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("没装 pyserial：pip install pyserial")
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

VID_PID = "0D28:0204"          # micro:bit 的 USB 标识
BAUDRATE = 115200

LINE = "-" * 70


def find_board():
    """扫一遍串口，返回 micro:bit 的 COM 号（找不到返回 None）。"""
    hits = []
    for port in serial.tools.list_ports.comports():
        hwid = (port.hwid or "").upper()
        desc = (port.description or "").lower()
        if VID_PID.upper() in hwid or "microbit" in desc or "mbed" in desc:
            hits.append((port.device, port.description))
    return hits


def configured_port():
    """读 config.ini 里配的口，读不到返回 None。"""
    root = os.path.dirname(HERE)
    path = os.path.join(root, "config.ini")
    try:
        import configparser
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        return parser.get("microbit", "port", fallback=None)
    except Exception:
        return None


def looks_like_text(data):
    """一段字节像不像正常 ASCII 文本（用来判断是不是乱码）。"""
    if not data:
        return False
    ok = sum(1 for b in data if 32 <= b < 127 or b in (10, 13))
    return ok / len(data) > 0.85


def looks_like_usb_glitch(data):
    """
    判断这是不是「按 RESET 导致 USB 重新枚举」的毛刺，而不是固件发的真数据。

    2026-10-09 对照实验：什么键都不按、空跑 25 秒 -> 0 字节；一按 RESET 就立刻
    冒出十几个字节，形如 b'IIkI*kIkIILIk\\x15IIK*kILI\\x11M...'。它们没有 \\n、
    夹着 0x15 这类控制字符，既排不成以换行结尾的行，也拼不出任何协议字段。

    而板端真实报文恒以 \\n 结尾，所以「没有换行 + 夹了控制字符」就是噪声的指纹。
    顺带说：looks_like_text 只要求 85% 可打印，上面那串算下来是 87.5%，
    会被它误判成正常文本 —— 所以这道判断不能交给它。

    以前没做这层区分，看到毛刺就报「收到数据 / 乱码」，让人以为串口坏了；
    而脚本自己又建议按 RESET，等于亲手制造了它要报警的东西。
    """
    if not data or b"\n" in data:
        return False                      # 有换行 -> 像真的行结构数据
    if len(data) >= 64:
        return False                      # 真数据流不会这么短就停
    text = data.decode("utf-8", "replace").upper()
    return not any(cmd in text for cmd in _KNOWN_COMMANDS)


# 板端可能发出来的东西；真正的数据里至少会含一个
_KNOWN_COMMANDS = ("QUERY", "PREV", "NEXT", "VOLUP", "VOLDOWN",
                   "PAUSE", "STOP", "PANIC", "PING")


def find_microbit_drive():
    """找 MICROBIT 那个 U 盘的盘符（靠盘上的 MICROBIT.HTM 认）。"""
    for letter in string.ascii_uppercase:
        path = letter + ":\\"
        try:
            if os.path.exists(os.path.join(path, "MICROBIT.HTM")):
                return path
        except Exception:
            continue
    return None


def flash_result():
    """
    看 MICROBIT 盘上有没有 FAIL.TXT —— 有就说明上次刷写根本没成功。

    为什么单列这一步：DAPLink 刷写失败时不会在电脑上报错，只是悄悄在盘上
    留一个 FAIL.TXT。板子上跑的还是旧固件（甚至是只写进去半截的坏固件），
    表现是静默 / 串口乱码 / 屏幕哭脸 —— 全都像是「程序没跑」，很容易把人
    引到重刷固件、查波特率、怀疑硬件上去，其实根因在刷写那一步。
    """
    drive = find_microbit_drive()
    if not drive:
        return None, None, "没找到 MICROBIT 盘（板子不在 U 盘模式，或线没插好）"
    fail = os.path.join(drive, "FAIL.TXT")
    if os.path.exists(fail):
        try:
            with open(fail, "r", encoding="utf-8", errors="replace") as fh:
                return drive, fh.read().strip(), None
        except Exception as exc:
            return drive, "(读不到内容：%s)" % exc, None
    return drive, None, None


def listen(port, seconds):
    """原样监听若干秒，返回 (总字节, 样例行列表)。"""
    ser = serial.Serial(port, BAUDRATE, timeout=0.3)
    total = b""
    samples = []
    end = time.time() + seconds
    try:
        while time.time() < end:
            data = ser.readline()
            if data:
                total += data
                if len(samples) < 8:
                    samples.append(data)
    finally:
        ser.close()
    return total, samples


def probe_repl(port):
    """发个空行，看有没有 REPL 的 >>> 提示符。"""
    ser = serial.Serial(port, BAUDRATE, timeout=1)
    try:
        ser.reset_input_buffer()
        ser.write(b"\r\n")
        time.sleep(1.5)
        out = b""
        while True:
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                break
            out += chunk
            if len(out) > 4096:
                break
        return out
    finally:
        ser.close()


def send_probe(port):
    """用真正的协议下发几条报文，让人看屏幕。"""
    from microbit_bridge import MicrobitBridge

    bridge = MicrobitBridge(
        port=port,
        callbacks={
            "get_state": lambda: {"status": "play", "title": "HELLO",
                                  "volume": 50, "position": 0,
                                  "duration": 100},
            "log": lambda msg: print("      [bridge]", msg),
        },
    )
    if not bridge.start():
        return False
    try:
        bridge.send_status("play")
        time.sleep(0.4)
        bridge.send_title("HELLO")
        time.sleep(0.4)
        bridge.send_volume(50)
        time.sleep(0.4)
        bridge.send_time(0, 100)
    finally:
        bridge.stop()
    return True


def main():
    ap = argparse.ArgumentParser(description="micro:bit 链路诊断")
    ap.add_argument("--port", help="指定串口，不给就自动找")
    ap.add_argument("--listen", type=int, default=6, help="监听秒数，默认 6")
    ap.add_argument("--no-send", action="store_true",
                    help="跳过下发测试（板子在跑别的程序时用）")
    args = ap.parse_args()

    print("=" * 70)
    print("micro:bit 链路诊断")
    print("=" * 70)

    # ---------- [1] 找板子 ----------
    print("\n[1/5] 找板子")
    hits = find_board()
    if not hits:
        print("  ✗ 一个 micro:bit 都没扫到。")
        print("    → 换根 USB 线（很多线只能充电不能传数据）")
        print("    → 换个 USB 口，别接在集线器上")
        print("    → 擦一下金手指；板子背面红灯亮不亮？不亮就是没供上电")
        return 1

    port = args.port or hits[0][0]
    for device, desc in hits:
        mark = " ← 用这个" if device == port else ""
        print("  找到 %s  %s%s" % (device, desc, mark))

    conf = configured_port()
    if conf:
        same = (conf.strip().upper() == port.upper())
        print("  config.ini 里配的是 %s：%s" % (conf, "一致" if same else
                                              "★ 不一致！程序会连错口"))
        if not same:
            print("    → 改 config.ini 的 [microbit] port，或运行时 --port 指定")
    else:
        print("  config.ini 里没配 micro:bit 端口")

    # ---------- [2] 上次刷写成功了吗 ----------
    print("\n[2/5] 上次刷写成功了吗（看 MICROBIT 盘上有没有 FAIL.TXT）")
    drive, fail_text, drive_note = flash_result()
    flash_failed = bool(fail_text)
    if drive_note:
        print("  %s" % drive_note)
    elif flash_failed:
        print("  ★★★ %s 上有 FAIL.TXT —— 上次刷写失败了！" % drive)
        print("      板子上跑的还是旧固件，甚至可能是只写进去半截的坏固件。")
        print("      静默 / 串口乱码 / 屏幕哭脸都是这么来的 —— 不是代码也不是硬件。")
        print("      盘上写的失败原因：%s" % fail_text.replace("\n", " | "))
        print("      → 重新刷一次；刷完回来再看这个文件还在不在。")
    else:
        print("  %s 上没有 FAIL.TXT —— 上次刷写是成功的" % drive)

    # ---------- [3] 原样监听 ----------
    print("\n[3/5] 原样监听 %d 秒（板子会自己开口吗）" % args.listen)
    print("  ※ 主固件只在开机后发一阵 QUERY 握手，没人按键时它一句话不说，")
    print("    所以「板子已经跑了一会儿」时静默是正常的，不代表坏了。")
    print("    ★ 这几秒里请按一下板子的 A 或 B 键 —— 按键即可让板子开口，")
    print("      而且不像 RESET 那样引起 USB 重新枚举（重枚举会冒出一串垃圾字节，")
    print("      以前本脚本会把那串东西当成乱码报出来，白让人紧张半天）。")

    glitch = False
    try:
        total, samples = listen(port, args.listen)
    except Exception as exc:
        print("  ✗ 打不开 %s：%s" % (port, exc))
        print("    → 串口被别的程序占着了（Mu / Thonny / 串口助手 / main_CUI）")
        print("    → 把它们全关掉再来")
        return 1

    if not total:
        print("  ⚠ 一个字节都没有。")
        silent = True
    else:
        silent = False
        glitch = looks_like_usb_glitch(total)
        if glitch:
            print("  收到 %d 字节，但看着是 RESET 毛刺，不是固件数据：" % len(total))
            print("      %r" % total[:120])
            print("    → 按 RESET 会让 USB 重新枚举，主机常常采到一撮垃圾字节。")
            print("      实测：不按键空跑 25 秒是干净的 0 字节 —— 它不代表串口有问题。")
        else:
            clean = looks_like_text(total)
            print("  收到 %d 字节，像正常文本？%s"
                  % (len(total), "是" if clean else "否（乱码）"))
            for raw in samples:
                print("      %r" % raw[:120])
            if not clean:
                print("    → 乱码通常说明波特率不对或线路有干扰")

    # ---------- [3] REPL 探测 ----------
    print("\n[4/5] REPL 探测（发个空行，看有没有 >>> 回显）")
    try:
        out = probe_repl(port)
    except Exception as exc:
        print("  探测失败：%s" % exc)
        out = b""
    in_repl = b">>>" in out
    if in_repl:
        print("  ★ 板子回 >>> 了 —— 它在 MicroPython REPL 里等命令，")
        print("    说明我们的固件没在跑（崩了 / 被 Ctrl+C 打断 / 没刷进去）")
    elif out:
        print("  有回应但不是 >>> ：%r" % out[:120])
    else:
        print("  没回应 —— 不像 REPL")

    # ---------- [4] 下发测试 ----------
    sent = False
    if args.no_send:
        print("\n[5/5] 已跳过下发测试（--no-send）")
    else:
        print("\n[5/5] 下发 STATUS:play / TITLE:HELLO / VOL:50 / TIME:0/100")
        print("      ★ 现在请看板子屏幕：有没有出现 HELLO 或播放图标？")
        try:
            sent = send_probe(port)
        except Exception as exc:
            print("  下发失败：%s" % exc)

    # ---------- 结论 ----------
    print("\n" + "=" * 70)
    print("结论")
    print("=" * 70)
    if flash_failed:
        print("★ 根因就在这一步：上次刷写失败了，板子上的固件本身是坏的。")
        print("  串口静默 / 收到乱码 / 屏幕哭脸全是它引起的 ——")
        print("  别去查波特率，也别急着怀疑硬件，先把固件刷进去再说：")
    elif in_repl:
        print("固件没在跑（板子在 REPL）。重刷一次：")
    elif glitch:
        print("只收到 USB 毛刺，没抓到任何有效数据 —— 它本身不代表链路坏了。")
        print("按 RESET 会引起 USB 重新枚举（实测：空跑 25 秒 0 字节，一按就冒出")
        print("十几个杂字节）。判断链路真正的办法是按键，别用 RESET：")
        print("  python microbit/key_capture.py --plan       # 按提示依次按键")
        print("  python microbit/key_capture.py --downlink   # 测下行要不要连")
    elif silent and not in_repl:
        print("板子没开口，两种可能：")
        print("  1) 主固件的开机握手（QUERY）早发完了，没人按键时它本来就不说话（正常）")
        print("  2) 程序确实没跑起来 / 串口没初始化")
        print("先按一下板子的 A 或 B 键再跑一次：")
        print("抓得到 PREV / NEXT 就是第 1 种，链路没问题。抓不到再往下查：")
    elif not silent and looks_like_text(total):
        print("板子在正常发数据，链路是通的。")
        print("那问题多半在主程序侧（端口配错 / 没启动监听 / 串口被占）。")
        print("核对：")
    else:
        print("收到的是乱码 —— 波特率或线路有问题。")
        print("排查：")

    print(LINE)
    print("重刷固件两条路（推荐第一条，V2.2 上更稳）：")
    print("  1) 官方编辑器：打开 python.microbit.org → 新建 → 把")
    print("     microbit/microbit_remote.py 整份粘进去 → 下载 hex →")
    print("     拖到 MICROBIT 那个盘里")
    print("  2) uflash：  python microbit/hex_sync.py --regen")
    print("     然后把 microbit/microbit_remote.hex 拖到 MICROBIT 盘")
    print("     （注意 uflash 带的是 MicroPython 2.0.0-beta.5，V2.2 上偶尔抽风）")
    print(LINE)
    print("⚠️ 刷写这一步本身也会失败，而且电脑上一声不吭 —— 只在 MICROBIT 盘上")
    print("   留一个 FAIL.TXT。常见那条 'File sent out of order by PC' 就是")
    print("   文件块被写乱序了（磁盘缓存 / 杀毒软件实时扫描 / U 盘写入策略都会触发）。")
    print("   实测最稳的写法是 PowerShell 的 Copy-Item 或资源管理器拖放；")
    print("   刷完一定回来看一眼 FAIL.TXT 在不在 —— 它在，说明这一遍白刷了。")
    print(LINE)
    print("刷完先跑离线自检再上机：")
    print("     python microbit/check_remote.py       # 固件静态检查，不用板子")
    print("     python microbit/diagnose.py           # 再看一眼链路")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
