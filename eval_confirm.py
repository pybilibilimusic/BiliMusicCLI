"""
半自动确认搜索评测集的期望 BV 号。

为什么要有这个东西：
    eval_search.py 的期望值必须人工确认 —— 拿程序跑出来的 Top1 反填期望值，
    准确率必然 100%，评测集就废了。但 20 多条手动查 BV 号确实烦，
    所以做成「脚本开网页、人挑链接、脚本洗 BV 号 + 反查标题、最后写文件」。

用法：
    python eval_confirm.py                 # 从第一条待确认的开始
    python eval_confirm.py --start 5       # 跳过前 5 条（接着上次的进度）
    python eval_confirm.py --only 花海     # 只处理包含某个词的用例
    python eval_confirm.py --rounds 5      # 每条最多粘 5 次（默认 3）
    python eval_confirm.py --no-browser    # 不自动开浏览器（自己已经开着了）
    python eval_confirm.py --apply         # 确认完顺手写回 eval_search.py
    python eval_confirm.py --selftest      # 离线自测粘贴轮次逻辑（不联网）

逐条流程：
    1. 自动打开 B 站搜索页（关键词就是评测用例的查询词）
    2. 你在浏览器里挑出确实是这首歌的视频，把链接复制回来粘贴
       —— 整段乱贴也行，支持完整网址、b23.tv 短链、纯 BV 号、av 号混着来
    3. 脚本洗出 BV 号，并调 B 站接口把标题查出来给你二次核对
    4. 一条用例最多粘 MAX_PASTE_ROUNDS 次（默认 3），多轮结果合并去重
    5. 结束后写入 eval_confirmed.json（+ 可读版 eval_confirmed.md）

为什么要允许多次粘贴：
    一首歌在 B 站往往有好几个像样的版本（官方 MV、专辑音源、现场、翻唱、
    不同 UP 主转投）。只锚定一个 BV 号的话，程序返回另一个合理版本会被判
    失败 —— 分数虚低；反过来只放一个号，又有「碰巧命中」的成分。
    所以一条用例收一组可接受答案，评测时 Top1 命中其中任意一个就算通过
    （eval_search.run_one 里就是 `bvid in expected`）。
    想再补几个版本，就换个说法再搜一轮（比如加「原唱」「官方」），粘回来。

粘贴时的命令（单独一行输入）：
    空行  够用了，提前结束这条（至少已收 1 个才算；一条没粘则是提示重来）
    s     跳过这条，下次再处理
    d     这条用例无效（B 站上压根没有这首歌 / 用例本身不合理）
    q     结束并把已经确认的写进文件

⚠️ 期望值是你确认的，不是程序跑出来的。脚本只负责洗链接和查标题，
   不会自己挑一个「看起来对」的 BV 号填进去。
"""

import json
import os
import re
import sys
import time
import urllib.parse
import webbrowser
from datetime import datetime

import requests

import config
import eval_search

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_SEARCH = os.path.join(HERE, "eval_search.py")
JSON_FILE = os.path.join(HERE, "eval_confirmed.json")
MD_FILE = os.path.join(HERE, "eval_confirmed.md")

SEARCH_URL = "https://search.bilibili.com/all?keyword=%s"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"

# 一条用例最多粘几次。同一首歌往往有多个合理版本，多给几次机会把
# 「可接受答案」凑全一点，评测时 Top1 命中其中任意一个就算通过。
MAX_PASTE_ROUNDS = 3

# BV 号：BV + 10 位字母数字。前后加边界，避免从更长的串里截出半截
BV_RE = re.compile(r"(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}(?![0-9A-Za-z])")
AV_RE = re.compile(r"(?<![0-9A-Za-z])av(\d{1,12})(?![0-9A-Za-z])", re.I)
SHORT_RE = re.compile(r"b23\.tv/[0-9A-Za-z]+", re.I)


# ------------------------------------------------------------------ 网络
def api_get(params, timeout=15):
    try:
        resp = requests.get(VIEW_API, params=params, headers=config.headers,
                            timeout=timeout)
        return resp.json()
    except Exception:
        return None


