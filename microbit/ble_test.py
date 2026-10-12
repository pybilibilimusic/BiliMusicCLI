"""BiliMusicCLI —— micro:bit BLE 通路测试（电脑端，bleak）。

跟 microbit/makecode/ble_min_test.ts 配套。分五步走，每一步都会给出明确结论，
挂在哪一步就照那一步的提示查，不用瞎猜：

    [1/5] 环境      bleak 装没装、蓝牙服务开没开
    [2/5] 扫描      板子有没有在广播
    [3/5] 连接      电脑能不能连上（Windows 配对是这一步的常见坑）
    [4/5] 上行      板子每秒的 PING 能不能收到
    [5/5] 回环      发一条下去，看板子有没有回 PONG（同时验下行）

用法：
    .\\.venv\\Scripts\\python.exe microbit\\ble_bridge_test.py

    # 已知板子地址，跳过扫描（扫描偶发扫不到时用它）
    .\\.venv\\Scripts\\python.exe microbit\\ble_bridge_test.py --addr XX:XX:XX:XX:XX:XX

判据：五步全过 = BLE 这条路通了，可以谈把主固件移植成 MakeCode。
      [4/5] 过但 [5/5] 不过 = 只有单向通，也比现在强，但仍要查。
"""

import argparse
import asyncio
import sys
import time

UART_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"

# ⚠️ 这里有个大坑：micro:bit 的 UART 服务虽然用的是 Nordic UART 的 UUID，
#    但收发方向跟标准是**反的**，而且用 indicate 而不是 notify。
#    Espruino 官方文档原话："The micro:bit's UART isn't actually a standard
#    Nordic UART"，所以不能拿标准 NUS 的用法套。
#    实测学到的服务属性：
#      6e400003  [write, write-without-response]   <- 电脑往这写（真·RX）
#      6e400002  [indicate]                        <- 板子从这发（真·TX）
#    搞反了会报 characteristic does not support notifications。
UART_TO_BOARD = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"     # 主机 -> 板子
UART_FROM_BOARD = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # 板子 -> 主机
CHUNK = 20          # 默认 ATT MTU 23，扣头剩 20

OK = "[OK]  "
NG = "[!!]  "
results = []


def head(n, text):
    print()
    print("[%d/5] %s" % (n, text))
    print("-" * 60)


def verdict(n, ok, text, hint=""):
    results.append((n, ok, text))
    print("%s%s" % (OK if ok else NG, text))
    if hint:
        for line in hint.strip().splitlines():
            print("       %s" % line)
    return ok


class Board:
    def __init__(self):
        self.q = asyncio.Queue()
        self.client = None

    def on_notify(self, *args):
        """bleak 各版本回调签名不同，最后一个参数一定是 bytes。"""
        data = args[-1]
        try:
            text = data.decode("utf-8", "replace").strip()
        except Exception:
            text = repr(data)
        self.q.put_nowait(text)

    async def wait_for(self, prefix, timeout):
        """等到一条以 prefix 开头的报文，超时返回 None。"""
        end = time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                return None
            try:
                line = await asyncio.wait_for(self.q.get(), left)
            except asyncio.TimeoutError:
                return None
            if line.startswith(prefix):
                return line

    async def send(self, line):
        payload = (line + "\n").encode("utf-8")
        for i in range(0, len(payload), CHUNK):
            await self.client.write_gatt_char(UART_TO_BOARD, payload[i:i + CHUNK],
                                              response=False)


async def step1_env():
    head(1, "环境检查")
    try:
        import bleak
        print("       bleak 已装")
    except ImportError:
        return verdict(1, False, "bleak 没装",
                       "装一下：.\\.venv\\Scripts\\pip.exe install bleak")
    try:
        import importlib.metadata as md
        print("       版本 %s" % md.version("bleak"))
    except Exception:
        pass
    return verdict(1, True, "环境 OK")


