// =====================================================================
// BiliMusicCLI —— micro:bit 遥控端（MakeCode / TypeScript）
//   从 microbit/microbit_remote.py 移植过来，按键映射和协议完全一致。
//
// 为什么要有这一份：板子的 USB 串口(CDC) 物理链路坏了（详见 TODO「搁置」一节），
//   而 BLE 射频在目标芯片上、不经过坏掉的接口芯片 —— 所以走蓝牙救活遥控。
//   代价是 MicroPython 不支持 BLE，固件必须换成 MakeCode。
//
// 按键映射（跟 Python 版一致）：
//   A 短按 PREV / A 长按(>600ms) VOLDOWN
//   B 短按 NEXT / B 长按(>600ms) VOLUP
//   A+B 短按 PAUSE / A+B 长按 STOP / 触摸 logo PAUSE / 摇一摇 QUERY
//
// 刷入：
//   1. https://makecode.microbit.org/  新建项目
//   2. Advanced -> Extensions -> 加 Bluetooth（会移除 Radio，选是）
//   3. 齿轮 -> Project Settings -> 打开 "No Pairing Required"   ★ 关键 ★
//   4. 切 JavaScript，整份覆盖粘贴
//   5. WebUSB 刷入（别拖盘，这台机器的 MSD 刷写是坏的）
//
// ⚠️ 两个移植要点：
//   1. MakeCode 的 basic.showString() 是阻塞的，三十几个字符能卡 28 秒，
//      期间按键和蓝牙全没人管 —— 所以这里自带 3x5 字模 + 手写非阻塞滚动。
//   2. 提示音必须 Background 模式且很短，前台播放会把 BLE 栈掐断。
// =====================================================================

// --- 可调参数（跟 Python 版对齐）
const LONG_PRESS_MS = 600
const COMBO_WINDOW_MS = 250
const SCROLL_STEP_MS = 150      // 每步滚一列
const SCREEN_PERIOD_MS = 6000   // 兜底：至少这么久才重滚一遍
const DIGIT_MS = 420            // 音量播报时每一位停留多久
const LOOP_MS = 50              // 主循环间隔
const TOUCH_DEBOUNCE_MS = 400
const HANDSHAKE_TIMEOUT = 15    // 开机后最多发这么多次 QUERY
const HOST_TIMEOUT_MS = 15000   // 连上过主机后，这么久没报文就告警
const BRIGHT = 110              // 点阵亮度（满 255；晚上看久了刺眼）

// --- 3x5 字模。每行用 3 位二进制表示（bit2=最左列），一个字形 = 5 行。
//     例如 "25755" = 111/101/111/101/101 -> A
const UPPER = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-./:?"
const CODES = [
    "00000", "25755", "65656", "34443", "65556", "74647", "74644", "34553",
    "55755", "72227", "11152", "55655", "44447", "57755", "56535", "25552",
    "75744", "25213", "75655", "34216", "72222", "55552", "55522", "55775",
    "55255", "55222", "71247",
    "75557", "26227", "71747", "71717", "55711", "74717", "74757", "71111",
    "75757", "75717",
    "00700", "00002", "11244", "02020", "71302"
]

// 小写跟大写共用字形，省一半数据。
// 手写查找而不用 indexOf / fromCharCode：MakeCode 的静态 TS 对这些方法
// 支持不全，写死的字符串最保险。
const LOWER = "abcdefghijklmnopqrstuvwxyz"
const GLYPH_CH = [" "]
const GLYPH_CD = ["00000"]
for (let _i = 1; _i < UPPER.length; _i++) {
    GLYPH_CH.push(UPPER.charAt(_i))
    GLYPH_CD.push(CODES[_i])
}
for (let _i = 0; _i < 26; _i++) {
    GLYPH_CH.push(LOWER.charAt(_i))
    GLYPH_CD.push(CODES[1 + _i])
}