def bvid_to_info(bvid):
    """反查标题 / UP 主 / 时长，用来二次核对「这个 BV 号是不是这首歌」。"""
    data = api_get({"bvid": bvid})
    if not data or data.get("code") != 0:
        return None
    info = data.get("data") or {}
    return {
        "bvid": bvid,
        "title": info.get("title") or "",
        "uploader": (info.get("owner") or {}).get("name") or "",
        "duration": info.get("duration") or 0,
    }


def av_to_bvid(aid):
    data = api_get({"aid": aid})
    if not data or data.get("code") != 0:
        return None
    return (data.get("data") or {}).get("bvid")


def resolve_short(url):
    """b23.tv 短链跟着跳转拿到真实地址，再从中抠 BV 号。"""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        resp = requests.get(url, headers=config.headers, timeout=15,
                            allow_redirects=True)
        found = BV_RE.findall(resp.url)
        if found:
            return found[0]
        found = BV_RE.findall(resp.text[:20000])
        return found[0] if found else None
    except Exception:
        return None


# ------------------------------------------------------------------ 清洗
def extract_bvids(blob):
    """
    从一段乱七八糟的粘贴内容里洗出 BV 号，保持出现顺序并去重。

    能认的写法：完整视频网址、b23.tv 短链、纯 BV 号、av 号，
    B 站 App 分享出来那种「标题 https://...」也一并处理。
    """
    found, seen = [], set()

    def add(bvid):
        if bvid and bvid not in seen:
            seen.add(bvid)
            found.append(bvid)

    for bvid in BV_RE.findall(blob):
        add(bvid)

    for short in SHORT_RE.findall(blob):
        add(resolve_short(short))

    for aid in AV_RE.findall(blob):
        add(av_to_bvid(aid))

    return found


