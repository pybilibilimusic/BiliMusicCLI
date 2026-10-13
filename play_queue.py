"""
播放队列（纯逻辑层）。

只负责一件事：一串歌，现在播到第几首，下一首该是谁。
**不碰 mpv、不碰网络、不碰数据库** —— 所以能离线跑测试，将来接进
main_CUI 时逻辑一行都不用改。

为什么要跟现有行为对齐：
    现在播完一首是 `random.choice(缓存文件)` 随便换一首（main_CUI._auto_play_next）。
    接进来之后变成：队列里有歌 → 按队列走；队列空 → 退回原来的随机兜底
    （构造时传 fallback）。这样即使你一首歌都没排队，行为也跟现在一样，
    不会出现「接了队列反而没歌听」。

术语：
    - 指针 index：当前曲目在队列里的下标，-1 表示「游离态」
      （正在播的那首不在队列里，比如直接 dl 播的、或者随机兜底播的）
    - 游离态按下一首 → 从队头开始播
"""

from __future__ import annotations

import os
import random
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# 四种播放模式
MODE_SEQUENCE = "sequence"    # 顺序播放：播完最后一首就停
MODE_LOOP = "loop"            # 列表循环：播完最后一首回到第一首
MODE_SHUFFLE = "shuffle"      # 随机播放
MODE_SINGLE = "single"        # 单曲循环：播完还是自己

MODES = (MODE_SEQUENCE, MODE_LOOP, MODE_SHUFFLE, MODE_SINGLE)

MODE_NAMES = {
    MODE_SEQUENCE: "顺序播放",
    MODE_LOOP: "列表循环",
    MODE_SHUFFLE: "随机播放",
    MODE_SINGLE: "单曲循环",
}

# 当前曲目标记：用纯 ASCII。别换成 ▶ / ⏸ 这类字符 —— 有些终端字体没这个字形，
# 实测这个项目里就渲染不出来（进度条踩过一次）。
CURRENT_MARK = ">"

MAX_HISTORY = 50


def _norm_path(path) -> str:
    """
    路径归一化，用来判断「是不是同一首歌」。

    Windows 上路径大小写不敏感，而且同一首歌可能一次是相对路径、
    一次是绝对路径，所以统一成 normcase + abspath 再比。
    """
    if path is None or str(path) == "":
        return ""
    return os.path.normcase(os.path.abspath(str(path)))


def _clip(text: str, width: int) -> str:
    """按显示宽度截断（中文算两格），塞不下就砍掉尾巴加 …。"""
    if width <= 0:
        return ""
    total = 0
    out = []
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if total + w > width:
            break
        out.append(ch)
        total += w
    if len(out) < len(text):
        # 留出 … 的位置
        while out and total + 1 > width:
            ch = out.pop()
            total -= 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        out.append("…")
    return "".join(out)


@dataclass
class QueueItem:
    """队列里的一条。bvid 可以为空（比如本地文件）。"""

    title: str = ""
    path: str = ""
    bvid: str = ""

    @property
    def name(self) -> str:
        """文件名。"""
        return Path(self.path).name if self.path else ""

    @property
    def display(self) -> str:
        """给人看的名字：有标题用标题，没有就用文件名。"""
        return self.title or self.name

    def matches(self, path) -> bool:
        """是不是同一个文件。"""
        return bool(self.path) and _norm_path(self.path) == _norm_path(path)


