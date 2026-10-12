"""BiliMusicCLI —— BLE 遥控 demo（电脑端，模拟一个播放器）

跟 microbit/makecode/remote_main.ts 配套。电脑这边假装正在放歌，
每隔 1 秒把 STATE 推给板子，板子屏幕上就会滚「歌名 已播/总长」；
你按板子上的键，它把指令发回来，这边照做 —— 音量、切歌、暂停都跟着变。

用法：
    .\\.venv\\Scripts\\python.exe microbit\\ble_demo.py
    Ctrl-C 退出

刷了固件之后、跑这个之前：
    1. 板子刷的是 remote_main.ts（MakeCode，开了 No Pairing Required）
    2. 先跑 microbit\\ble_test.py，五步全过再玩这个
"""

import argparse
import asyncio
import time

# ⚠️ micro:bit 的 UART 服务跟 Nordic 标准是反的，别照抄标准 NUS 的写法
UART_TO_BOARD = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"      # 电脑往这写
UART_FROM_BOARD = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"    # 板子从这发
CHUNK = 19          # 留 1 字节给 \n，正好塞进板子的 20 字节缓冲

SONGS = [
    "Zhou Jie Lun - Hua Hai",
    "Chen Yi Xun - Fu Kua",
    "G.E.M. - Guang Nian Zhi Wai",
    "Mayday - Wan Jia",
    "JJ Lin - Jiang Nan",
]

CMD_DESC = {
    "PREV": "上一首",
    "NEXT": "下一首",
    "VOLUP": "音量 +5",
    "VOLDOWN": "音量 -5",
    "PAUSE": "暂停/继续",
    "STOP": "停止",
    "QUERY": "问主机要状态",
}


class Demo:
    def __init__(self):
        self.idx = 0
        self.playing = True
        self.volume = 65
        self.pos = 12
        self.dur = 242
        self.client = None
        self.t0 = time.time()
        self.title_sent = ""

    # --- 发送 ---
    # ⚠️ 板子的 BLE 接收缓冲只有 20 字节，一行写进去会被切碎。
    #    所以拆成 19 字节的小片发，最后单独补一个 \n 告诉它「这行完了」，
    #    板端侧会把碎片拼起来再按 \n 分行。
    async def write(self, line):
        payload = (line + "\n").encode("ascii", "replace")
        for i in range(0, len(payload), CHUNK):
            await self.client.write_gatt_char(UART_TO_BOARD,
                                              payload[i:i + CHUNK],
                                              response=False)
        print("  +%6.1fs  -> %s" % (time.time() - self.t0, line))

    async def push_title(self):
        t = SONGS[self.idx]
        if t != self.title_sent:
            await self.write("TITLE:" + t)
            self.title_sent = t

    async def push_state(self):
        st = "play" if self.playing else "pause"
        await self.write("STATE:%s|%d|%d/%d" % (st, self.volume,
                                                self.pos, self.dur))

    # --- 收到指令 ---
    async def on_cmd(self, cmd):
        desc = CMD_DESC.get(cmd, "?")
        print("  +%6.1fs  <- %-8s  (%s)" % (time.time() - self.t0, cmd, desc))

        if cmd == "PREV":
            self.idx = (self.idx - 1) % len(SONGS)
            self.pos = 0
            await self.push_title()
        elif cmd == "NEXT":
            self.idx = (self.idx + 1) % len(SONGS)
            self.pos = 0
            await self.push_title()
        elif cmd == "VOLUP":
            self.volume = min(100, self.volume + 5)
        elif cmd == "VOLDOWN":
            self.volume = max(0, self.volume - 5)
        elif cmd == "PAUSE":
            self.playing = not self.playing
        elif cmd == "STOP":
            self.playing = False
            self.pos = 0

        await self.push_state()

    def make_handler(self):
        def _h(*args):
            data = args[-1]
            try:
                line = data.decode("utf-8", "replace").strip()
            except Exception:
                return
            if not line:
                return
            # 真崩了会把异常名甩出来
            if line.startswith("PANIC"):
                print("  *** 板子崩了：%s" % line)
                return
            asyncio.create_task(self.on_cmd(line))
        return _h


async def find_board(seconds):
    from bleak import BleakScanner
    print("扫描 %.0f 秒找 micro:bit ..." % seconds)
    try:
        res = await BleakScanner.discover(timeout=seconds, return_adv=True)
        pairs = list(res.values())
    except TypeError:
        devs = await BleakScanner.discover(timeout=seconds)
        pairs = [(d, None) for d in devs]
    for dev, _adv in pairs:
        nm = (getattr(dev, "name", "") or "").lower()
        if "micro:bit" in nm or "bbc" in nm:
            print("  找到 %s  %s" % (getattr(dev, "name", ""),
                                     getattr(dev, "address", "?")))
            return dev
    print("  没找到。先跑 microbit\\ble_test.py 看看。")
    print("  如果刚才强行中断过脚本（Ctrl-C 之外的强杀），板子可能还在")
    print("  「已连接」状态因而不再广播 —— 按一下板子背面的 RESET 再试。")
    return None


async def run(args):
    demo = Demo()
    dev = await find_board(args.scan_seconds)
    if dev is None:
        return 1

    from bleak import BleakClient
    print("连接中 ...")
    client = BleakClient(dev)
    await asyncio.wait_for(client.connect(), 15)
    demo.client = client
    demo.t0 = time.time()
    print("已连接。你在板子上按键试试：")
    print("  A短按=上一首  B短按=下一首  A长按=音量-  B长按=音量+")
    print("  A+B短按=暂停  A+B长按=停止  摸logo=暂停  摇一摇=要状态")
    print("-" * 62)

    await client.start_notify(UART_FROM_BOARD, demo.make_handler())
    await asyncio.sleep(0.5)
    await demo.push_title()
    await demo.push_state()

    n = 0
    try:
        while True:
            await asyncio.sleep(1)
            n += 1
            if demo.playing:
                demo.pos = (demo.pos + 1) % demo.dur
            if n % 5 == 0:
                # keepalive：主机固定 5 秒说句话，太久不说话板子会打 !
                await demo.push_state()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # 板子崩了/掉线时 write 会抛，这里把现象说清楚再收尾
        print()
        print("  *** 链路断了：%s: %s" % (type(exc).__name__, exc))
        print("      多半是板子崩了或 BLE 断了。看一下板子屏幕：")
        print("        - 哭脸 + 一串数字 = CODAL 崩溃码，把数字告诉我")
        print("        - 直接黑屏 = 板子重启了")

    try:
        await client.stop_notify(UART_FROM_BOARD)
    except Exception:
        pass
    await client.disconnect()
    print("-" * 62)
    print("结束")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-seconds", type=float, default=12.0)
    a = ap.parse_args()
    try:
        asyncio.run(run(a))
    except KeyboardInterrupt:
        print("\n退出")
    except Exception as exc:
        print("失败：%s: %s" % (type(exc).__name__, exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
