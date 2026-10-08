"""
搜索排序评测脚本（TODO 里 P0「检索准确率评测集」的种子版本）。

用法：
    python eval_search.py                # 跑全部用例
    python eval_search.py 花海           # 只跑包含指定关键词的用例

用例格式：
    (查询词, 期望 BV 号集合, 备注)
同一首歌在 B 站往往有多个可接受的官方版本，
所以期望值用「集合」，命中其中任意一个即判通过。

两种模式：
    python eval_search.py            # 只对已确认期望值的用例打分
    python eval_search.py --audit    # 跑全部用例，把 Top5 打出来供人工核对

⚠️ 期望值必须人工确认过，不要用程序跑出来的 Top1 反过来填期望值 ——
   那样准确率必然是 100%，这个评测集就彻底没意义了。
   先 --audit 看结果，人工确认哪些 BV 号算对，再填进 CASES。

想扩充评测集，往 CASES 里加一行即可。
"""

import os
import sys
import time

# 本脚本在 review/ 子目录，被测模块（song_search 等）在项目根
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from song_search import SongSearch

# --audit 的核对表落到哪儿
AUDIT_FILE = os.path.join(HERE, "eval_audit.md")

# (查询词, 期望 BV 集合, 备注)
#
# 期望集合为空 = 还没人工确认，打分模式会跳过它。
# 确认办法：先 --audit 把 Top5 打出来，人工看哪些 BV 号确实是这首歌，再填进来。
CASES = [
    # ---- 已确认 ----
    ("约会 音乐", {"BV1JW411D7rX"}, "RADWIMPS - デート《约会》"),
    ("花海", {"BV1MwYg6tEsR"}, "周杰伦《花海》"),
    ("雨爱", {"BV118411B7XU", "BV1yeiiBaEVr"},
     "杨丞琳《雨爱》，两版均可（前者 1600 万播放，后者 Hi-Res 滚动歌词版）"),

    # ---- 待人工确认（--audit 跑完后填）----
    # 选词思路：难度要有梯度，别全是「搜一下就中」的简单题。
    ("晴天", set(), "周杰伦《晴天》，翻唱和二创极多"),
    ("稻香", set(), "周杰伦《稻香》"),
    ("七里香", set(), "周杰伦《七里香》"),
    ("起风了", set(), "买辣椒也用券《起风了》，原曲是高桥优《ヤキモチ》"),
    ("芒种", set(), "音阙诗听 / 赵方婧《芒种》"),
    ("孤勇者", set(), "陈奕迅《孤勇者》"),
    ("漠河舞厅", set(), "柳爽《漠河舞厅》"),
    ("海阔天空", set(), "Beyond《海阔天空》—— 与信乐团同名曲容易混"),
    ("演员", set(), "薛之谦《演员》—— 纯通用词，考验噪声过滤"),
    ("成都", set(), "赵雷《成都》—— 地名，容易混进旅行 vlog"),
    ("南山南", set(), "马頔《南山南》"),
    ("夜空中最亮的星", set(), "逃跑计划"),
    ("平凡之路", set(), "朴树《平凡之路》"),
    ("千千阙歌", set(), "陈慧娴，粤语经典"),
    ("打上花火", set(), "DAOKO × 米津玄师《打上花火》"),
    ("Lemon 米津玄师", set(), "米津玄师《Lemon》"),
    ("恋爱循环", set(), "千石抚子《恋爱循环》，日文原曲"),
    ("Shape of You", set(), "Ed Sheeran"),
    ("Numb Linkin Park", set(), "Linkin Park《Numb》"),
    ("生日快乐歌", set(), "超通用词，最容易混进无关视频"),
    ("我和我的祖国", set(), "红歌，容易被合唱 / 晚会视频淹没"),

    # ---- 难题：热门歌基本都能搜对，真正拉开差距的是这几种 ----
    ("回家", set(), "超通用词 —— 歌手多、同名曲多，还容易混进 vlog"),
    ("勇气", set(), "同名曲多，默认是梁静茹那首"),
    ("传奇", set(), "王菲 / 李健两个版本都很常见，强混淆"),
    ("朋友", set(), "周华健 vs 无印良品，都是常见结果"),
    ("大鱼", set(), "周深《大鱼》，和《大鱼海棠》相关二创容易混"),
    ("囍", set(), "葛东琪《囍》，单字歌名 + 同名民俗内容多"),
    # ⚠️ 填 BV 号前务必核实它确实对应这首歌：
    #    python -c "import requests,config;print(requests.get(
    #    'https://api.bilibili.com/x/web-interface/view?bvid=<BV号>',
    #    headers=config.headers).json()['data']['title'])"
    # 不要凭印象填 BV 号 —— 曾经有人（就是我）填错了还以为是程序的问题。
]


def run_one(keyword: str, expected: set, note: str = "") -> dict:
    searcher = SongSearch(prompt=keyword, timeout=0)
    started = time.time()
    bvid, title, candidates = searcher.search()
    elapsed = time.time() - started

    rank = None
    for index, item in enumerate(candidates, 1):
        if item["bvid"] in expected:
            rank = index
            break

    return {
        "keyword": keyword,
        "note": note,
        "top1": bvid,
        "top1_title": title,
        "expected": expected,
        "rank": rank,
        "passed": bvid in expected if bvid else False,
        "elapsed": elapsed,
        "candidates": candidates,
    }