async def step2_scan(seconds, addr):
    head(2, "扫描板子（%.0f 秒）" % seconds)
    if addr:
        return addr
    from bleak import BleakScanner
    try:
        res = await BleakScanner.discover(timeout=seconds, return_adv=True)
        pairs = list(res.values())
    except TypeError:
        devs = await BleakScanner.discover(timeout=seconds)
        pairs = [(d, None) for d in devs]

    hits = []
    for dev, adv in pairs:
        nm = (getattr(dev, "name", "") or "")
        if "micro:bit" in nm.lower() or "bbc" in nm.lower():
            hits.append(dev)

    if hits:
        for d in hits:
            print("       %s  %s" % (getattr(d, "name", ""), d.address))
        return hits[0]

    print("       扫到的其他 BLE 设备（说明电脑蓝牙本身是好的）：")
    for dev, _adv in pairs[:8]:
        print("         %-24s %s" % (getattr(dev, "name", "") or "(无名)",
                                     getattr(dev, "address", "?")))
    verdict(2, False, "没扫到 micro:bit", """
板子没在广播。按可能性排序：
1. 板子上还是 MicroPython 固件 —— MicroPython 不带 BLE，
   必须先刷 MakeCode 固件（刷了就自动把 BLE 找回来）
2. MakeCode 里没加 Bluetooth 扩展，或忘了开 "No Pairing Required"
3. 板子没通电 / 屏幕没亮
4. 用 --addr 直接指定地址重试（扫描偶发扫不到）""")
    return None


async def step3_connect(board, dev, retries=3):
    head(3, "连接（最多试 %d 次）" % retries)
    from bleak import BleakClient
    client = None
    for i in range(1, retries + 1):
        print("       第 %d/%d 次连 %s ..." % (i, retries,
                                               getattr(dev, "address", dev)))
        try:
            client = BleakClient(dev)
            await asyncio.wait_for(client.connect(), 15)
            break
        except Exception as exc:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                client = None
            print("       失败：%s: %s" % (type(exc).__name__, exc))
            if i < retries:
                await asyncio.sleep(4)

    if client is None:
        verdict(3, False, "%d 次都没连上" % retries, """
板子屏幕这会儿应该是"勾"（说明它认为连上了），如果还是叉，是板子侧没接受。
按命中率排序的排查梯子：
1. **关开一次蓝牙**（任务栏蓝牙图标 关 -> 等 5 秒 -> 开）再跑。
   Windows 会缓存"未配对设备"的服务表，你之前用别的配对模式刷过几次，
   缓存里的旧表和新固件对不上，就会连上又被拆掉 —— 关开蓝牙即清掉。
2. 还不行就把 MakeCode 换成 **JustWorks 配对**：
   Project Settings 里关掉"无需配对"、打开"JustWorks配对(默认)"，重新刷；
   然后 Windows 设置 -> 蓝牙和其他设备 -> 添加设备 -> 选 BBC micro:bit
   （JustWorks 不会弹 6 位码，直接就配上了）；配对完再跑本脚本。
   Windows 对"已配对设备"反而更稳，配一次以后都自动连。
3. 蓝牙设置里若有 BBC micro:bit 条目，先"移除设备"再试。""")
        return None

    board.client = client
    print("       已连接")
    await asyncio.sleep(0.5)     # 个别栈要缓一下才能稳定订阅
    try:
        await client.start_notify(UART_FROM_BOARD, board.on_notify)
    except Exception as exc:
        verdict(3, False, "连接上了但订阅通知失败：%s: %s"
                % (type(exc).__name__, exc), "")
        dump_services(client)
        print("""
判读上面的服务表：
- 如果根本没有 6e400001...（Nordic UART）-> 固件里没开 UART 服务，
  或 MakeCode 没加 Bluetooth 扩展 -> 重刷确认 startUartService() 在。
- 如果有 UART、但 6e400003 的属性里没有 notify -> 这是 Windows 的
  固件服务表缓存错位（按地址缓存，换固件后句柄对不上）。
  解法：任务栏把蓝牙 关掉 -> 等 5 秒 -> 再开，然后重跑本脚本。
  （系统设置里若有 BBC micro:bit 条目也顺手"移除设备"）""")
        return None
    return client


