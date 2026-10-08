"""
搜索反馈埋点：把「用户本来就在做的动作」攒下来，够量了再去分析排序该怎么改。

为什么不做人工评测集了：
    人工挑出来的期望值和程序的排序其实是同源的 —— 都在 B 站搜索结果的前几名里。
    实测 26 条待确认用例里，23 条的前 3 名全是同一首歌的不同版本（录音棚版、
    4K 修复 MV、Hi-Res 版……），程序怎么排都对，人怎么选也只能在这几个里选。
    这种用例测出来的永远是高分，没有信息量；而它消耗的时间一点不少。

    真正有价值的是「程序错了 + 人不同意」的那一刻。那一刻什么时候发生，
    只有用户在用的时候才知道 —— 所以改成埋点，让真实使用替我们把错误挑出来。

记什么：不猜，只看用户的行为（没有额外操作，全是本来就有的动作）

    accept   回车 / 选 1            -> 认可推荐
    pick     选了第 2~N 个           -> 否定了 Top1，选了别的
    page     按 r 翻页               -> 这一整页都不行
    cancel   按 q 取消               -> 压根没找到想要的
    switch   语音喊「换个版本」        -> 播了但不对
    auto     语音 / 批量直接播了 Top1  -> 没接着喊换版本，就是默认认可

⚠️ 三条底线：
    1. 只记关键词和候选视频的信息，不记账号、不上传，纯本地文件
    2. config.ini 里 [feedback] enabled = 0 可以彻底关掉，关了就一个字都不写
    3. 埋点出任何岔子都不能影响播放 —— record() 内部自己兜住所有异常

什么时候看：样本 100 条以上再说。100 条大概能把 Top1 接受率的误差压到 ±10%，
低于这个量，看出来的「规律」多半是噪声。

用法：
    python feedback.py            # 打印统计报告
    python feedback.py --selftest # 离线自测（用临时库，不碰真实数据）
"""

import configparser
import json
import os
import sqlite3
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.ini")
DB_PATH = os.path.join(HERE, "feedback.db")

# 建议的样本量：低于这个数就别急着下结论
ENOUGH_SAMPLES = 100

# 候选视频只留这些字段，够分析了，也不至于把库撑大
MAX_CANDIDATES = 10
CANDIDATE_FIELDS = ("bvid", "title", "duration", "typename", "play", "score")

ACTIONS = {
    "accept": "回车 / 选 1 —— 认可推荐",
    "pick": "选了第 2~N 个 —— 否定了 Top1",
    "page": "按 r 翻页 —— 这一整页都不行",
    "cancel": "按 q 取消 —— 压根没找到想要的",
    "switch": "语音喊「换个版本」—— 播了但不对",
    "auto": "语音 / 批量直接播了 Top1（没喊换版本就是认可）",
}

# 报告文案双语 —— 主程序是双语的，这个面板没道理只有中文
REPORT = {
    "zh": {
        "title": "搜索反馈统计",
        "empty": "还没有任何记录。用用看：search / voice 的每次选择都会记下来。",
        "samples": "样本 %d 条",
        "not_enough": "　⚠ 还不够 %d 条，先别急着下结论 —— 样本太少看出来的都是噪声。",
        "accept_rate": "  Top1 接受率   %.1f%%　（认可 %d / 否定 %d）",
        "deep": "  否定后再挑时的平均位次　%.1f　（越大说明想要的歌排得越靠后）",
        "switch": "  喊换版本平均在第　%.1f 个候选上发生",
        "breakdown": "  各种行为占比：",
        "worst": "  被否得最多的 Top1（调排序优先看这里）：",
        "worst_row": "    %d 次  %s | %s　（搜「%s」时）",
    },
    "en": {
        "title": "Search feedback",
        "empty": "Nothing recorded yet. Every search / voice pick gets logged.",
        "samples": "Samples: %d",
        "not_enough": "  ! fewer than %d samples - too early to read anything into this.",
        "accept_rate": "  Top-1 kept      %.1f%%   (kept %d / rejected %d)",
        "deep": "  Average rank picked after rejecting: %.1f (higher = wanted song sat deeper)",
        "switch": "  \"Another version\" usually said at candidate #%.1f",
        "breakdown": "  What you actually did:",
        "worst": "  Top-1 rejected most often (fix the ranking for these first):",
        "worst_row": "    %d times  %s | %s   (query: %s)",
    },
}

