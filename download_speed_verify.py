"""独立验证脚本：downloading.avg_speed_mbps 的除零保护（离线，秒跑）。

  python download_speed_verify.py             # 全量断言
  python download_speed_verify.py --mutation  # A4 变异测试：旧公式必须变红

背景：2026-11-15 冒烟测试里「单线程下载」偶发
      `float division by zero` —— 本机 HTTP 服务器太快，total_time 落在
      Windows time.time() 的一个计时滴答内变成 0，异常冒到 download()
      后文件被当成不完整产物删掉，下载被误判失败。
"""
import math
import sys

import downloading

N = [0]
FAILED = []


def check(label, cond, detail=""):
    N[0] += 1
    if cond:
        print("  [OK]   %s" % label)
    else:
        FAILED.append(label)
        print("  [FAIL] %s  %s" % (label, detail))


def mutation():
    """A4：证明新用例在旧代码上确实会变红。"""
    def old_formula(downloaded, total_time):
        return downloaded / total_time / 1024 / 1024

    try:
        old_formula(716800, 0)
    except ZeroDivisionError:
        print("  [OK]   旧公式在 total_time=0 抛 ZeroDivisionError"
              "（新回归用例能抓住这个 bug）")
        return 0
    print("  [FAIL] 旧公式没变红，用例无效")
    return 1


def run():
    print("=" * 62)
    print("downloading 平均速度除零保护 离线验证")
    print("=" * 62)
    f = downloading.avg_speed_mbps

    check("正常值 2MB / 1s = 2.0 MB/s",
          abs(f(2 * 1024 * 1024, 1.0) - 2.0) < 1e-9,
          repr(f(2 * 1024 * 1024, 1.0)))
    check("0.5s 翻倍", abs(f(1024 * 1024, 0.5) - 2.0) < 1e-9)

    # 核心回归：total_time 为 0 不能抛异常
    try:
        v = f(716800, 0)
        check("total_time=0 不抛异常且返回 0", v == 0.0, repr(v))
    except Exception as e:
        check("total_time=0 不抛异常且返回 0", False, repr(e))

    check("total_time=0.0（浮点）同样安全", f(100, 0.0) == 0.0)
    check("total_time=None 安全", f(100, None) == 0.0)
    check("total_time 为负数安全", f(100, -1.0) == 0.0)
    check("downloaded=0 返回 0", f(0, 1.0) == 0.0)

    v = f(100, 1e-9)
    check("极小正时间返回有限值（不抛、不是 nan/inf）",
          math.isfinite(v), repr(v))

    # 防止「只改了一处、另一处漏掉」
    src = open(downloading.__file__, encoding="utf-8-sig").read()
    check("两处完成提示都改用了安全函数",
          src.count("avg_speed_mbps(") >= 3,
          "出现 %d 次（应为 定义1 + 调用2）" % src.count("avg_speed_mbps("))
    check("原来的裸除法已从 print 里消失",
          "/ total_time / 1024 / 1024:.2f" not in src)

    print("-" * 62)
    if "--mutation" in sys.argv:
        print("A4 变异测试（旧代码必须变红）：")
        return 1 if mutation() or FAILED else 0
    print("结果：%d/%d 通过" % (N[0] - len(FAILED), N[0]))
    if FAILED:
        for x in FAILED:
            print("  失败：%s" % x)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