class PlayQueue:
    """
    播放队列。

    用法（最小）：
        q = PlayQueue()
        q.add("晴天", "mp3/晴天.mp3")
        item = q.next()          # -> QueueItem
    """

    def __init__(self, fallback=None, mode: str = MODE_SEQUENCE, rng=None):
        self._items: list[QueueItem] = []
        self._index = -1                 # -1 = 游离态
        self._history: list[str] = []    # 存归一化路径，比存下标稳（删歌后下标会串）
        self.fallback = fallback         # 队列空时调用，返回 path 或 None
        self.rng = rng or random.Random()
        self.mode = MODE_SEQUENCE
        self.set_mode(mode)

    # ---------- 看 ----------

    @property
    def items(self) -> list[QueueItem]:
        """队列内容（副本，改它没用）。"""
        return list(self._items)

    @property
    def current_index(self) -> int:
        """当前下标，-1 = 游离态。"""
        return self._index

    @property
    def current(self):
        """当前曲目，没有就 None。"""
        if 0 <= self._index < len(self._items):
            return self._items[self._index]
        return None

    @property
    def history(self) -> list[str]:
        return list(self._history)

    def __len__(self) -> int:
        return len(self._items)

    def is_empty(self) -> bool:
        return not self._items

    def summary(self) -> str:
        """一行状态：共几首 / 第几首 / 什么模式。"""
        if not self._items:
            return f"队列是空的（{MODE_NAMES[self.mode]}）"
        where = "未选中" if self._index < 0 else f"第 {self._index + 1} 首"
        return f"共 {len(self._items)} 首 / {where} / {MODE_NAMES[self.mode]}"

    # ---------- 增删改 ----------

    def add(self, title="", path="", bvid="", position=None) -> int:
        """
        加一首。position 为空加在末尾，否则插到那个下标前面。
        返回实际插入的下标。
        """
        item = QueueItem(title=title, path=str(path), bvid=bvid)
        if position is None:
            self._items.append(item)
            return len(self._items) - 1
        position = max(0, min(len(self._items), int(position)))
        self._items.insert(position, item)
        if self._index >= position:
            # 插在指针前面，指针得往后让一位
            self._index += 1
        return position

    def add_next(self, title="", path="", bvid="") -> int:
        """插到当前曲目后面（游离态就插到队头）—— 「下一首播这个」。"""
        return self.add(title, path, bvid, position=self._index + 1)

    def remove_at(self, index) -> bool:
        """
        删掉第 index 首（0 起）。返回有没有删成。

        指针怎么处理是最容易写错的地方，规则是：
        - 删的是指针前面的 → 指针 -1
        - 删的就是当前这首 → 指针不动（后面的顶上来，但**不会自动开始播**）
        - 删的是最后一首 → 退回游离态
        """
        if not (0 <= index < len(self._items)):
            return False
        self._items.pop(index)
        if index < self._index:
            self._index -= 1
        elif index == self._index and self._index >= len(self._items):
            self._index = -1
        return True

    def remove_current(self) -> bool:
        return self.remove_at(self._index)

    def move(self, src, dst) -> bool:
        """把第 src 首挪到第 dst 个位置。当前曲目跟着一起挪。"""
        n = len(self._items)
        if not (0 <= src < n) or not (0 <= dst < n):
            return False
        item = self._items.pop(src)
        self._items.insert(dst, item)
        if self._index == src:
            self._index = dst
        elif src < self._index <= dst:
            self._index -= 1
        elif dst <= self._index < src:
            self._index += 1
        return True

    def clear(self):
        """清空队列、指针和历史。"""
        self._items.clear()
        self._index = -1
        self._history.clear()

    # ---------- 指针 ----------

    def _find_index(self, path) -> int:
        """找这个文件在队列里的下标，找不到返回 -1（不产生副作用）。"""
        target = _norm_path(path)
        if not target:
            return -1
        for i, item in enumerate(self._items):
            if _norm_path(item.path) == target:
                return i
        return -1

    def sync_current(self, path) -> bool:
        """
        把指针对到正在播的那首。

        不在队列里 → 退回游离态并返回 False。播放器每放一首都该调一次，
        不然「下一首」会从莫名其妙的地方开始。
        """
        idx = self._find_index(path)
        self._index = idx
        return idx >= 0

    def set_index(self, index) -> bool:
        """直接跳到第 index 首（0 起）。"""
        if not (0 <= index < len(self._items)):
            return False
        self._index = index
        return True

    # ---------- 走位 ----------

    def _push_history(self):
        """离开当前这首之前记一笔，「上一首」要靠它。"""
        cur = self.current
        if cur is None:
            return
        path = _norm_path(cur.path)
        if path:
            self._history.append(path)
            if len(self._history) > MAX_HISTORY:
                del self._history[0]

    def _pick_shuffle(self) -> int:
        """随机挑一个下标，尽量不挑到当前这首。"""
        n = len(self._items)
        if n <= 1:
            return 0
        choices = [i for i in range(n) if i != self._index]
        return self.rng.choice(choices)

    def _plan_next(self):
        """算下一首的下标，没有就返回 None（顺序播放到尾了）。"""
        if len(self._items) == 1:
            # 只有一首就一直播它 —— 对齐接进来之前的行为
            # （原来随机兜底在只剩一首时会重复自己，不会说「没歌了」）
            return 0
        if self.mode == MODE_SHUFFLE:
            return self._pick_shuffle()
        nxt = self._index + 1
        if nxt < len(self._items):
            return nxt
        return 0 if self.mode == MODE_LOOP else None

    def next(self, auto: bool = False):
        """
        下一首。

        auto=True 表示「这首歌自己播完了」，False 表示「用户手动按的下一首」。
        区别只在单曲循环：播完了还是自己，手动按就该真的换一首。

        返回 QueueItem 或 None（None = 没有下一首了，该停下）。
        """
        if not self._items:
            return self._fallback_item()
        if auto and self.mode == MODE_SINGLE and self._index >= 0:
            return self.current          # 单曲循环：播完还是这首
        target = self._plan_next()
        if target is None:
            return None                  # 顺序播放到尾
        self._push_history()
        self._index = target
        return self.current

    def prev(self):
        """
        上一首。

        先退播放历史（随机模式下尤其重要，不然「上一首」根本回不去），
        历史空了才退到列表里前一首。
        """
        if not self._items:
            return None
        while self._history:
            path = self._history.pop()
            idx = self._find_index(path)
            if idx >= 0:
                self._index = idx
                return self.current
            # 那首被删了就继续往前翻
        if self._index < 0:
            return None
        prev = self._index - 1
        if prev < 0:
            if self.mode != MODE_LOOP:
                return None
            prev = len(self._items) - 1
        self._index = prev
        return self.current

    def _fallback_item(self):
        """队列空时的兜底（默认行为：跟现在一样随机挑一首缓存）。"""
        if not callable(self.fallback):
            return None
        path = self.fallback()
        if not path:
            return None
        return QueueItem(path=str(path))

    # ---------- 模式 ----------

    def set_mode(self, mode: str) -> bool:
        if mode not in MODES:
            return False
        self.mode = mode
        return True

    def cycle_mode(self) -> str:
        """顺序 → 循环 → 随机 → 单曲 → 回到顺序。"""
        self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)]
        return self.mode

    # ---------- 渲染 ----------

    def format_list(self, width=None, max_rows=None) -> list[str]:
        """
        渲染成人能看的列表：编号从 1 起，当前那行打 `>` 标记。

        - width：按显示宽度截断（中文算两格）
        - max_rows：最多显示几行，会以当前曲目为中心开窗，保证当前那首一定在
        """
        total = len(self._items)
        if total == 0:
            return ["（队列是空的）"]

        start = 0
        end = total
        if max_rows is not None and max_rows > 0 and total > max_rows:
            anchor = self._index if self._index >= 0 else 0
            start = max(0, anchor - 2)
            if start + max_rows > total:
                start = max(0, total - max_rows)
            end = min(total, start + max_rows)

        rows = []
        for i in range(start, end):
            mark = CURRENT_MARK if i == self._index else " "
            text = f"{mark} {i + 1}. {self._items[i].display}"
            rows.append(_clip(text, width) if width else text)
        if end < total:
            rows.append(f"  … 还有 {total - end} 首")
        if start > 0:
            rows.insert(0, f"  … 前面还有 {start} 首")
        return rows
