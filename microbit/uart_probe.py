"""
最小串口探测固件 —— 板子完全静默时用它二分定位故障层。

为什么单独做一份：
    主固件是个几百行的大程序，它静默的时候你分不清是「程序没跑」
    还是「跑了但串口不通」。这个固件只干三件事，每层都能单独看结果。

怎么用：
    用官方编辑器（python.microbit.org）新建项目 → 把这份文件整份粘进去 →
    下载 hex → 拖到 MICROBIT 那个盘。不要用 hex_sync --regen（uflash 的
    beta runtime 在 V2.2 上本身就可能不通，会污染判断）。

刷完看三件事：
    1. 屏幕：勾（YES）/ 叉（NO）每 2 秒交替
       → 不闪 = 程序压根没跑，问题在刷写环节或板子硬件
       → 会闪 = 程序在跑，继续看第 2 条
    2. 电脑串口能不能收到 PING，收到的又是哪一个：
       「PING:uart N」  → uart.write() 这条路通（主固件用的就是这条）
       「PING:print N」 → print() 这条路通，但 uart 那条不通
                          （说明这台板的 uart 不走 USB，主固件得改）
       两个都收不到     → USB 数据通道本身有问题（线 / 接口 / 驱动）
    3. 电脑往板子发任意字符，屏幕会滚出收到的次数
       → 能滚出数字 = 板子收得到，双向都正常
"""

from microbit import *
import time

uart.init(baudrate=115200)

n = 0
received = 0

display.show(Image.YES)
sleep(500)

while True:
    n += 1

    try:
        uart.write("PING:uart %d\n" % n)
    except Exception:
        pass

    try:
        print("PING:print %d" % n)
    except Exception:
        pass

    try:
        while uart.any():
            uart.read()
            received += 1
    except Exception:
        pass

    if received:
        display.scroll("R%d" % received, delay=60)
        received = 0

    display.show(Image.YES)
    sleep(2000)
    display.show(Image.NO)
    sleep(2000)
