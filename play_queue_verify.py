"""
独立验证脚本：播放队列（play_queue.py）。

只测纯逻辑，不碰 mpv / 网络 / 数据库，也不改主程序。跑法：

    python play_queue_verify.py           # 跑全部用例
    python play_queue_verify.py --demo    # 看一段模拟操作的输出，判断手感
"""

import argparse
import random
import sys

from play_queue import (CURRENT_MARK, MAX_HISTORY, MODE_LOOP, MODE_SEQUENCE,
                        MODE_SHUFFLE, MODE_SINGLE, MODES, MODE_NAMES,
                        PlayQueue, QueueItem, _clip, _norm_path)

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK  ] {label}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


def mk(n=0, mode=MODE_SEQUENCE, seed=1, fallback=None):
    """造一个装了 n 首假歌的队列（第 1 首起编号）。"""
    q = PlayQueue(fallback=fallback, mode=mode, rng=random.Random(seed))
    for i in range(n):
        q.add(f"歌{i + 1}", f"mp3/song{i + 1}.mp3", bvid=f"BV{i + 1}")
    return q


# ---------------- 1. 加歌 ----------------

def test_add():
    print("\n[1] 加歌")
    q = mk(3)
    check("长度", len(q) == 3, str(len(q)))
    check("加在末尾", q.add("歌4", "mp3/s4.mp3") == 3)
    check("末尾那首对得上", q.items[3].title == "歌4")
    check("插到指定位置", q.add("插队", "mp3/x.mp3", position=1) == 1)
    check("插完顺序对", [i.title for i in q.items] == ["歌1", "插队", "歌2", "歌3", "歌4"])

    q2 = mk(3)
    q2.set_index(2)                       # 指针在第 3 首
    q2.add("前面插", "mp3/y.mp3", position=0)
    check("插在指针前面，指针跟着后移", q2.current_index == 3, str(q2.current_index))
    check("指针还是指着原来那首", q2.current.title == "歌3", q2.current.title)

    q3 = mk(0)
    check("空队列 add_next 进队头", q3.add_next("下一首", "mp3/n.mp3") == 0)
    q3.set_index(0)
    check("有指针时 add_next 插到后面", q3.add_next("再下一首", "mp3/n2.mp3") == 1)


# ---------------- 2. 删歌（重点：指针怎么动）----------------

def test_remove():
    print("\n[2] 删歌时的指针")
    q = mk(3)
    q.set_index(2)
    q.remove_at(0)
    check("删指针前面的 → 指针 -1", q.current_index == 1, str(q.current_index))
    check("指针仍然指着歌3", q.current.title == "歌3", q.current.title)

    q = mk(3)
    q.set_index(1)                         # 当前是歌2
    q.remove_at(1)                         # 删掉正在播的
    check("删当前这首 → 指针不动（后面的顶上来）", q.current_index == 1)
    check("顶上来的是歌3", q.current.title == "歌3", q.current.title)

    q = mk(3)
    q.set_index(2)                         # 当前是最后一首
    q.remove_at(2)
    check("删掉最后一首 → 退回游离态", q.current_index == -1, str(q.current_index))
    check("current 变 None", q.current is None)

    q = mk(3)
    check("越界删除返回 False", q.remove_at(3) is False)
    check("负数下标返回 False", q.remove_at(-1) is False)
    check("删完长度不变", len(q) == 3)

    q = mk(2)
    q.set_index(0)
    check("remove_current 删的是当前这首", q.remove_current() and q.current.title == "歌2")


# ---------------- 3. 移动 ----------------

def test_move():
    print("\n[3] 移动")
    q = mk(4)
    q.set_index(2)                         # 当前是歌3
    q.move(0, 2)
    check("把队头挪到指针后面，指针 -1", q.current_index == 1, str(q.current_index))
    check("指的还是歌3", q.current.title == "歌3", q.current.title)

    q = mk(4)
    q.set_index(2)
    q.move(3, 1)
    check("把队尾挪到指针前面，指针 +1", q.current_index == 3, str(q.current_index))
    check("指的还是歌3", q.current.title == "歌3", q.current.title)

    q = mk(4)
    q.set_index(0)
    q.move(0, 3)
    check("移动的就是当前这首 → 指针跟过去", q.current_index == 3, str(q.current_index))
    check("过去之后还是歌1", q.current.title == "歌1", q.current.title)

    q = mk(3)
    check("越界移动返回 False", q.move(0, 5) is False)
    check("越界后顺序没变", [i.title for i in q.items] == ["歌1", "歌2", "歌3"])


