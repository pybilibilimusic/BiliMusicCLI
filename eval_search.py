"""
搜索排序评测脚本（TODO 里 P0「检索准确率评测集」的种子版本）。

用法：
    python eval_search.py                # 跑全部用例
    python eval_search.py 花海           # 只跑包含指定关键词的用例

用例格式：
    (查询词, 期望 BV 号集合, 备注)
同一首歌在 B 站往往有多个可接受的官方版本，
所以期望值用「集合」，命中其中任意一个即判通过。

想扩充评测集，往 CASES 里加一行即可。
"""

import sys
import time

from song_search import SongSearch

# (查询词, 期望 BV 集合, 备注)
CASES = [
    ("约会 音乐", {"BV1JW411D7rX"}, "RADWIMPS - デート《约会》"),
    ("花海", {"BV1MwYg6tEsR"}, "周杰伦《花海》"),
    ("雨爱", {"BV118411B7XU", "BV1yeiiBaEVr"},
     "杨丞琳《雨爱》，两版均可（前者 1600 万播放，后者 Hi-Res 滚动歌词版）"),
    # ⚠️ 新增用例务必先核实 BV 号确实对应这首歌：
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


def main() -> int:
    keyword_filter = sys.argv[1] if len(sys.argv) > 1 else None
    cases = [c for c in CASES if not keyword_filter or keyword_filter in c[0]]

    if not cases:
        print(f"没有匹配「{keyword_filter}」的用例。")
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
