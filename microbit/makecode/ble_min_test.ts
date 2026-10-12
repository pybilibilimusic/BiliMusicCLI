// =====================================================================
// BiliMusicCLI —— micro:bit V2 BLE 最小测试固件（MakeCode / TypeScript）
// ---------------------------------------------------------------------
// 只有三件事，别的都没有。目的是用最小代价回答一个问题：
//     "这板子的 BLE 到底能不能跟这台电脑说话？"
// 答完了再谈移植那 542 行逻辑。
//
//   1. 连上 -> 屏幕显示勾；断开 -> 显示叉
//   2. 上行：连上后每秒发一条 PING:<n>
//   3. 下行：收到什么就原样回声 PONG:<原内容>
//      （这样电脑端能自动验证下行，不用盯着屏幕看）
//
// 刷入步骤（顺序不能变）：
//   1. https://makecode.microbit.org/  新建项目
//   2. Advanced -> Extensions -> 加 Bluetooth（会移除 Radio，选是）
//   3. 齿轮 -> Project Settings -> 打开 "No Pairing Required"   ★ 关键 ★
//   4. 切到 JavaScript，整份粘进去覆盖
//   5. 用 WebUSB 刷（别拖盘，这台机器的 MSD 刷写是坏的）
//
// 然后电脑端跑：
//   .\.venv\Scripts\python.exe microbit\ble_test.py
// =====================================================================

let connected = false
let n = 0

bluetooth.onBluetoothConnected(function () {
    connected = true
    basic.showIcon(IconNames.Yes)
})

bluetooth.onBluetoothDisconnected(function () {
    connected = false
    basic.showIcon(IconNames.No)
})

// 下行：原样回声。电脑收到 PONG:xxx 就说明下行通了
bluetooth.onUartDataReceived(serial.delimiters(Delimiters.NewLine), function () {
    const line = bluetooth.uartReadUntil(serial.delimiters(Delimiters.NewLine))
    bluetooth.uartWriteString("PONG:" + line + "\n")
    if (line.length <= 10) {
        basic.showString(line)      // 短的顺便滚一下，肉眼也能确认
    } else {
        basic.showIcon(IconNames.Heart)
    }
})

// 上行：连上后每秒一条
basic.forever(function () {
    if (connected) {
        n = n + 1
        bluetooth.uartWriteString("PING:" + n + "\n")
    }
    basic.pause(1000)
})

bluetooth.startUartService()
basic.showIcon(IconNames.No)