# ---------------- 4. 指针对齐 ----------------

def test_sync():
    print("\n[4] 指针对齐（sync_current）")
    q = mk(3)
    check("能对齐", q.sync_current("mp3/song2.mp3") is True)
    check("对齐后下标对", q.current_index == 1)

    q2 = mk(3)
    check("不在队列里返回 False", q2.sync_current("mp3/别处的歌.mp3") is False)
    check("不在队列里 → 游离态", q2.current_index == -1)

    q3 = mk(3)
    q3.set_index(0)
    check("对齐失败会把原来的指针清掉", q3.sync_current("mp3/别处.mp3") is False)
    check("指针确实是 -1", q3.current_index == -1)

    # 同一首歌，一次相对路径一次绝对路径，也应该算同一首
    q4 = PlayQueue()
    q4.add("雨爱", "mp3/雨爱.mp3")
    import os
    check("相对 / 绝对路径算同一首",
          q4.sync_current(os.path.abspath("mp3/雨爱.mp3")) is True)

    check("None 不会误匹配", q4.sync_current(None) is False)
    check("空串不会误匹配", q4.sync_current("") is False)


# ---------------- 5. 下一首 ----------------

def test_next():
    print("\n[5] 下一首")
    q = mk(3)
    check("游离态按下一首 → 从队头开始", q.next().title == "歌1")
    check("再按 → 歌2", q.next().title == "歌2")
    check("再按 → 歌3", q.next().title == "歌3")
    check("顺序模式到尾 → None（停下）", q.next() is None)
    check("停下时指针还在最后一首", q.current_index == 2)

    q = mk(3, mode=MODE_LOOP)
    for _ in range(3):
        q.next()
    check("循环模式到尾 → 回到队头", q.next().title == "歌1")
    check("指针回到 0", q.current_index == 0)

    q = mk(1)
    check("只有一首，下一首还是它", q.next().title == "歌1")
    check("只有一首也能一直 next", q.next() is not None)

    q = mk(4, mode=MODE_SINGLE)
    q.next()
    check("单曲：手动按下一首会真的换歌", q.next().title == "歌2")
    check("单曲：播完了还是自己", q.next(auto=True).title == "歌2")
    check("单曲 auto 不改变指针", q.current_index == 1)

    # 随机模式
    q = mk(5, mode=MODE_SHUFFLE, seed=42)
    q.set_index(0)
    seen = set()
    same_as_current = 0
    for _ in range(50):
        prev_idx = q.current_index
        item = q.next()
        seen.add(item.title)
        if q.current_index == prev_idx:
            same_as_current += 1
    check("随机模式从不原地不动", same_as_current == 0, f"{same_as_current} 次")
    check("随机模式能覆盖全部歌曲", len(seen) == 5, f"{len(seen)}/5")

    q = mk(1, mode=MODE_SHUFFLE)
    check("只有一首时随机也返回它", q.next().title == "歌1")

    # 顺序模式到尾之后，指针不该被改坏
    q = mk(2)
    q.next()
    q.next()
    check("到尾后再按，指针没被推走", q.next() is None and q.current_index == 1)


# ---------------- 6. 上一首 ----------------

def test_prev():
    print("\n[6] 上一首")
    q = mk(3)
    q.next()          # 歌1
    q.next()          # 歌2
    check("退到上一首", q.prev().title == "歌1")
    check("再退 → 历史空了退到列表前一首（已经是头，返回 None）", q.prev() is None)

    q = mk(3)
    q.next()
    q.next()
    q.next()          # 歌3
    check("从歌3 退到歌2", q.prev().title == "歌2")
    check("再退到歌1", q.prev().title == "歌1")

    q = mk(3, mode=MODE_LOOP)
    q.set_index(0)
    check("循环模式在队头往回退 → 队尾", q.prev().title == "歌3")

    q = mk(3)
    q.set_index(1)
    check("没有历史时，退到列表前一首", q.prev().title == "歌1")

    # 随机模式下「上一首」必须靠历史，不然回不去
    q = mk(5, mode=MODE_SHUFFLE, seed=7)
    a = q.next()
    b = q.next()
    back = q.prev()
    check("随机模式 prev 能回到刚才那首", back is not None and back.title == a.title,
          f"{a.title} -> {b.title} -> {back.title if back else None}")

    # 历史里那首被删了，应该继续往前翻而不是卡住
    q = mk(3)
    q.next()
    q.next()
    q.next()          # 现在在歌3，历史 [歌1, 歌2]
    q.remove_at(0)    # 把歌1 删掉（历史里最早那条）
    check("历史里的歌被删了 → 继续往前退到还在的那首", q.prev().title == "歌2")

    q2 = mk(3)
    q2.next()
    q2.next()
    q2.next()
    q2.remove_at(0)
    q2.remove_at(0)   # 历史里两首都没了
    check("历史整段被删光 → 不崩，退回列表前一首（没有就 None）", q2.prev() is None)

    q = mk(0)
    check("空队列 prev 返回 None", q.prev() is None)