// --- 图标：每行一个 5 位掩码（bit0 = 最左列）
const ROW_PAUSE = [10, 10, 10, 10, 10]          // 两条竖杠
const ROW_STOP = [17, 10, 4, 10, 17]            // ✗ 两条对角线
const ROW_NOTE = [28, 4, 4, 7, 7]               // 八分音符
const ROW_NOLINK = [4, 4, 4, 0, 4]              // ! 链路断了
const ROW_V = [10, 10, 10, 10, 4]               // 音量前缀 V

// --- 运行时状态
let status = "stop"
let title = ""
let volume = 0
let position = "?"
let duration = "?"

let connected = false
let hostSeen = false
let lastHostAt = 0
let linkAlarm = false         // 连上过但太久没报文 -> 打 !
let handshakeLeft = HANDSHAKE_TIMEOUT
let lastQueryAt = 0
let lastScrollAt = 0
let needRender = false

// 滚动状态
let scrollCols: number[] = []  // 一列一个 5 位掩码
let scrollOffset = 0
let scrolling = false
let scrollNextAt = 0

// 音量播报
let volFrames: number[][] = []
let volIndex = 0
let volUntil = 0
let volChanged = false

// 按键状态机
let aDown = false
let bDown = false
let aDownAt = 0
let bDownAt = 0
let aLongFired = false
let bLongFired = false
let pressQueue: string[] = []   // 待判定的键
let pressQueueAt: number[] = []
let comboAt = -1
let lastTouchAt = 0

// --- 显示
function drawRows(rows: number[]) {
    for (let y = 0; y < 5; y++) {
        for (let x = 0; x < 5; x++) {
            if (rows[y] & (1 << x)) {
                led.plotBrightness(x, y, BRIGHT)
            } else {
                led.plotBrightness(x, y, 0)
            }
        }
    }
}

function clearAll() {
    for (let y = 0; y < 5; y++) {
        for (let x = 0; x < 5; x++) {
            led.plotBrightness(x, y, 0)
        }
    }
}

// 把一个字形按 3 列宽居中，返回 5 行掩码
function glyphRows(code: string): number[] {
    let rows: number[] = []
    for (let y = 0; y < 5; y++) {
        let v = parseInt(code.charAt(y))
        let m = 0
        if ((v & 4) !== 0) { m |= 2 }    // 左 -> x=1
        if ((v & 2) !== 0) { m |= 4 }    // 中 -> x=2
        if ((v & 1) !== 0) { m |= 8 }    // 右 -> x=3
        rows.push(m)
    }
    return rows
}

// MakeCode 的 indexOf / parseInt / isNaN 不那么可靠，这里都手写
function strIndexOf(s: string, ch: string): number {
    for (let i = 0; i < s.length; i++) {
        if (s.charAt(i) == ch) { return i }
    }
    return -1
}

function arrIndexOf(arr: string[], v: string): number {
    for (let i = 0; i < arr.length; i++) {
        if (arr[i] == v) { return i }
    }
    return -1
}

function glyphRowsOf(ch: string): number[] {
    let idx = arrIndexOf(GLYPH_CH, ch)
    if (idx < 0) { idx = arrIndexOf(GLYPH_CH, "?") }
    return glyphRows(GLYPH_CD[idx])
}

// 十进制解析。非法字符返回 -1，省掉 isNaN
function toNumber(s: string): number {
    if (s.length == 0) { return -1 }
    let v = 0
    for (let i = 0; i < s.length; i++) {
        let c = s.charAt(i)
        if (c < "0" || c > "9") { return -1 }
        v = v * 10 + (strIndexOf("0123456789", c))
    }
    return v
}

// --- 上行 / 下行
function send(line: string) {
    if (!connected) { return }
    bluetooth.uartWriteString(line + "\n")
}

function beep(up: boolean) {
    // 必须 Background，否则会把 BLE 栈掐断
    if (up) {
        music.play(music.tonePlayable(Note.C5, 60), music.PlaybackMode.InBackground)
    } else {
        music.play(music.tonePlayable(Note.C4, 60), music.PlaybackMode.InBackground)
    }
}