def fmt_duration(seconds):
    seconds = int(seconds or 0)
    return "%d:%02d" % (seconds // 60, seconds % 60)


# ------------------------------------------------------------------ 存档
def load_confirmed():
    if not os.path.exists(JSON_FILE):
        return {}
    try:
        with open(JSON_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_confirmed(data):
    with open(JSON_FILE, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)


def write_markdown(data, cases):
    """写一份人读的版本，末尾附好可直接粘进 eval_search.py 的 CASES 片段。"""
    notes = {c[0]: c[2] for c in cases}
    lines = ["# 搜索评测集：已人工确认的期望 BV 号", "",
             "由 `python eval_confirm.py` 生成，%s。"
             % datetime.now().strftime("%Y-%m-%d %H:%M"),
             "真源是 `eval_confirmed.json`（`--apply` 读的就是它）。", ""]
    for keyword in sorted(data):
        entry = data[keyword]
        lines.append("## %s" % keyword)
        if notes.get(keyword):
            lines.append("")
            lines.append("> %s" % notes[keyword])
        lines.append("")
        if entry.get("dropped"):
            lines.append("- **用例无效**，建议从 CASES 里删掉。"
                         + ("  理由：%s" % entry["dropped"]
                            if entry["dropped"] is not True else ""))
        elif entry.get("bvids"):
            for bvid in entry["bvids"]:
                lines.append("- `%s`" % bvid)
        else:
            lines.append("- （还没确认）")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 可直接粘进 CASES 的片段")
    lines.append("")
    lines.append("```python")
    for keyword in sorted(data):
        entry = data[keyword]
        if entry.get("bvids"):
            joined = ", ".join('"%s"' % b for b in entry["bvids"])
            lines.append('    ("%s", {%s}, "%s"),'
                         % (keyword, joined, notes.get(keyword, "")))
    lines.append("```")

    with open(MD_FILE, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


# ------------------------------------------------------------------ 回写
def apply_to_eval(data):
    """把确认结果写回 eval_search.py 的 CASES。改之前先备份。"""
    stamp = datetime.now().strftime("%Y-%m-%d-confirm")
    backup_dir = os.path.join(HERE, "backup", stamp)
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, "eval_search.py")
    with open(EVAL_SEARCH, encoding="utf-8") as handle:
        src = handle.read()
    with open(backup_path, "w", encoding="utf-8") as handle:
        handle.write(src)

    changed, skipped = [], []
    for keyword, entry in data.items():
        if entry.get("dropped"):
            # 只删单行写法；跨行的（备注太长换行）不猜，留给人手处理
            pattern = re.compile(r'^[ \t]*\("%s", .*?\),[ \t]*\n' % re.escape(keyword),
                                 re.M)
            if pattern.search(src):
                src = pattern.sub("", src, count=1)
                changed.append("%s（删除）" % keyword)
            else:
                skipped.append("%s（跨行，没敢自动删）" % keyword)
            continue
        if not entry.get("bvids"):
            continue
        joined = ", ".join('"%s"' % b for b in entry["bvids"])
        pattern = re.compile(r'(\("%s", )set\(\)' % re.escape(keyword))
        src, count = pattern.subn(lambda m: m.group(1) + "{" + joined + "}",
                                  src, count=1)
        if count:
            changed.append("%s -> %s" % (keyword, joined))
        elif ('("%s", {' % keyword) in src:
            skipped.append("%s（已经有期望值，跳过）" % keyword)
        else:
            skipped.append("%s（CASES 里找不到）" % keyword)

    with open(EVAL_SEARCH, "w", encoding="utf-8") as handle:
        handle.write(src)

    print("\n已写回 eval_search.py（备份在 %s）"
          % os.path.relpath(backup_path, HERE))
    for item in changed:
        print("  [OK]   %s" % item)
    for item in skipped:
        print("  [--]   %s" % item)


# ------------------------------------------------------------------ 交互
def read_paste():
    """读一段粘贴内容，空行结束。返回 (文本, 命令 or None)。"""
    lines = []
    while True:
        try:
            line = input("  > ").strip()
        except EOFError:
            return "\n".join(lines), "q"
        if not line:
            return "\n".join(lines), None
        if len(lines) == 0 and line.lower() in ("s", "d", "q"):
            return "", line.lower()
        lines.append(line)


def ask_line(prompt="  > "):
    """读一行；EOF 一律当成 q（结束保存）。"""
    try:
        return input(prompt).strip()
    except EOFError:
        return "q"


def one_case(index, total, keyword, note, use_browser, round_no, max_rounds,
             collected):
    """打印一轮提示并读一段粘贴。返回 (blob, command)。"""
    print()
    print("=" * 72)
    head = "[%d/%d] %s" % (index, total, keyword)
    if collected:
        head += "    第 %d/%d 次粘贴，已收 %d 个" % (round_no, max_rounds,
                                                 len(collected))
    else:
        head += "    第 %d/%d 次粘贴" % (round_no, max_rounds)
    print(head)
    if note:
        print("       %s" % note)

    if round_no == 1:
        url = SEARCH_URL % urllib.parse.quote_plus(keyword)
        print("\n  搜索页：%s" % url)
        if use_browser:
            try:
                webbrowser.open(url)
                print("  （已尝试用默认浏览器打开；没弹出来就把上面这行复制到地址栏）")
            except Exception as exc:
                print("  打开浏览器失败：%s" % exc)
        print("\n  在搜索结果里挑出确实是这首歌的，把链接粘回来。")
        print("  整段乱贴也行（网址 / b23.tv 短链 / BV 号 / av 号都能认）。")
        print("  粘完按一次回车、再按一次空回车结束这一轮。")
    else:
        print("\n  想再补几个版本，就换个说法再搜一轮（加「原唱」「官方 MV」之类），")
        print("  把新的链接粘回来；不补了就直接空回车收工。")

    print("  同一首歌的多个版本都会算作期望值，评测时命中任意一个即通过。")
    tail = "（还能再粘 %d 次）" % (max_rounds - round_no)
    if round_no >= max_rounds:
        tail = "（这是最后一次了）"
    print("  这条最多粘 %d 次 %s" % (max_rounds, tail))
    if collected:
        print("  已收：%s" % ", ".join(collected))
    print("  单独输入 s=跳过  d=这条用例无效  q=结束保存")

    return read_paste()


def confirm_bvids(keyword, bvids, existing=()):
    """
    把 BV 号对应的标题查出来给人核对，允许删掉查错的。

    返回 (采纳的 BV 号, action)，action 为 None / "retry" / "skip"。
    existing 是前几轮已经收下的，重复的不重复计入。
    """
    existing = list(existing)
    while True:
        print("\n  洗出 %d 个 BV 号，调接口核对标题：" % len(bvids))
        infos = []
        for bvid in bvids:
            info = bvid_to_info(bvid)
            infos.append(info)
            flag = "（已有）" if bvid in existing else ""
            if info:
                print("    [%d] %s%s  %s  | %s | UP %s"
                      % (len(infos), bvid, flag, fmt_duration(info["duration"]),
                         info["title"][:44], info["uploader"][:16]))
            else:
                print("    [%d] %s%s  （接口查不到，可能已失效）"
                      % (len(infos), bvid, flag))
            time.sleep(0.3)

        if all(b in existing for b in bvids):
            print("\n  这些之前都收过了。要补别的版本就重新粘贴，不需要就空行收工。")
        print("\n  Y = 全部采纳（都会算作这条用例的可接受答案）；"
              "输入序号（如 2 或 2,3）删掉不对的；r = 重新粘贴；s = 跳过这条")
        choice = input("  确认？[Y] ").strip().lower()
        if choice in ("", "y", "yes"):
            return bvids, None
        if choice == "r":
            return [], "retry"
        if choice == "s":
            return [], "skip"
        try:
            drop = {int(x) for x in re.split(r"[,\s]+", choice) if x}
            kept = [b for i, b in enumerate(bvids, 1) if i not in drop]
        except ValueError:
            print("  没看懂，请输入 Y / 序号 / r / s")
            continue
        if not kept:
            print("  全删光了，重新粘贴吧")
            return [], "retry"
        bvids = kept


def merge_bvids(base, extra):
    """合并两批 BV 号，保序去重。"""
    merged, seen = [], set()
    for bvid in list(base) + list(extra):
        if bvid and bvid not in seen:
            seen.add(bvid)
            merged.append(bvid)
    return merged


def collect_for_case(index, total, keyword, note, use_browser,
                     max_rounds=MAX_PASTE_ROUNDS):
    """
    跑完一条用例：最多粘 max_rounds 次，多轮结果合并；空行可提前收工。

    返回 (action, bvids)：
        "ok"   收下，bvids 是这条的可接受答案集合
        "skip" 一条都没收才算跳过；已经收了东西的按ok处理（不然前面白粘）
        "drop" 这条用例无效
        "quit" 结束整个流程
    """
    def finish_or_skip():
        """说跳过但已经收过东西 —— 那就当收工，别把前面的辛苦丢掉。"""
        if collected:
            print("  保留已收的 %d 个，这条到此为止。" % len(collected))
            return "ok", collected
        return "skip", None

    collected = []
    used = 0
    while used < max_rounds:
        blob, command = one_case(index, total, keyword, note, use_browser,
                                 used + 1, max_rounds, collected)
        if command == "q":
            return "quit", None
        if command == "s":
            return finish_or_skip()
        if command == "d":
            return "drop", None

        if not blob.strip():
            if collected:
                print("  够用了 —— 这条收 %d 个期望 BV 号。" % len(collected))
                return "ok", collected
            print("  这轮没粘东西。至少粘一条链接；"
                  "跳过输 s，判无效输 d，收工输 q。（空行不占粘贴次数）")
            continue

        used += 1
        bvids = extract_bvids(blob)
        if not bvids:
            print("  没洗出任何 BV 号。检查下粘的是不是 B 站链接？")
            again = ask_line("  重来 r / 跳过 s / 判无效 d：[r] ").lower()
            used -= 1                       # 没认出来不算一次
            if again == "s":
                return finish_or_skip()
            if again == "d":
                return "drop", None
            continue

        kept, action = confirm_bvids(keyword, bvids, collected)
        if action == "retry":
            used -= 1
            continue
        if action == "skip":
            return finish_or_skip()

        before = len(collected)
        collected = merge_bvids(collected, kept)
        print("  本轮新增 %d 个，累计 %d 个。"
              % (len(collected) - before, len(collected)))

    print("  已达 %d 次上限，这条收 %d 个。" % (max_rounds, len(collected)))
    return "ok", collected


def selftest():
    """
    离线自测粘贴轮次逻辑：不联网、不开浏览器，用假按键把交互整跑一遍。

    验的是这几条规则：
      1. 一条用例最多粘 N 次，粘满就停，不再多问
      2. 空行能提前收工
      3. 多轮结果合并去重
      4. 什么都没粘的空行 / 认不出链接，都不占次数
      5. s / d / q 三个命令、删序号，都还灵
    """
    import builtins
    import contextlib
    import io

    # 通过 globals() 换掉这两个，别用 global 关键字：那样会和赋值打架
    ns = globals()
    real_sleep, real_info = time.sleep, ns["bvid_to_info"]
    time.sleep = lambda *_args: None
    ns["bvid_to_info"] = lambda bvid: {"bvid": bvid, "title": "测试标题 " + bvid,
                                       "uploader": "测试UP", "duration": 214}

    checks = []

    def record(name, ok, detail=""):
        checks.append((name, ok, detail))

    def run_lines(lines, max_rounds=MAX_PASTE_ROUNDS):
        """
        喂一串假按键，静音跑一条用例。

        返回 (action, bvids, 剩余没用上的按键数, 打印出来的内容)。
        写的时候要卡死的那种提问会直接抛 AssertionError —— 比如粘满 3 次后
        还在问第 4 次，说明上限没生效。
        """
        feed = list(lines)
        asked = {"n": 0}

        def fake_input(prompt=""):
            asked["n"] += 1
            if not feed:
                raise AssertionError("还在问第 %d 次：%s" % (asked["n"], prompt))
            return feed.pop(0)

        real_input = builtins.input
        builtins.input = fake_input
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                action, bvids = collect_for_case(1, 1, "测试歌", "自测用例",
                                                 False, max_rounds)
        except AssertionError as exc:
            # 按键喂完了还在问 —— 多半是该停的时候没停
            return "stuck", None, len(feed), "%s\n  %s" % (out.getvalue(), exc)
        finally:
            builtins.input = real_input
        return action, bvids, len(feed), out.getvalue()

    A, B, C = "BV1Aa1111111", "BV1Bb2222222", "BV1Cc3333333"

    # 1. 粘满 3 次：收 3 个，配额用完就停（输入恰好耗尽，没多问）
    action, bvids, leftover, _ = run_lines([A, "", "", B, "", "", C, "", ""])
    record("粘满 3 次收 3 个", action == "ok" and bvids == [A, B, C],
           "%s / %s" % (action, bvids))
    record("达到上限即停止提问", action == "ok" and leftover == 0,
           "%s / 还剩 %d 条按键没用到" % (action, leftover))

    # 2. 第一轮粘完直接空行 -> 提前收工
    action, bvids, leftover, _ = run_lines([A, "", "", ""])
    record("空行可提前收工", action == "ok" and bvids == [A],
           "%s / %s" % (action, bvids))

    # 3. 多轮有重复 -> 合并去重（最后一轮空行收工）
    action, bvids, leftover, _ = run_lines([A, "", "", A + " " + B, "", "", ""])
    record("多轮合并去重", action == "ok" and bvids == [A, B], str(bvids))

    # 4. 什么都没粘的空行不算次数，提示后接着来
    action, bvids, leftover, _ = run_lines(["", A, "", "", ""])
    record("空粘贴不占次数", action == "ok" and bvids == [A], str(bvids))

    # 5. 认不出 BV 号 -> 提示后按 s 跳过
    action, bvids, leftover, out = run_lines(["这不是链接", "", "s"])
    record("认不出链接可跳过", action == "skip", str(action))

    # 6. 命令仍然有效；但已经收过东西时的 s 按收工算，不吃掉前面的成果
    action, bvids, leftover, _ = run_lines([A, "", "", "s"])
    record("已收货后按 s 不丢成果", action == "ok" and bvids == [A],
           "%s / %s" % (action, bvids))
    record("d = 判用例无效", run_lines(["d"])[0] == "drop")
    record("s = 跳过", run_lines(["s"])[0] == "skip")
    record("q = 结束保存", run_lines([A, "", "", "q"])[0] == "quit")

    # 7. 删序号：粘两个删掉第 2 个
    action, bvids, leftover, _ = run_lines([A + " " + B, "", "2", "", ""])
    record("删序号生效", action == "ok" and bvids == [A], str(bvids))

    # 8. --rounds 能改上限：上限设 1 时，粘 1 次就收工
    action, bvids, leftover, _ = run_lines([A, "", ""], max_rounds=1)
    record("--rounds=1 时只粘一次", action == "ok" and bvids == [A], str(bvids))

    time.sleep, ns["bvid_to_info"] = real_sleep, real_info

    print("=" * 72)
    print("eval_confirm 自测（离线，不联网不弹浏览器）")
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
    argv = sys.argv[1:]
    start = 0
    only = None
    rounds = MAX_PASTE_ROUNDS
    use_browser = "--no-browser" not in argv
    do_apply = "--apply" in argv
    for arg in argv:
        if arg.startswith("--start="):
            start = int(arg.split("=", 1)[1])
        elif arg.startswith("--only="):
            only = arg.split("=", 1)[1]
        elif arg.startswith("--rounds="):
            rounds = int(arg.split("=", 1)[1])
        elif arg.startswith("--start"):
            idx = argv.index(arg)
            start = int(argv[idx + 1]) if idx + 1 < len(argv) else 0
    if rounds < 1:
        rounds = 1

    cases = eval_search.CASES
    data = load_confirmed()

    pending = []
    for keyword, expected, note in cases:
        if only and only not in keyword:
            continue
        if keyword in data and (data[keyword].get("bvids")
                                or data[keyword].get("dropped")):
            continue                      # 已确认过的不再问
        pending.append((keyword, note))
    pending = pending[start:]

    if not pending:
        print("没有待确认的用例了。想重来就删掉 %s。"
              % os.path.basename(JSON_FILE))
        if do_apply and data:
            apply_to_eval(data)
        return 0

    print("=" * 72)
    print("待确认 %d 条%s" % (len(pending),
                            "（还有 %d 条之前已确认）" % (len(data) - len(pending))
                            if len(data) > len(pending) else ""))
    print("每条最多粘 %d 次，多轮结果合并；空行可提前收工。"
          "评测时命中其中任意一个即通过。" % rounds)
    print("=" * 72)

    for offset, (keyword, note) in enumerate(pending, 1):
        action, bvids = collect_for_case(offset, len(pending), keyword, note,
                                         use_browser, rounds)
        if action == "quit":
            save_confirmed(data)
            write_markdown(data, cases)
            print("\n已保存 %d 条 -> %s" % (len(data), JSON_FILE))
            return 0
        if action == "skip":
            print("  跳过。")
            continue
        if action == "drop":
            data[keyword] = {"dropped": True, "bvids": []}
            print("  已标记为无效用例。")
            save_confirmed(data)
            continue

        data[keyword] = {"bvids": bvids, "dropped": False}
        save_confirmed(data)
        print("  [OK] %s -> %s（命中其一即通过）"
              % (keyword, ", ".join(bvids)))

    save_confirmed(data)
    write_markdown(data, cases)

    done = sum(1 for v in data.values() if v.get("bvids") or v.get("dropped"))
    print()
    print("=" * 72)
    print("已确认 %d 条，写入 %s" % (done, JSON_FILE))
    print("可读版：%s" % MD_FILE)
    remaining = len(cases) - done
    if remaining:
        print("还剩 %d 条没确认，下次跑 `python eval_confirm.py` 接着来。" % remaining)
    print("=" * 72)

    if do_apply:
        apply_to_eval(data)
    else:
        print("确认无误后跑 `python eval_confirm.py --apply` 写回 eval_search.py。")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