# ---------------- 7. 队列空 / 兜底 ----------------

def test_fallback():
    print("\n[7] 队列空时的兜底")
    q = mk(0)
    check("没有兜底 → next 返回 None", q.next() is None)

    called = {"n": 0}

    def fb():
        called["n"] += 1
        return "mp3/随机.mp3"

    q = mk(0, fallback=fb)
    item = q.next()
    check("有兜底 → 返回兜底那首", item is not None and item.path == "mp3/随机.mp3")
    check("兜底被调了一次", called["n"] == 1)
    check("兜底的歌不会混进队列", len(q) == 0)
    check("兜底不影响指针", q.current_index == -1)

    q2 = mk(0, fallback=lambda: None)
    check("兜底也拿不到 → None", q2.next() is None)

    q3 = mk(0, fallback="我不是函数")
    check("fallback 不是函数也不会崩", q3.next() is None)

    # 队列里有歌就不该走兜底
    q4 = mk(2, fallback=fb)
    called["n"] = 0
    q4.next()
    check("队列非空时不碰兜底", called["n"] == 0)


# ---------------- 8. 模式 ----------------

def test_mode():
    print("\n[8] 播放模式")
    q = mk(1)
    check("默认顺序播放", q.mode == MODE_SEQUENCE)
    check("合法模式能设", q.set_mode(MODE_SHUFFLE) is True and q.mode == MODE_SHUFFLE)
    check("非法模式被拒", q.set_mode("乱写") is False)
    check("被拒后模式没变", q.mode == MODE_SHUFFLE)

    q2 = mk(1)
    order = [q2.cycle_mode() for _ in range(4)]
    check("切四次回到原点", order[0] == MODE_LOOP and order[3] == MODE_SEQUENCE,
          " → ".join(MODE_NAMES[m] for m in order))
    check("四种模式都有中文名", all(m in MODE_NAMES for m in MODES))


# ---------------- 9. 渲染 ----------------

def test_render():
    print("\n[9] 列表渲染")
    q = mk(3)
    q.set_index(1)
    rows = q.format_list()
    check("行数 = 歌曲数", len(rows) == 3, str(len(rows)))
    check("当前那行有标记", rows[1].startswith(CURRENT_MARK), rows[1])
    check("其它行没有标记", not rows[0].startswith(CURRENT_MARK), rows[0])
    check("编号从 1 起", rows[0].strip().startswith("1."), rows[0].strip())
    check("标记是纯 ASCII", all(ord(c) < 128 for c in CURRENT_MARK), CURRENT_MARK)

    check("空队列有提示", mk(0).format_list() == ["（队列是空的）"])
    q_no_title = PlayQueue()
    q_no_title.add("", "mp3/abc.mp3")
    check("没标题时退回文件名", q_no_title.items[0].display == "abc.mp3",
          q_no_title.items[0].display)

    long = PlayQueue()
    long.add("一个非常非常长的歌名用来测试截断效果一二三四五六七八", "mp3/x.mp3")
    row = long.format_list(width=20)[0]
    check("超宽会截断", len(row) <= 21, f"{len(row)} 字符: {row}")

    q = mk(20)
    q.set_index(15)
    rows = q.format_list(max_rows=5)
    check("开窗后行数受控", len(rows) <= 7, str(len(rows)))
    check("开窗时当前那首一定在", any(CURRENT_MARK in r for r in rows))
    check("开头提示前面还有几首", "前面还有" in rows[0], rows[0])

    q = mk(20)
    q.set_index(19)
    rows = q.format_list(max_rows=5)
    check("在队尾开窗也不会越界", len(rows) <= 6, str(len(rows)))
    check("队尾时当前那首在", any(CURRENT_MARK in r for r in rows))


# ---------------- 10. 杂项 ----------------

