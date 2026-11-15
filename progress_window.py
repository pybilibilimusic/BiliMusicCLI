"""
进度条独立终端窗口 —— 验证层（**尚未接入 main_CUI.py**）。

按 PITFALLS A9：新功能先交纯逻辑模块 + *_verify.py（带 --demo），
等用户玩过、提完想法再整合进主程序。本文件不修改、不导入主程序。

## 为什么要有独立窗口

主程序的 `ProgressBar` / `DownloadBar` 都是后台线程直接往 `sys.stdout`
原地 `\r` 刷新，跟 `input()` 提示符抢同一行、抢同一个 stdout 锁。
把渲染搬进**另一个进程 + 另一个控制台**，主界面就只剩干净的提示符。

## 通信 / 同步方案（UDP loopback + JSON datagram）

    主进程 main_CUI                 窗口进程（新控制台）
      ProgressWindow.publish(msg)  --UDP-->  _WindowServer.run()
      （只发不收，无连接）                   （bind 端口，settimeout 非阻塞收）

- **单向 UDP + 127.0.0.1**：不加锁、不落地文件、窗口进程退出后不留垃圾；
  主程序完全不等待窗口，窗口卡了/没了也不影响播放。
- **无强同步**：进度是幂等的状态快照，丢一包只是跳一帧，下一帧就会补上。
  窗口端始终画「最近一次收到的」消息，所以消息乱序也不会倒退显示。
- **端口**：主进程用临时 socket 探测一个空闲 UDP 端口，通过命令行参数传给
  窗口进程，`start()` 后等 0.3s 让窗口 bind 完成（有 TOCTOU，失败可重试）。
- **退出**：主进程发 `bye` 消息；窗口进程自己 Ctrl+C 也会收尾擦行。

## 预留的歌词扩展位

消息协议带 `type` 字段，已注册 `lyric` 类型并有 `LyricPane` 分派实现，
但 `render_message()` 默认 `include_lyric=False` —— 窗口当前只画一行进度。
想加歌词时：把 `include_lyric=True`，`LyricPane.render()` 返回文本，
`_WindowServer._draw()` 用「上移一行 + 擦两行」即可，不用改协议和主程序。
多行渲染必须自己搬光标，别再靠 `\r`（F9：\r 只回最后一行行首）。

## F 节约束怎么落地的

- 三档策略（命令关 / 语音开 / 连 micro:bit 关）→ `ProgressPolicy.enabled()`
- 宽度计算 east_asian_width、条→剩余时间→大小→速度裁剪、ASCII `||`
  → **直接复用** `progress_bar.render_bar()` / `progress_download.render_download()`
  和 `progress_bar.visible_width()`，本模块一行渲染逻辑都没重写。
- 暂停标记 `||`：只在 `paused` 时由 `render_bar` 加，彩色/不上色两条分支都有。
- `\r` 折行：`paint()` 按 `max_width`（终端宽度 - 1）裁剪后再写，绝不让一行超宽。
- 交互用 `input()`，本模块不碰 `msvcrt`。

## 独立运行

    python progress_window.py --window --port 47621     # 作为窗口进程被拉起
    python progress_window_verify.py --demo             # 开真窗口看效果
    python progress_window_verify.py --demo --no-window # 在当前终端逐帧打印
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

import progress_bar
import progress_download

DEFAULT_HOST = "127.0.0.1"

MSG_PROGRESS = "progress"
MSG_DOWNLOAD = "download"
MSG_LYRIC = "lyric"
MSG_BYE = "bye"
MESSAGE_TYPES = (MSG_PROGRESS, MSG_DOWNLOAD, MSG_LYRIC, MSG_BYE)


# ---------------- 纯逻辑层：协议 ----------------

def encode_message(msg: dict) -> bytes:
    """字典 -> UDP 载荷。中文歌词要 ensure_ascii=False，否则板端/窗口都是乱码。"""
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_message(data: bytes) -> dict:
    """UDP 载荷 -> 字典。坏包返回 {'type': ''}，绝不抛给渲染循环。"""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"type": ""}
    return obj if isinstance(obj, dict) else {"type": ""}


def make_progress(pos, duration, paused=False, title=None) -> dict:
    return {"type": MSG_PROGRESS, "pos": pos, "duration": duration,
            "paused": bool(paused), "title": title}


def make_download(done, total=None, speed=0.0) -> dict:
    return {"type": MSG_DOWNLOAD, "done": done, "total": total,
            "speed": speed}


def make_lyric(line: str, index: int = 0) -> dict:
    """歌词消息（扩展位，窗口默认不显示）。"""
    return {"type": MSG_LYRIC, "line": line, "index": index}


def make_bye() -> dict:
    return {"type": MSG_BYE}


# ---------------- 纯逻辑层：三档策略 ----------------

@dataclass(frozen=True)
class ProgressPolicy:
    """
    F5 三档策略：
      命令模式默认关（会顶掉 input() 里正在敲的内容）
      语音模式自动开（没人用键盘输入）
      连着 micro:bit 时关（进度由板子点阵屏显示，两边同时刷会互相干扰）
    """
    mode: str = "command"            # "command" | "voice"
    microbit_connected: bool = False
    force: bool = False              # 用户手动 `progress force` 时强制开

    def enabled(self) -> bool:
        if self.microbit_connected:
            return False
        if self.force:
            return True
        return self.mode == "voice"


# ---------------- 纯逻辑层：歌词扩展位 ----------------

class LyricPane:
    """
    歌词面板的扩展骨架。当前只存最近一行 + 提供 render()，
    窗口默认不画它；协议和分派已经通了，将来接进来不用改主程序。
    """

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self.line = ""
        self.index = 0

    def update(self, msg: dict):
        self.line = str(msg.get("line", ""))
        self.index = int(msg.get("index", 0) or 0)

    def render(self, max_width: int = 80) -> str:
        if not self.enabled or not self.line:
            return ""
        return clip_line("[词] " + self.line, max_width)


# ---------------- 纯逻辑层：渲染分派 ----------------

def clip_line(text: str, max_width: int) -> str:
    """按显示宽度裁剪（中文算两格），保证一行不折行 —— F9 的前提。"""
    if max_width <= 0:
        return ""
    out, total = [], 0
    for ch in text:
        w = 2 if progress_bar.display_width(ch) == 2 else 1
        if total + w > max_width:
            break
        out.append(ch)
        total += w
    return "".join(out)


def render_message(msg: dict, max_width: int, include_lyric: bool = False,
                   lyrics: LyricPane = None) -> str:
    """
    把一条消息渲染成一行文本。

    ⚠️ 宽度自适应 / 裁剪顺序 / `||` 暂停标记全部由现有 render_* 负责，
    这里只做分派，避免两边逻辑跑偏（PITFALLS F3/F4/F9）。
    """
    kind = msg.get("type")
    if kind == MSG_PROGRESS:
        return progress_bar.render_bar(
            msg.get("pos"), msg.get("duration"),
            max_width=max_width, color=False,
            paused=bool(msg.get("paused")),
        )
    if kind == MSG_DOWNLOAD:
        return progress_download.render_download(
            msg.get("done", 0), total=msg.get("total"),
            speed=msg.get("speed", 0.0),
            max_width=max_width, color=False,
        )
    if kind == MSG_LYRIC and include_lyric:
        pane = lyrics or LyricPane(enabled=True)
        pane.update(msg)
        return pane.render(max_width)
    return ""


def paint(stream, line: str, max_width: int, last_width: int = 0) -> int:
    """
    原地刷新一行。传 stream 而不是硬编码 sys.stdout（B7），
    这样离验证脚本能用 StringIO 测，不会擦到真控制台。
    """
    safe = clip_line(line, max_width)
    width = progress_bar.visible_width(safe)
    pad = max(0, last_width - width)
    stream.write("\r" + safe + " " * pad + "\r")
    stream.flush()
    return width


# ---------------- 窗口进程 ----------------

class _WindowServer:
    """跑在新控制台里，只负责收消息 + 画，绝不反向阻塞主程序。"""

    def __init__(self, port: int, host: str = DEFAULT_HOST,
                 stream=None, include_lyric: bool = False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.settimeout(0.1)
        self.stream = stream or sys.stdout
        self.include_lyric = include_lyric
        self.lyrics = LyricPane(enabled=include_lyric)
        self.last = {}
        self._drawn_width = 0

    def _draw(self):
        width = max(20, progress_bar.terminal_width() - 1)
        # 下载优先于播放：下载时用户更关心还要等多久
        msg = self.last.get(MSG_DOWNLOAD) or self.last.get(MSG_PROGRESS)
        line = render_message(msg or {}, width,
                              include_lyric=self.include_lyric,
                              lyrics=self.lyrics)
        self._drawn_width = paint(self.stream, line, width, self._drawn_width)

    def _clear(self):
        paint(self.stream, "", 1, self._drawn_width)
        self._drawn_width = 0

    def run(self, seconds=None):
        end = None if seconds is None else time.time() + seconds
        try:
            while True:
                if end is not None and time.time() >= end:
                    break
                try:
                    data, _addr = self.sock.recvfrom(65535)
                except socket.timeout:
                    self._draw()
                    continue
                msg = decode_message(data)
                if msg.get("type") == MSG_BYE:
                    break
                if msg.get("type") in MESSAGE_TYPES:
                    if msg["type"] == MSG_LYRIC:
                        self.lyrics.update(msg)
                    else:
                        self.last[msg["type"]] = msg
                self._draw()
        except KeyboardInterrupt:
            pass
        finally:
            self._clear()
            self.sock.close()


# ---------------- 主进程侧客户端 ----------------

def _free_udp_port(host: str = DEFAULT_HOST) -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


class ProgressWindow:
    """
    主程序侧的唯一入口。整合时 main_CUI 只需要：

        win = ProgressWindow(ProgressPolicy(mode="voice"))
        win.start()
        win.progress(pos, duration)      # 或 win.download(done, total, speed)
        win.stop()

    本次**没有**接进 main_CUI.py（A9）。
    """

    def __init__(self, policy: ProgressPolicy = None, host: str = DEFAULT_HOST,
                 spawn: bool = True, python_exe: str = None):
        self.policy = policy or ProgressPolicy()
        self.host = host
        self.spawn = spawn
        self.python_exe = python_exe or sys.executable
        self.port = None
        self.sock = None
        self.proc = None

    def start(self) -> bool:
        if not self.policy.enabled():
            return False
        self.port = _free_udp_port(self.host)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.spawn:
            creation = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
            self.proc = subprocess.Popen(
                [self.python_exe, os.path.abspath(__file__),
                 "--window", "--port", str(self.port)],
                creationflags=creation if sys.platform == "win32" else 0,
            )
            time.sleep(0.3)          # 等窗口进程 bind 完，第一帧不至于丢
        return True

    def publish(self, msg: dict) -> bool:
        if self.sock is None or self.port is None:
            return False
        try:
            self.sock.sendto(encode_message(msg), (self.host, self.port))
            return True
        except OSError:
            return False

    def progress(self, pos, duration, paused=False, title=None):
        return self.publish(make_progress(pos, duration, paused, title))

    def download(self, done, total=None, speed=0.0):
        return self.publish(make_download(done, total, speed))

    def lyric(self, line: str, index: int = 0):
        return self.publish(make_lyric(line, index))

    def stop(self):
        try:
            self.publish(make_bye())
        finally:
            if self.sock is not None:
                self.sock.close()
                self.sock = None
            self.proc = None


def main(argv=None):
    ap = argparse.ArgumentParser(description="进度条独立窗口进程")
    ap.add_argument("--window", action="store_true", help="作为窗口进程运行")
    ap.add_argument("--port", type=int, required=True, help="UDP 端口")
    ap.add_argument("--seconds", type=float, default=None, help="调试：跑几秒自动退出")
    args = ap.parse_args(argv)
    if not args.window:
        ap.error("窗口进程必须带 --window")
    server = _WindowServer(args.port)
    server.run(seconds=args.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