def dump_services(client):
    print()
    print("       --- Windows 眼里这块板子的服务表 ---")
    try:
        for svc in client.services:
            print("       服务 %s" % svc.uuid)
            for ch in svc.characteristics:
                props = ",".join(sorted(ch.properties))
                print("         %s  [%s]" % (ch.uuid, props))
    except Exception as exc:
        print("       读服务表失败：%s: %s" % (type(exc).__name__, exc))
    print("       ------------------------------------")


async def step4_uplink(board):
    head(4, "上行：等板子的 PING（最多 8 秒）")
    first = await board.wait_for("PING", 8.0)
    if first is None:
        verdict(4, False, "8 秒内没收到 PING", """
连上了但板子不说话。查：
1. 板子屏幕是不是"勾"？是叉说明它自己觉得没连上
2. 固件里 basic.forever 那段有没有真的刷进去（重刷一次看屏幕有没有先出叉）
3. 通知订阅没生效 —— 重跑一次这个脚本
注：测试固件是"连上后才发 PING"的，没连上本来就什么都不发。""")
        return False
    print("       %s" % first)
    # 再收一条，确认是连续的而不是偶发一发
    second = await board.wait_for("PING", 3.0)
    if second:
        print("       %s" % second)
    return verdict(4, True, "上行通了（收到 %s）" %
                   ("连续多条" if second else "1 条"))


async def step5_loopback(board):
    head(5, "回环：发一条下去，看板子回不回 PONG（最多 6 秒）")
    await board.send("HELLO")
    print("       已发 HELLO")
    got = await board.wait_for("PONG", 6.0)
    if got is None:
        verdict(5, False, "没有收到 PONG", """
下行（电脑 -> 板子）不通。查：
1. 板子屏幕有没有变化（收到短字符串会滚字、长的会出爱心）
   —— 屏幕动了 = 板子收到了，只是回声没发回来，那是上行订阅的问题
   —— 屏幕没动 = 真没收到，检查 RX 特征 UUID 和 20 字节切段
2. bleak 写特征要在连接建立后一小会儿才稳，重跑一次试试""")
        return False
    print("       %s" % got)
    return verdict(5, True, "下行通了（%s）" % got)


async def main(args):
    board = Board()
    if not await step1_env():
        return 1

    dev = await step2_scan(args.scan_seconds, args.addr)
    if dev is None:
        return 1
    if isinstance(dev, str):
        from bleak import BleakScanner
        dev = await BleakScanner.find_device_by_address(dev, timeout=15)
        if dev is None:
            verdict(2, False, "按地址也没找到")
            return 1
    verdict(2, True, "扫到板子")

    client = await step3_connect(board, dev, args.retries)
    if client is None:
        return 1
    verdict(3, True, "连接成功")

    up = await step4_uplink(board)
    down = await step5_loopback(board) if up else False

    try:
        await client.stop_notify(UART_FROM_BOARD)
    except Exception:
        pass
    try:
        await client.disconnect()
    except Exception:
        pass

    print()
    print("=" * 60)
    print("结论")
    print("=" * 60)
    for n, ok, text in results:
        print("  %s[%d/5] %s" % (OK if ok else NG, n, text))
    print()
    if up and down:
        print("  >>> BLE 双向都通了。可以谈把主固件移植成 MakeCode。")
    elif up:
        print("  >>> 只有上行通。能用一半，但还不值得移植，先查下行。")
    else:
        print("  >>> 上行不通。这条路目前走不通，退回 CH340(P0/P1) 保底方案。")
    return 0


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-seconds", type=float, default=12.0)
    ap.add_argument("--addr", default="", help="已知地址，跳过扫描")
    ap.add_argument("--retries", type=int, default=3, help="连接重试次数")
    args = ap.parse_args()
    try:
        return asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\n中断")
        return 0


if __name__ == "__main__":
    sys.exit(run())
