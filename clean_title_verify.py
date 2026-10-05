"""
独立验证脚本：分层歌名清洗。

逻辑全部来自 title_cleaner.py，这里只负责跑用例、报结果。
用来在修改清洗规则后快速确认「有没有改坏」，可以直接运行：

    python clean_title_verify.py
"""

from title_cleaner import clean_song_title, title_segments, query_core

# (原始标题, 结果里必须包含的关键词, 结果里不允许出现的字符)
CASES = [
    ("RADWIMPS - デート《约会》", ["约会"], "《》"),
    ("《花海》周杰伦丨百万级录音棚试听丨【Hi-Res无损】", ["花海", "周杰伦"], "《》【】丨"),
    ("【4K60FPS】晴天 - 周杰伦 (无损音质)", ["晴天", "周杰伦"], "【】()"),
    ("晴天 | 周杰伦 | 动态歌词", ["晴天"], "|"),
    ("起风了 · 买辣椒也用券 · 无损Hi-Res", ["起风"], "·"),
    ("Windy Hill ｜ 羽肿 ｜ 动态歌词PV", ["Windy Hill"], "｜"),
    ("孤勇者 - 陈奕迅「爱你孤身走暗巷」", ["孤勇者"], "「」"),
    ("【钢琴版】起风了 - 买辣椒也用券", ["起风了"], "【】"),
    ("夜空中最亮的星『逃跑计划』8K HDR", ["星"], "『』"),
    ("周杰伦《七里香》Official MV", ["七里香"], "《》"),
    ("成都 - 赵雷 录音室版本", ["成都"], ""),
    ("Lemon 米津玄師 无损音质 60FPS", ["Lemon"], ""),
    ("", [], ""),
    ("【Hi-Res无损】", [], "【】"),
    ("花海", ["花海"], ""),
    # 下面几条来自真实搜索结果，用来压极端情况
    ("【无损Hi-Res】周杰伦《花海》“不要你离开，距离隔不开，思念变成海，在窗外进不来”-4K",
     ["花海"], "《》“”【】"),
    ("【𝐇𝐢-𝐑𝐞𝐬无损音质】｜《晴天》- 周杰伦 -‘故事的小黄花’",
     ["晴天"], "《》“”‘’【】|｜"),
    ("周杰伦《晴天》｜从前从前有个人爱你很久，但偏偏雨渐渐把距离吹得好远【Hi-Res无损音质】",
     ["晴天"], "《》｜【】，"),
    # ---- UP 主自造前缀（L2.5）----
    ("在百万豪装录音棚大声听 杨丞琳《雨爱》【Hi-res】", ["雨爱", "杨丞琳"], "《》【】"),
    ("在专业录音棚听《晴天》- 周杰伦", ["晴天"], "《》"),
    ("在录音棚里静静听 陈奕迅 - 孤勇者", ["孤勇者"], ""),
    ("【日推音乐】起风了 - 买辣椒也用券", ["起风了"], "【】"),
    ("每日推荐 | 成都 - 赵雷 | 无损音质", ["成都"], "|"),
    ("在百万级录音棚大声听《花海》周杰伦丨动态歌词", ["花海", "周杰伦"], "《》丨"),
    # 反向用例：以「在」开头但不是前缀话术，不能被误删
    ("在人间 - 王建房", ["在人间"], ""),
    ("在那遥远的地方", ["在那遥远"], ""),
    ("在你身边", ["在你身边"], ""),
]

# 文件名里绝对不能出现的符号（Windows 非法字符之外的杂符号）
BANNED_FILENAME_CHARS = "《》「」『』【】〈〉〖〗"


def main():
    print("=" * 78)
    print("分层歌名清洗 —— 独立验证（来源 title_cleaner.py）")
    print("=" * 78)

    all_pass = True
    for raw, must_have, forbidden in CASES:
        result = clean_song_title(raw)
        issues = []

        for keyword in must_have:
            if keyword not in result:
                issues.append(f"丢失关键词 {keyword!r}")
        for char in forbidden:
            if char in result:
                issues.append(f"残留符号 {char!r}")
        if "-" * 2 in result or " - " * 2 in result or result.startswith("-"):
            issues.append("存在连续或悬空分隔符")

        status = "OK " if not issues else "FAIL"
        all_pass = all_pass and not issues
        print(f"[{status}] {raw!r}")
        print(f"       -> {result!r}")
        if issues:
            print("       问题: " + "; ".join(issues))

    print("-" * 78)
    print("落地文件名检查（额外查杂符号）")
    for raw, _, _ in CASES:
        name = clean_song_title(raw)
        bad = [ch for ch in BANNED_FILENAME_CHARS if ch in name]
        if bad:
            all_pass = False
            print(f"[FAIL] {raw!r} -> {name!r} 残留 {bad}")
    if all_pass:
        print("       全部通过")

    print("-" * 78)
    print("分段 / 查询核心词检查")
    seg_cases = [
        ("RADWIMPS - デート《约会》", ["约会"]),
        ("《花海》周杰伦丨百万级录音棚试听丨【Hi-Res无损】", ["花海", "周杰伦"]),
        ("约会大作战背景音乐 - Ground Zero", ["约会大作战背景音乐"]),
    ]
    for raw, expected in seg_cases:
        segments = title_segments(raw)
        print(f"       {raw!r}")
        print(f"         -> 分段 {segments}")
        missing = [item for item in expected if item not in segments]
        if missing:
            all_pass = False
            print(f"         [FAIL] 缺少分段 {missing}")

    print(f"       query_core('约会 音乐') = {query_core('约会 音乐')!r}")
    print(f"       query_core('晴天 音乐') = {query_core('晴天 音乐')!r}")

    print("=" * 78)
    print("总体结果:", "全部通过" if all_pass else "有失败项")
    print("=" * 78)
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