// --- 滚动串
function screenText(): string {
    let t = ""
    if (title.length > 0) { t = title }
    if (position != "?" || duration != "?") {
        if (t.length > 0) { t = t + "  " }
        t = t + position + "/" + duration
    }
    return t
}

function startScroll(text: string) {
    scrollCols = []
    for (let i = 0; i < text.length; i++) {
        let rows = glyphRowsOf(text.charAt(i))
        for (let x = 1; x <= 3; x++) {
            let col = 0
            for (let y = 0; y < 5; y++) {
                if (rows[y] & (1 << x)) { col |= (1 << y) }
            }
            scrollCols.push(col)
        }
        scrollCols.push(0)          // 字符间隔
    }
    scrollOffset = 0
    scrollNextAt = 0
    scrolling = scrollCols.length > 5
    if (!scrolling) {
        // 短到不用滚：直接静态显示前 5 列（多半是纯数字时间）
        let w: number[] = []
        for (let x = 0; x < 5; x++) {
            w.push(scrollCols[x])
        }
        // 转成行掩码画出来
        let rows: number[] = [0, 0, 0, 0, 0]
        for (let y = 0; y < 5; y++) {
            let m = 0
            for (let x = 0; x < 5; x++) {
                if (w[x] & (1 << y)) { m |= (1 << x) }
            }
            rows[y] = m
        }
        drawRows(rows)
    }
}

function stepScroll(now: number) {
    if (!scrolling) { return }
    if (now < scrollNextAt) {
        // 还没到时间：先把当前窗口画住（防止被别的写入清掉）
        return
    }
    if (scrollOffset > scrollCols.length - 5) {
        // 滚完了，等重试周期到再重来
        scrolling = false
        lastScrollAt = now
        return
    }
    let rows: number[] = [0, 0, 0, 0, 0]
    for (let y = 0; y < 5; y++) {
        let m = 0
        for (let x = 0; x < 5; x++) {
            let c = scrollCols[scrollOffset + x]
            if (c & (1 << y)) { m |= (1 << x) }
        }
        rows[y] = m
    }
    drawRows(rows)
    scrollOffset = scrollOffset + 1
    scrollNextAt = now + SCROLL_STEP_MS
}

function screenPeriod(): number {
    // 标题长的时候滚完要半分钟，写死 6 秒会一直从头开始（看着像抽风）
    let n = scrollCols.length * SCROLL_STEP_MS
    let p = SCREEN_PERIOD_MS
    if (n + 1500 > p) { p = n + 1500 }
    return p
}

// --- 画面
function render() {
    lastScrollAt = input.runningTime()
    volFrames = []
    if (linkAlarm) {
        scrolling = false
        drawRows(ROW_NOLINK)
        return
    }
    if (status == "pause") {
        scrolling = false
        drawRows(ROW_PAUSE)
        return
    }
    if (status == "stop" && title.length == 0) {
        scrolling = false
        drawRows(ROW_STOP)
        return
    }
    if (title.length > 0) {
        startScroll(screenText())
        return
    }
    if (status == "play") {
        scrolling = false
        drawRows(ROW_NOTE)
        return
    }
    scrolling = false
    drawRows(ROW_STOP)
}

function startVolumeAnnounce() {
    volFrames = [ROW_V]
    let s = "" + volume
    for (let i = 0; i < s.length; i++) {
        volFrames.push(glyphRowsOf(s.charAt(i)))
    }
    volIndex = 0
    volUntil = 0
    scrolling = false
}

function pumpScreen(now: number) {
    if (volFrames.length > 0) {
        if (now >= volUntil) {
            if (volIndex >= volFrames.length) {
                volFrames = []
                volIndex = 0
                volUntil = 0
                render()
            } else {
                drawRows(volFrames[volIndex])
                volIndex = volIndex + 1
                volUntil = now + DIGIT_MS
            }
        }
        return
    }
    if (scrolling) {
        stepScroll(now)
        return
    }
    if (title.length > 0 && now - lastScrollAt >= screenPeriod()) {
        render()
    }
}

// --- 下行报文
const HOST_KEYS = ["STATUS", "VOL", "TITLE", "TIME", "STATE"]