def test_misc():
    print("\n[10] 杂项")
    q = mk(3)
    q.next()
    q.next()
    q.clear()
    check("清空后长度 0", len(q) == 0)
    check("清空后指针回 -1", q.current_index == -1)
    check("清空后历史也清了", q.history == [])
    check("清空后 next 不崩", q.next() is None)

    check("路径归一化：空值安全", _norm_path(None) == "" and _norm_path("") == "")
    check("路径归一化：大小写统一", _norm_path("MP3/A.mp3") == _norm_path("mp3/a.mp3"))

    check("截断：宽度 0 返回空", _clip("abc", 0) == "")
    check("截断：够宽就不动", _clip("晴天", 10) == "晴天")
    check("截断：中文按两格算", len(_clip("一二三四五", 6)) == 3,
          _clip("一二三四五", 6))

    # 历史上限
    q = mk(60)
    for i in range(MAX_HISTORY + 10):
        q.set_index(i % 60)
        q.next()
    check("历史不会无限涨", len(q.history) <= MAX_HISTORY, str(len(q.history)))

    # items 是副本
    q = mk(2)
    got = q.items
    got.clear()
    check("items 是副本，改它不影响队列", len(q) == 2)

    # QueueItem 基本属性
    it = QueueItem(title="晴天", path="mp3/晴天.mp3", bvid="BV1")
    check("display 优先用标题", it.display == "晴天")
    check("name 取文件名", it.name == "晴天.mp3")
    check("matches 认得自己", it.matches("mp3/晴天.mp3"))
    check("matches 不认别人", not it.matches("mp3/别的.mp3"))
    check("空路径不算匹配", QueueItem(title="x", path="").matches("") is False)

    q = mk(1)
    check("summary 非空队列", "共 1 首" in q.summary(), q.summary())
    check("summary 空队列", mk(0).summary().startswith("队列是空的"))


# ---------------- demo ----------------

def demo():
    """模拟一段真实使用，看输出顺不顺眼。"""
    print("=== 播放队列 demo（全是假文件，不会真的播）===\n")

    q = PlayQueue(fallback=lambda: "mp3/【随机】某首缓存里的歌.mp3")
    print("· 加了 4 首")
    for name in ["晴天 - 周杰伦", "花海 - 周杰伦", "雨爱 - 杨丞琳", "起风了 - 买辣椒也用券"]:
        q.add(name, f"mp3/{name}.mp3")
    print("  " + q.summary())

    print("\n· 按下「下一首」三次")
    for i in range(3):
        item = q.next()
        print(f"  第 {i + 1} 次 -> {item.display}")
    print("  " + q.summary())

    print("\n· 按「上一首」一次")
    print(f"  -> {q.prev().display}")

    print("\n· 现在的队列长这样：")
    for row in q.format_list():
        print("  " + row)

    print("\n· 切到随机播放，连按 5 次下一首")
    print(f"  模式 -> {MODE_NAMES[q.cycle_mode()]} -> {MODE_NAMES[q.cycle_mode()]}"
          f"（现在是 {MODE_NAMES[q.mode]}）")
    q.set_mode(MODE_SHUFFLE)
    for _ in range(5):
        print(f"  -> {q.next().display}")

    print("\n· 把「下一首播这个」插到当前后面")
    q.add_next("稻香 - 周杰伦", "mp3/稻香.mp3")
    print(f"  -> 下一首是 {q.next().display}")

    print("\n· 删掉正在播的这首")
    q.remove_current()
    print("  " + q.summary())
    for row in q.format_list():
        print("  " + row)

    print("\n· 把队列清空，再按下一首（走原来的随机兜底）")
    q.clear()
    print(f"  -> {q.next().display}")
    print("  " + q.summary())

    print("\n· 长歌名 + 窄终端（width=24，max_rows=3）")
    q2 = PlayQueue()
    for name in ["一个特别特别长的歌名测试截断效果", "短歌", "第三首", "第四首", "第五首"]:
        q2.add(name, f"mp3/{name}.mp3")
    q2.set_index(3)
    for row in q2.format_list(width=24, max_rows=3):
        print("  " + row)


def main():
    ap = argparse.ArgumentParser(description="播放队列验证脚本")
    ap.add_argument("--demo", action="store_true", help="看一段模拟操作的输出")
    args = ap.parse_args()

    if args.demo:
        demo()
        return 0

    print("播放队列 play_queue.py —— 离线验证")
    test_add()
    test_remove()
    test_move()
    test_sync()
    test_next()
    test_prev()
    test_fallback()
    test_mode()
    test_render()
    test_misc()

    print(f"\n{'=' * 46}")
    print(f"通过 {passed} / {passed + failed}")
    if failed:
        print(f"失败 {failed} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