def audit(cases) -> int:
    """
    人工核对模式：不打分数，把每个查询的 Top5 列出来。

    用途是确认「Top1 到底是不是这首歌」，确认完再把 BV 号填进 CASES。
    没这一步就只能靠印象填期望值，而那正是最容易填错的做法。
    """
    print("人工核对模式：只列结果，不打分。\n")
    rows = []
    for keyword, expected, note in cases:
        print(f"正在检索：{keyword}")
        rows.append(run_one(keyword, expected, note))
        time.sleep(0.5)

    print("\n" + "=" * 96)
    lines = ["# 搜索结果人工核对表", "",
             "跑出来的 Top5 列在这儿。逐条确认哪些 BV 号确实是这首歌，",
             "把挑中的填到每节末尾的「我确认的 BV 号」一行。",
             "（这份文件每次 --audit 都会重写，确认结果请填进 eval_search.py 的 CASES。）",
             ""]
    for row in rows:
        flag = "已确认" if row["expected"] else "待确认"
        print(f"\n[{flag}] {row['keyword']}"
              + (f"  —— {row['note']}" if row["note"] else ""))
        lines.append("## %s  `%s`" % (row["keyword"], flag))
        if row["note"]:
            lines.append("")
            lines.append("> %s" % row["note"])
        lines.append("")
        lines.append("| # | BV 号 | 时长 | 清洗后标题 | 原标题 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for index, item in enumerate(row["candidates"][:5], 1):
            seconds = item.get("duration") or 0
            clean = (item.get("clean") or "").replace("|", "\\|")[:40]
            raw = (item.get("title") or "").replace("|", "\\|")[:46]
            stamp = "%d:%02d" % (seconds // 60, seconds % 60)
            print(f"   {index}. {item['bvid']}  {stamp}  | {clean}")
            lines.append("| %d | `%s` | %s | %s | %s |"
                         % (index, item["bvid"], stamp, clean, raw))
        lines.append("")
        lines.append("**我确认的 BV 号：** ")
        lines.append("")

    try:
        with open(AUDIT_FILE, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        print("\n核对表已写入：%s" % AUDIT_FILE)
    except OSError as exc:
        print("写入核对表失败：%s" % exc)

    print("\n" + "=" * 96)
    print("把这些 BV 号里确实是这首歌的挑出来，填进 eval_search.py 的 CASES。")
    print("填完再跑 `python eval_search.py` 才有分数。")
    return 0


def main() -> int:
    argv = [a for a in sys.argv[1:]]
    audit_mode = "--audit" in argv
    keyword_filter = next((a for a in argv if not a.startswith("--")), None)

    cases = [c for c in CASES if not keyword_filter or keyword_filter in c[0]]
    if not cases:
        print(f"没有匹配「{keyword_filter}」的用例。")
        return 1

    if audit_mode:
        return audit(cases)

    pending = [c for c in cases if not c[1]]
    cases = [c for c in cases if c[1]]
    if pending:
        print(f"跳过 {len(pending)} 个还没确认期望值的用例："
              f"{', '.join(c[0] for c in pending)}")
        print("先跑 `python eval_search.py --audit` 人工确认，再填进 CASES。\n")
    if not cases:
        return 1

    results = []
    print(f"共 {len(cases)} 个用例，开始评测...\n")
    for keyword, expected, note in cases:
        print(f"正在检索：{keyword}")
        results.append(run_one(keyword, expected, note))
        time.sleep(0.5)          # 避免请求过于频繁

    print("\n" + "=" * 78)
    passed = 0
    for result in results:
        flag = "PASS" if result["passed"] else "FAIL"
        passed += result["passed"]
        print(f"\n[{flag}] {result['keyword']}"
              + (f"  —— {result['note']}" if result["note"] else ""))
        print(f"       Top1  : {result['top1']}  ({result['elapsed']:.1f}s)")
        print(f"       标题  : {result['top1_title']}")
        print(f"       期望  : {', '.join(sorted(result['expected']))}")
        print(f"       命中排名: {result['rank'] or '未进入候选'}")

        print("       Top5 候选：")
        for index, item in enumerate(result["candidates"][:5], 1):
            mark = "   <<< 期望项" if item["bvid"] in result["expected"] else ""
            quality = ",".join(item.get("quality") or []) or "-"
            print(f"         {index}. [{item['score']:>6}] {item['bvid']} "
                  f"| 音质词 {quality:<12} | {item.get('clean', '')[:36]}{mark}")

    print("\n" + "=" * 78)
    total = len(results)
    print(f"结果：{passed}/{total} 通过，Top-1 准确率 {passed / total * 100:.0f}%")
    missed = [r["keyword"] for r in results if not r["passed"]]
    if missed:
        print(f"未命中：{', '.join(missed)}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