function parseLine(line: string) {
    let key = line
    let value = ""
    let c = strIndexOf(line, ":")
    if (c >= 0) {
        key = line.substr(0, c)
        value = line.substr(c + 1)
    }
    value = trimStr(value)

    if (arrIndexOf(HOST_KEYS, key) >= 0) {
        hostSeen = true
        lastHostAt = input.runningTime()
        linkAlarm = false
    }

    if (key == "STATUS") {
        if (value == status) { return false }   // keepalive 别当变化，会掐回头
        status = value
        return true
    }
    if (key == "VOL") {
        let v = toNumber(value)
        if (v < 0) { return false }
        if (v == volume) { return false }
        volume = v
        volChanged = true
        return true
    }
    if (key == "TITLE") {
        if (value == title) { return false }
        title = value
        return true
    }
    if (key == "TIME") {
        let s = strIndexOf(value, "/")
        if (s >= 0) {
            position = value.substr(0, s)
            duration = value.substr(s + 1)
        }
        return false
    }
    if (key == "STATE") {
        // play|65|37/242
        let changed = false
        let f0 = value, f1 = "", f2 = ""
        let p1 = strIndexOf(value, "|")
        if (p1 >= 0) {
            f0 = value.substr(0, p1)
            let rest = value.substr(p1 + 1)
            let p2 = strIndexOf(rest, "|")
            if (p2 >= 0) {
                f1 = rest.substr(0, p2)
                f2 = rest.substr(p2 + 1)
            } else {
                f1 = rest
            }
        }
        if (f0.length > 0 && f0 != status) { status = f0; changed = true }
        if (f1.length > 0) {
            let v = toNumber(f1)
            if (v >= 0 && v != volume) { volume = v; volChanged = true; changed = true }
        }
        let s2 = strIndexOf(f2, "/")
        if (s2 >= 0) {
            position = f2.substr(0, s2)
            duration = f2.substr(s2 + 1)
        }
        return changed
    }
    return false
}

function trimStr(s: string): string {
    let a = 0
    let b = s.length
    while (a < b && s.charAt(a) == " ") { a = a + 1 }
    while (b > a && s.charAt(b - 1) == " ") { b = b - 1 }
    return s.substr(a, b - a)
}

// --- 按键
function pollButtons(): string {
    let now = input.runningTime()
    // MakeCode 没有 input.buttonA 对象，轮询用 buttonIsPressed(Button.A)
    let a = input.buttonIsPressed(Button.A)
    let b = input.buttonIsPressed(Button.B)

    if (a && !aDown) {
        aDown = true
        aDownAt = now
        aLongFired = false
        pressQueue.push("a")
        pressQueueAt.push(now)
    }
    if (b && !bDown) {
        bDown = true
        bDownAt = now
        bLongFired = false
        pressQueue.push("b")
        pressQueueAt.push(now)
    }
    if (!a) { aDown = false }
    if (!b) { bDown = false }

    // 组合键：两键都按着才算，判定必须排在长按之前，
    // 否则按住 A+B 会先触发一次音量键
    if (comboAt < 0 && aDown && bDown) {
        let span = aDownAt - bDownAt
        if (span <= COMBO_WINDOW_MS && -span <= COMBO_WINDOW_MS) {
            comboAt = aDownAt
            if (bDownAt < comboAt) { comboAt = bDownAt }
            pressQueue = []
            pressQueueAt = []
        }
    }
    if (comboAt >= 0) {
        aLongFired = true
        bLongFired = true
        if (!aDown && !bDown) {
            let held = now - comboAt
            comboAt = -1
            pressQueue = []
            pressQueueAt = []
            if (held >= LONG_PRESS_MS) { return "STOP" }
            return "PAUSE"
        }
        return ""
    }

    if (a && aDown && !aLongFired && now - aDownAt >= LONG_PRESS_MS) {
        aLongFired = true
        removeFromQueue("a")
        return "VOLDOWN"
    }
    if (b && bDown && !bLongFired && now - bDownAt >= LONG_PRESS_MS) {
        bLongFired = true
        removeFromQueue("b")
        return "VOLUP"
    }

    if (pressQueue.length > 0) {
        let k = pressQueue[0]
        let at = pressQueueAt[0]
        let stillDown = (k == "a") ? aDown : bDown
        if (stillDown) { return "" }        // 还按着，留给长按/组合
        if (!aDown && !bDown || now - at > COMBO_WINDOW_MS) {
            pressQueue.removeAt(0)
            pressQueueAt.removeAt(0)
            if (k == "a") { return "PREV" }
            return "NEXT"
        }
    }
    return ""
}

