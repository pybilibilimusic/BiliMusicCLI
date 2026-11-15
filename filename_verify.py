"""
独立验证脚本：B站音频文件名里的 studio-video-* 占位符回退。

    python filename_verify.py              # 回归用例
    python filename_verify.py --mutation   # A4 变异测试：确认用例在旧逻辑上会变红
    python filename_verify.py --demo       # 打印一组对照表

只测纯函数 download_audio.resolve_song_filename()，不联网、不碰 B站 API。
"""

import argparse
import sys

import utils
from download_audio import (is_studio_placeholder, resolve_song_filename)

# 验收用例：(标签, title, part, bvid, page, multi_part, 期望文件名主体)
CASES = [
    ("普通单P", "晴天 - 周杰伦", "", "BV1aaa", 1, False, "晴天 - 周杰伦"),
    ("普通多P", "晴天", "伴奏版", "BV1aaa", 2, True, "晴天 - 伴奏版"),
    ("普通多P(第1P)", "起风了", "原唱版", "BV1bbb", 1, True, "起风了 - 原唱版"),
    ("单P part 占位符", "花海", "studio_video_1730000000", "BV1ccc", 1, False, "花海"),
    ("单P title 也占位符", "studio_video_1730000000",
     "studio-video-1730000001", "BV1ddd", 1, False, "BV1ddd"),
    ("单P title 为空", "", "studio-video-1730000002", "BV1eee", 1, False, "BV1eee"),
    ("多P part 占位符", "雨爱", "studio_video_1730000003", "BV1fff", 1, True, "雨爱_P1"),
    ("多P part 占位符(第3P)", "雨爱", "STUDIO-VIDEO-1730000004",
     "BV1fff", 3, True, "雨爱_P3"),
    ("多P title 也异常", "studio_video_1730000005", "studio_video_1730000006",
     "BV1ggg", 2, True, "BV1ggg_P2"),
    ("占位符无分隔符", "稻香", "STUDIO_VIDEO1730000007", "BV1hhh", 1, False, "稻香"),
]


def legacy_filename(title, part, bvid, page=1, multi_part=False):
    """改动**之前**的命名逻辑，仅用于 A4 变异测试。"""
    return f"{title} - {part}" if part and part != title else title


def run_cases(resolve, verbose=True):
    passed = failed = 0
    results = []
    for label, title, part, bvid, page, multi, expected in CASES:
        got = resolve(title, part, bvid, page=page, multi_part=multi)
        ok = got == expected
        passed += ok
        failed += (not ok)
        results.append((label, got, expected, ok))
        if verbose:
            mark = "OK  " if ok else "FAIL"
            print("  [%s] %-22s -> %r%s" % (
                mark, label, got,
                "" if ok else "   (期望 %r)" % expected))
    return passed, failed, results


def test_placeholder_detection():
    print("\n[2] 占位符识别（不区分大小写）")
    positives = ["studio_video_1730000000", "studio-video-1730000000",
                 "STUDIO_VIDEO1730000001", "Studio-Video_1730000002",
                 "前缀 studio_video_999 后缀"]
    negatives = ["", None, "伴奏版", "P1 正式版", "studio_video",
                 "晴天 - 周杰伦", "studio video 123"]
    ok = True
    for text in positives:
        hit = is_studio_placeholder(text)
        print("  [%s] 命中 %r" % ("OK  " if hit else "FAIL", text))
        ok &= hit
    for text in negatives:
        hit = is_studio_placeholder(text)
        print("  [%s] 不误伤 %r" % ("OK  " if not hit else "FAIL", text))
        ok &= not hit
    return ok


def test_uniqueness_and_cleanup():
    print("\n[3] 文件名唯一 + 非法字符清理保持原逻辑")
    ok = True
    # 同一多P视频：一个分P是占位符，另一个正常
    a = resolve_song_filename("雨爱", "studio_video_1730000000", "BV1fff",
                              page=1, multi_part=True)
    b = resolve_song_filename("雨爱", "伴奏版", "BV1fff",
                              page=2, multi_part=True)
    print("  P1 = %r" % a)
    print("  P2 = %r" % b)
    unique = a != b
    print("  [%s] 两个分P文件名唯一" % ("OK  " if unique else "FAIL"))
    ok &= unique
    no_placeholder = not is_studio_placeholder(a) and not is_studio_placeholder(b)
    print("  [%s] 都不含占位符" % ("OK  " if no_placeholder else "FAIL"))
    ok &= no_placeholder

    # normalize_filename 仍是原来的函数，且能处理带特殊字符的标题
    weird = resolve_song_filename("晴:天/周*杰?伦", "studio_video_123",
                                  "BV1iii", 1, False)
    cleaned = utils.normalize_filename(weird)
    illegal = set('<>:"/\\|?*')
    clean_ok = not illegal.intersection(cleaned)
    print("  [%s] 非法字符清理后 %r" % ("OK  " if clean_ok else "FAIL", cleaned))
    ok &= clean_ok
    return ok


def mutation():
    """A4：确认新增用例在旧代码上会变红，否则用例可能没测到这个 bug。"""
    print("=" * 66)
    print("A4 变异测试：把 resolve_song_filename 换成改动前的逻辑")
    print("=" * 66)
    print("\n[改动后的逻辑]")
    p1, f1, _ = run_cases(resolve_song_filename)
    print("\n[改动前的逻辑]")
    p2, f2, _ = run_cases(legacy_filename)
    print("\n  改动后：%d 通过 / %d 失败" % (p1, f1))
    print("  改动前：%d 通过 / %d 失败" % (p2, f2))
    ok = (f1 == 0) and (f2 >= 3)
    print("\n  [%s] 用例在新代码全绿、在旧代码至少 3 条变红（A4）"
          % ("OK  " if ok else "FAIL"))
    return 0 if ok else 1


def demo():
    print("=" * 66)
    print("占位符回退对照表")
    print("=" * 66)
    print("  %-22s %-30s %s" % ("场景", "输入 part", "输出文件名"))
    print("  " + "-" * 62)
    for label, title, part, bvid, page, multi, _ in CASES:
        got = resolve_song_filename(title, part, bvid, page=page,
                                    multi_part=multi)
        shown = (part[:28] + "…") if len(part) > 28 else (part or "(空)")
        print("  %-22s %-30s %s" % (label, shown, got))
    return 0


def main():
    ap = argparse.ArgumentParser(description="文件名占位符回退验证")
    ap.add_argument("--mutation", action="store_true", help="A4 变异测试")
    ap.add_argument("--demo", action="store_true", help="打印对照表")
    args = ap.parse_args()
    if args.mutation:
        return mutation()
    if args.demo:
        return demo()

    print("文件名占位符回退 filename_verify.py —— 离线验证")
    print("\n[1] 命名回归用例")
    passed, failed, _ = run_cases(resolve_song_filename)
    ok = test_placeholder_detection()
    ok &= test_uniqueness_and_cleanup()
    print("\n" + "=" * 46)
    print("通过 %d / %d 条用例" % (passed, passed + failed))
    if failed or not ok:
        print("有失败项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