ACTION_NOTES_EN = {
    "accept": "Enter / picked #1 - kept the recommendation",
    "pick": "Picked #2 or lower - rejected the top-1",
    "page": "Pressed r for the next page - nothing on this page worked",
    "cancel": "Pressed q - gave up entirely",
    "switch": "Said \"another version\" - played but wrong",
    "auto": "Voice/batch played the top-1 with no complaint",
}

# 哪些动作算「认可」，其余算「否定」
POSITIVE = ("accept", "auto")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS search_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    query         TEXT NOT NULL,
    action        TEXT NOT NULL,
    picked_index  INTEGER DEFAULT -1,
    page          INTEGER DEFAULT 0,
    source        TEXT DEFAULT 'cli',
    chosen_bvid   TEXT DEFAULT '',
    chosen_title  TEXT DEFAULT '',
    candidates    TEXT DEFAULT '[]'
)
"""

INSERT_SQL = """
INSERT INTO search_events
    (ts, query, action, picked_index, page, source, chosen_bvid, chosen_title,
     candidates)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def config_enabled(default=True):
    """读 config.ini 的 [feedback] enabled；读不到就按默认（开）。"""
    try:
        parser = configparser.ConfigParser()
        parser.read(CONFIG_FILE, encoding="utf-8")
        return parser.getboolean("feedback", "enabled", fallback=default)
    except Exception:
        return default


def config_language(default="zh"):
    """读主程序的语言设置，好让统计面板跟着走。"""
    try:
        parser = configparser.ConfigParser()
        parser.read(CONFIG_FILE, encoding="utf-8")
        raw = (parser.get("language", "language", fallback=default) or "").strip()
        if raw.lower().startswith("en"):
            return "en"
        return "zh"
    except Exception:
        return default


def _slim(candidates, limit=MAX_CANDIDATES):
    """候选列表瘦身后再存：只要分析用得到的字段，且最多留前 N 条。"""
    slim = []
    for item in list(candidates)[:limit]:
        row = {}
        for field in CANDIDATE_FIELDS:
            row[field] = item.get(field)
        row["clean"] = item.get("clean") or item.get("title") or ""
        slim.append(row)
    return slim


