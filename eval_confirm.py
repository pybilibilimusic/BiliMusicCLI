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
    python eval_confirm.py --no-browser    # 不自动开浏览器（自己已经开着了）
    python eval_confirm.py --apply         # 确认完顺手写回 eval_search.py

逐条流程：
    1. 自动打开 B 站搜索页（关键词就是评测用例的查询词）
    2. 你在浏览器里挑出确实是这首歌的视频，把链接复制回来粘贴
       —— 整段乱贴也行，支持完整网址、b23.tv 短链、纯 BV 号、av 号混着来
    3. 脚本洗出 BV 号，并调 B 站接口把标题查出来给你二次核对
    4. 确认后记录，结束后写入 eval_confirmed.json（+ 可读版 eval_confirmed.md）

粘贴时的命令（单独一行输入）：
    s   跳过这条，下次再处理
    d   这条用例无效（B 站上压根没有这首歌 / 用例本身不合理）
    q   结束并把已经确认的写进文件

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


def one_case(index, total, keyword, note, use_browser):
    print()
    print("=" * 72)
    print("[%d/%d] %s" % (index, total, keyword))
    if note:
        print("       %s" % note)

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
    print("  粘完按一次回车、再按一次空回车结束。")
    print("  单独输入 s=跳过  d=这条用例无效  q=结束保存")

    blob, command = read_paste()
    return blob, command


def confirm_bvids(keyword, bvids):
    """把 BV 号对应的标题查出来给人核对，允许删掉查错的。"""
    while True:
        print("\n  洗出 %d 个 BV 号，调接口核对标题：" % len(bvids))
        infos = []
        for bvid in bvids:
            info = bvid_to_info(bvid)
            infos.append(info)
            if info:
                print("    [%d] %s  %s  | %s | UP %s"
                      % (len(infos), bvid, fmt_duration(info["duration"]),
                         info["title"][:44], info["uploader"][:16]))
            else:
                print("    [%d] %s  （接口查不到，可能已失效）" % (len(infos), bvid))
            time.sleep(0.3)

        print("\n  Y = 全部采纳；输入序号（如 2 或 2,3）删掉不对的；"
              "r = 重新粘贴；s = 跳过这条")
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


def main():
    argv = sys.argv[1:]
    start = 0
    only = None
    use_browser = "--no-browser" not in argv
    do_apply = "--apply" in argv
    for arg in argv:
        if arg.startswith("--start="):
            start = int(arg.split("=", 1)[1])
        elif arg.startswith("--only="):
            only = arg.split("=", 1)[1]
        elif arg.startswith("--start"):
            idx = argv.index(arg)
            start = int(argv[idx + 1]) if idx + 1 < len(argv) else 0

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
    print("=" * 72)

    for offset, (keyword, note) in enumerate(pending, 1):
        while True:
            blob, command = one_case(offset, len(pending), keyword, note,
                                     use_browser)
            if command == "q":
                save_confirmed(data)
                write_markdown(data, cases)
                print("\n已保存 %d 条 -> %s" % (len(data), JSON_FILE))
                return 0
            if command == "s":
                print("  跳过。")
                break
            if command == "d":
                data[keyword] = {"dropped": True, "bvids": []}
                print("  已标记为无效用例。")
                save_confirmed(data)
                break

            bvids = extract_bvids(blob)
            if not bvids:
                print("  没洗出任何 BV 号。检查下粘的是不是 B 站链接？"
                      "（r=重来 s=跳过）")
                again = input("  > ").strip().lower()
                if again == "r":
                    continue
                if again == "s":
                    break
                continue

            kept, action = confirm_bvids(keyword, bvids)
            if action == "retry":
                continue
            if action == "skip":
                break
            data[keyword] = {"bvids": kept, "dropped": False}
            save_confirmed(data)
            print("  [OK] %s -> %s" % (keyword, ", ".join(kept)))
            break

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
    sys.exit(main())