function removeFromQueue(k: string) {
    let i = arrIndexOf(pressQueue, k)
    if (i >= 0) {
        pressQueue.removeAt(i)
        pressQueueAt.removeAt(i)
    }
}

// --- 主循环
function tick() {
    let now = input.runningTime()

    let cmd = pollButtons()
    if (cmd.length > 0) {
        send(cmd)
        if (cmd == "PAUSE" || cmd == "NEXT" || cmd == "PREV") {
            beep(true)
        } else {
            beep(false)
        }
    }

    if (volChanged) {
        volChanged = false
        startVolumeAnnounce()
    } else if (needRender) {
        needRender = false
        render()
    }

    pumpScreen(now)

    // 连上过之后太久没报文 = 链路悄悄断了（跟串口版的判据一样）
    if (hostSeen && !linkAlarm && now - lastHostAt >= HOST_TIMEOUT_MS) {
        linkAlarm = true
        render()
    }

    // 开机握手：主机程序可能还没起来，多发几次 QUERY
    if (!hostSeen && handshakeLeft > 0 && connected) {
        if (now - lastQueryAt >= 1000) {
            lastQueryAt = now
            handshakeLeft = handshakeLeft - 1
            send("QUERY")
        }
    }
}

// --- 蓝牙
bluetooth.onBluetoothConnected(function () {
    connected = true
    linkAlarm = false
    hostSeen = false
    handshakeLeft = HANDSHAKE_TIMEOUT
    lastQueryAt = 0
    render()
})

bluetooth.onBluetoothDisconnected(function () {
    connected = false
    // 断开时把状态清掉，重连后重新握手、重新要歌名
    status = "stop"
    title = ""
    position = "?"
    duration = "?"
    linkAlarm = false
    render()
})

// ⚠️ micro:bit 的 BLE 接收缓冲只有 20 字节，满了就会先交付一次，
//    长报文会被切成好几段到达 —— 所以必须自己拼起来再按 \n 分行，
//    不能假设「一个事件 = 一行」。
let rxBuf = ""

bluetooth.onUartDataReceived(serial.delimiters(Delimiters.NewLine), function () {
    let chunk = bluetooth.uartReadUntil(serial.delimiters(Delimiters.NewLine))
    rxBuf = rxBuf + chunk

    let guard = 0
    let nl = strIndexOf(rxBuf, "\n")
    while (nl >= 0 && guard < 20) {
        let line = rxBuf.substr(0, nl)
        rxBuf = rxBuf.substr(nl + 1)
        if (line.length > 0 && parseLine(line)) {
            needRender = true
        }
        nl = strIndexOf(rxBuf, "\n")
        guard = guard + 1
    }
    // 攒了两百字节还没一个换行 -> 多半是乱了，丢掉重来，别无限涨
    if (rxBuf.length > 200) { rxBuf = "" }
})

// --- 手势
input.onGesture(Gesture.Shake, function () {
    send("QUERY")
})

input.onLogoEvent(TouchButtonEvent.Pressed, function () {
    let now = input.runningTime()
    if (now - lastTouchAt < TOUCH_DEBOUNCE_MS) { return }
    lastTouchAt = now
    send("PAUSE")
    beep(true)
})

// --- 开机
basic.forever(function () {
    tick()
    basic.pause(LOOP_MS)
})

bluetooth.startUartService()
drawRows(ROW_STOP)
basic.showString("BLE")
render()
