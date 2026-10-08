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

四步分别看什么：
    [1] 板子在不在 USB 上、COM 号跟 config.ini 对不对得上
    [2] 原样监听：板子会不会自己开口（一个字节都不发 = 程序没跑）
    [3] REPL 探测：发个空行看有没有 >>> 回显（有 = 固件没跑，板子在等命令）
    [4] 下发协议报文：让你看屏幕有没有反应（区分「没收到」和「收到了不显示」）

⚠️ 跑之前把 Mu / Thonny / 串口助手 / main_CUI 都关掉 —— 串口一次只能一个人占。
"""

import argparse
import os
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
    print("\n[1/4] 找板子")
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

    # ---------- [2] 原样监听 ----------
    print("\n[2/4] 原样监听 %d 秒（板子会自己开口吗）" % args.listen)
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
        clean = looks_like_text(total)
        print("  收到 %d 字节，像正常文本？%s" % (len(total), "是" if clean else "否（乱码）"))
        for raw in samples:
            print("      %r" % raw[:120])
        if not clean:
            print("    → 乱码通常说明波特率不对或线路有干扰")

    # ---------- [3] REPL 探测 ----------
    print("\n[3/4] REPL 探测（发个空行，看有没有 >>> 回显）")
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
        print("\n[4/4] 已跳过下发测试（--no-send）")
    else:
        print("\n[4/4] 下发 STATUS:play / TITLE:HELLO / VOL:50 / TIME:0/100")
        print("      ★ 现在请看板子屏幕：有没有出现 HELLO 或播放图标？")
        try:
            sent = send_probe(port)
        except Exception as exc:
            print("  下发失败：%s" % exc)

    # ---------- 结论 ----------
    print("\n" + "=" * 70)
    print("结论")
    print("=" * 70)
    if in_repl:
        print("固件没在跑（板子在 REPL）。重刷一次：")
    elif silent and not in_repl:
        print("板子完全静默，也不在 REPL —— 程序没跑或串口没初始化。")
        print("重刷一次固件；刷完还这样就是硬件接触问题：")
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
    print("刷完先跑离线自检再上机：")
    print("     python microbit/check_remote.py       # 固件静态检查，不用板子")
    print("     python microbit/diagnose.py           # 再看一眼链路")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