class FeedbackStore:
    """存搜索反馈的 SQLite 小仓库。关掉时所有写入都变成空操作。"""

    def __init__(self, db_path=None, enabled=None):
        self.db_path = db_path or DB_PATH
        self.enabled = config_enabled() if enabled is None else enabled
        if self.enabled:
            self.enabled = self._init_db()

    def _init_db(self):
        """建表。失败就自我关闭 —— 不能拖累主流程。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(CREATE_SQL)
            return True
        except Exception:
            return False

    def record(self, query, action, candidates=(), picked_index=-1, chosen=None,
               page=0, source="cli"):
        """
        记一条事件。返回 True / False（False 不一定是出错，关掉也算）。

        picked_index 是 0-based 的选择位次（选了第一个就传 0）。
        chosen 可以传被选中的那个候选 dict，也可以不传。
        """
        if not self.enabled or action not in ACTIONS:
            return False
        chosen = chosen or {}
        payload = (
            datetime.now().isoformat(timespec="seconds"),
            str(query or "").strip(),
            action,
            int(picked_index),
            int(page),
            str(source or "cli"),
            chosen.get("bvid") or "",
            chosen.get("title") or chosen.get("clean") or "",
            json.dumps(_slim(candidates), ensure_ascii=False),
        )
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(INSERT_SQL, payload)
            return True
        except Exception:
            return False

    # ---------------------------------------------------------- 统计
    def events(self):
        rows = []
        if not self.enabled:
            return rows
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT ts, query, action, picked_index, page, source, "
                    "chosen_bvid, chosen_title, candidates "
                    "FROM search_events ORDER BY id")
                for row in cursor:
                    rows.append({
                        "ts": row[0], "query": row[1], "action": row[2],
                        "picked_index": row[3], "page": row[4],
                        "source": row[5], "chosen_bvid": row[6],
                        "chosen_title": row[7], "candidates": row[8],
                    })
        except Exception:
            return []
        return rows

    def stats(self):
        """把库里的记录算成几个能直接看的数。"""
        rows = self.events()
        total = len(rows)
        counts = {name: 0 for name in ACTIONS}
        for row in rows:
            counts[row["action"]] = counts.get(row["action"], 0) + 1

        positive = sum(counts.get(name, 0) for name in POSITIVE)
        negative = total - positive
        decided = total

        picks = [row["picked_index"] for row in rows if row["action"] == "pick"]
        switches = [row["picked_index"] for row in rows
                    if row["action"] == "switch"]
        deep = [row["picked_index"] for row in rows if row["picked_index"] >= 0]

        return {
            "total": total,
            "counts": counts,
            "positive": positive,
            "negative": negative,
            "accept_rate": (positive / decided) if decided else 0.0,
            "avg_pick_index": (sum(picks) / len(picks)) if picks else 0.0,
            "avg_switch_index": (sum(switches) / len(switches)) if switches else 0.0,
            "deep_pick_rate": (len([i for i in deep if i > 0]) / len(deep)) if deep else 0.0,
            "worst_top1": self._worst_top1(rows),
        }

    @staticmethod
    def _worst_top1(rows, top=5):
        """被否定的 Top1 排行 —— 这张表才是调排序的依据。"""
        tally = {}
        for row in rows:
            if row["action"] in POSITIVE:
                continue
            candidates = None
            try:
                candidates = json.loads(row["candidates"] or "[]")
            except ValueError:
                candidates = None
            if not candidates:
                continue
            first = candidates[0]
            key = first.get("bvid") or first.get("title") or "?"
            entry = tally.setdefault(key, {
                "bvid": first.get("bvid", ""),
                "title": first.get("clean") or first.get("title") or "",
                "query": row["query"],
                "times": 0,
            })
            entry["times"] += 1
        ranked = sorted(tally.values(), key=lambda x: -x["times"])
        return ranked[:top]

    def summarize(self, lang_code="zh"):
        """打印一份人读的报告。"""
        text = REPORT.get(lang_code) or REPORT["zh"]
        english = lang_code == "en"
        data = self.stats()
        print("=" * 72)
        print("%s　%s" % (text["title"], self.db_path) if not english
              else "%s  %s" % (text["title"], self.db_path))
        print("=" * 72)
        if not data["total"]:
            print(text["empty"])
            print("=" * 72)
            return data

        print(text["samples"] % data["total"])
        if data["total"] < ENOUGH_SAMPLES:
            print(text["not_enough"] % ENOUGH_SAMPLES)
        print()
        print(text["accept_rate"] % (data["accept_rate"] * 100,
                                     data["positive"], data["negative"]))
        print(text["deep"] % (data["avg_pick_index"] + 1))
        if data["counts"]["switch"]:
            print(text["switch"] % (data["avg_switch_index"] + 1))
        print()
        print(text["breakdown"])
        for name, count in data["counts"].items():
            if count:
                note = ACTION_NOTES_EN.get(name, "") if english \
                    else ACTIONS.get(name, "")
                print("    %-8s %3d 次   %s" % (name, count, note)
                      if not english else "    %-8s %3d       %s"
                      % (name, count, note))

        if data["worst_top1"]:
            print()
            print(text["worst"])
            for item in data["worst_top1"]:
                print(text["worst_row"] % (item["times"], item["bvid"],
                                           item["title"][:30], item["query"]))

        print("=" * 72)
        return data


# ---------------------------------------------------------- 模块级便捷入口
_default = None


def store():
    """全局单例。关掉再打开时调 reset() 让它重新读配置。"""
    global _default
    if _default is None:
        _default = FeedbackStore()
    return _default


def reset():
    global _default
    _default = None


def record(query, action, **kwargs):
    """
    主流程只调这个。埋点里出任何岔子都不许往外冒 —— 宁可不记，也不能影响播放。
    """
    try:
        return store().record(query, action, **kwargs)
    except Exception:
        return False


# ---------------------------------------------------------- 离线自测
def selftest():
    """
    用临时库跑一遍，不碰真实数据。

    验的是这几件事：记进去的东西能被正确算出来、关掉时一个字都不写、
    候选列表能原样取回来、样本量够不够的判断是对的。
    """
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, ok, detail))

    tmp_dir = tempfile.mkdtemp(prefix="feedback_selftest_")
    db = os.path.join(tmp_dir, "t.db")

    def fake_cands(n, prefix="BV_TEST"):
        return [{"bvid": "%s%03d" % (prefix, i), "title": "歌 %d" % i,
                 "clean": "歌 %d" % i, "duration": 200 + i, "score": 100 - i,
                 "typename": "音乐", "play": 1000 * i}
                for i in range(n)]

    enabled_store = FeedbackStore(db_path=db, enabled=True)

    # 12 次认可 + 8 次否定 = 20 条
    for i in range(12):
        enabled_store.record("歌%d" % i, "accept", candidates=fake_cands(5),
                             picked_index=0, chosen=fake_cands(5)[0])
    bad = fake_cands(5, "BV_BAD")
    for i in range(5):
        enabled_store.record("难的%d" % i, "pick", candidates=bad,
                             picked_index=2, chosen=bad[2])
    for i in range(2):
        enabled_store.record("难的%d" % i, "switch", candidates=bad,
                             picked_index=0, chosen=bad[1], source="voice")
    enabled_store.record("找不到的", "cancel", candidates=bad, picked_index=-1)

    data = enabled_store.stats()
    check("总条数正确", data["total"] == 20, str(data["total"]))
    check("Top1 接受率算对", abs(data["accept_rate"] - 12 / 20) < 1e-6,
          "%.3f" % data["accept_rate"])
    check("pick 计数正确", data["counts"]["pick"] == 5, str(data["counts"]))
    check("switch 计数正确", data["counts"]["switch"] == 2, str(data["counts"]))
    check("平均选择位次算对", abs(data["avg_pick_index"] - 2.0) < 1e-6,
          "%.2f" % data["avg_pick_index"])

    worst = data["worst_top1"]
    check("被否最多的 Top1 排第一", worst and worst[0]["bvid"] == "BV_BAD000"
          and worst[0]["times"] == 8, str(worst[:1]))

    # 候选列表能原样取回来
    rows = enabled_store.events()
    got = json.loads(rows[0]["candidates"])
    check("候选列表存取得回来",
          len(got) == 5 and got[0]["bvid"] == "BV_TEST000"
          and got[0]["duration"] == 200 and got[0]["score"] == 100,
          str(got[:1]))
    check("字段没多存", set(got[0]) == set(CANDIDATE_FIELDS) | {"clean"},
          str(sorted(got[0])))

    # 超出 MAX_CANDIDATES 的只留前 10 条
    enabled_store.record("长列表", "accept", candidates=fake_cands(30),
                         picked_index=0)
    rows = enabled_store.events()
    check("候选列表最多留 %d 条" % MAX_CANDIDATES,
          len(json.loads(rows[-1]["candidates"])) == MAX_CANDIDATES,
          str(len(json.loads(rows[-1]["candidates"]))))

    # 关掉状态：不建库、不写东西
    off_db = os.path.join(tmp_dir, "off.db")
    off_store = FeedbackStore(db_path=off_db, enabled=False)
    wrote = off_store.record("关了", "accept", candidates=fake_cands(3),
                             picked_index=0)
    check("关掉时不写入", wrote is False)
    check("关掉时不建库文件", not os.path.exists(off_db))
    check("关掉时统计为空", off_store.stats()["total"] == 0)

    # 非法动作不落库
    check("未知动作被挡掉",
          enabled_store.record("x", "totally_unknown", candidates=[]) is False)

    try:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass

    print("=" * 72)
    print("feedback 自测（离线，用临时库）")
    print("=" * 72)
    failed = 0
    for name, ok, detail in checks:
        print("  [%s] %s%s" % ("OK" if ok else "FAIL", name,
                               "" if ok else "   -> " + str(detail)))
        failed += 0 if ok else 1
    print("-" * 72)
    print("共 %d 项：通过 %d / 失败 %d" % (len(checks), len(checks) - failed,
                                     failed))
    print("=" * 72)
    return 1 if failed else 0


def main():
    if "--selftest" in __import__("sys").argv:
        return selftest()
    store_ = FeedbackStore()
    if not store_.enabled:
        print("反馈埋点已关闭（config.ini 的 [feedback] enabled = 0）。"
              if config_language() == "zh"
              else "Feedback logging is off ([feedback] enabled = 0).")
        return 0
    store_.summarize(lang_code=config_language())
    if store_.stats()["total"] < ENOUGH_SAMPLES:
        print("样本到 %d 条以上再来分析，现在看容易把噪声当规律。"
              % ENOUGH_SAMPLES if config_language() == "zh"
              else "Wait until %d+ samples before drawing conclusions."
              % ENOUGH_SAMPLES)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
