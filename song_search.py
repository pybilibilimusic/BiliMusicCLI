"""
歌曲搜索模块。

首选 B 站官方搜索接口（返回结构化数据：bvid / 时长 / 分区），
失败时降级为搜索页 HTML 解析。

相比旧版的两条独立正则，这里保证「标题」与「BV 号」严格一一对应，
并通过 时长上限 + 分区 + 黑名单 + 标题相似度 综合打分，
避免搜出「盘点史上最强的十大龙卷风」这类形式匹配但内容无关的视频。
"""

import difflib
import re
import unicodedata
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

import config
import feedback as fb
import title_cleaner

# 接口地址
SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
HTML_SEARCH_URL = "https://search.bilibili.com/all?keyword="
HOME_URL = "https://www.bilibili.com/"

# 卡片元素（HTML 降级方案用）
CARD_CLASS = "bili-video-card__info--right"
TITLE_CLASS = "bili-video-card__info--tit"

# 复用连接，并持有 buvid3（搜索接口需要）
_SESSION: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """获取带 buvid3 的公共 Session（官方搜索接口要求先拿到该 Cookie）。"""
    global _SESSION
    if _SESSION is None:
        session = requests.Session()
        try:
            session.get(HOME_URL, headers=config.headers, timeout=10)
        except Exception:
            # 拿不到 buvid3 也能继续，接口可能拒绝，届时走 HTML 降级
            pass
        _SESSION = session
    return _SESSION


class SongSearch:
    """按歌名在 B 站搜索最匹配的视频。"""

    # ---- 过滤参数（可调，做消融实验时改这里） ----
    MAX_DURATION = 600          # 超过 10 分钟基本不是单曲
    GOOD_DURATION = (90, 420)   # 正常单曲时长区间，命中给加分

    # 注意：以下两个词表均按「大小写不敏感」匹配，无需重复登记不同大小写形式。
    # 存在包含关系的词（如 "无损" / "无损音质"）不会重复计分，见 _keyword_hits()。

    BLACKLIST_WORDS = [
        "纯享", "循环", "盘点", "十大", "合集", "科普", "教学", "讲解", "解说",
        "reaction", "直播回放", "录播", "采访", "预告",
        "花絮", "混剪", "卡拉OK", "伴奏",
    ]

    HIGH_QUALITY_WORDS = [
        "Hi-Res", "Hi-Res无损", "HiFi", "无损", "无损音质", "母带",
        "录音棚", "官方", "官方版", "24bit", "4K修复", "重制", "Remaster",
    ]

    # 平台原生排序先验：
    # B 站按 totalrank 返回的顺序本身已含「相关性 + 热度」信号，完全丢弃可惜，
    # 但直接照单全收也不行 —— 平台排序会混入形式相关实则不符合要求的视频
    #（比如搜歌名结果排第一的是二创、鬼畜、无关翻唱）。
    #
    # 这里的处理是：把原生名次折算成一个「加成」，并且
    #   1) 幅度远小于相似度项（最多几分 vs 相似度最高 100 分）—— 只做同档位候选的排序依据
    #   2) 乘上相似度系数 —— 与查询本身就不相关的候选，吃不到这份加成
    # 真正决定生死的是上面的硬淘汰 + 相似度，原生排序只在旗鼓相当时拍板。
    NATIVE_RANK_BONUS = 8.0        # 原生第 1 名能拿到的加成上限（实际还会乘相似度）
    NATIVE_RANK_DECAY = 1.2        # 每往后一名衰减多少
    NATIVE_RANK_WEIGHT = 1.0       # 原生排序参与最终折算的总权重，设 0 即完全停用

    # 音乐类分区：命中加分
    GOOD_PARTITIONS = {
        "音乐综合", "音乐", "MV", "演唱", "原创音乐", "翻唱", "电台",
        "电子音乐", "说唱", "古风", "音乐现场",
    }

    # 明显与歌曲无关的分区：命中直接淘汰
    BAD_PARTITIONS = {
        "单机游戏", "网络游戏", "手机游戏", "电子竞技", "桌游棋牌",
        "科学科普", "社科人文", "科技", "数码", "资讯", "纪录片",
        "影视", "电影", "电视剧", "短片", "动画", "番剧", "国创",
        "鬼畜", "鬼畜调教", "美食", "动物圈", "运动", "汽车", "军事",
        "历史", "公开课", "课堂", "职场", "设计创意", "广告",
    }

    def __init__(self, prompt: str, timeout: int = 0, source: str = "cli"):
        self.prompt = prompt
        self.timeout = timeout if timeout and timeout > 0 else 10
        # 来源只用于埋点区分：cli=手动 search 命令，voice=语音点歌，batch=批量模式
        self.source = source or "cli"

    # ---------- 数据获取 ----------

    def _fetch_api(self, page: int = 1) -> List[Dict]:
        """调用官方搜索接口，返回结构化结果列表。"""
        params = {
            "search_type": "video",
            "keyword": self.prompt,
            "duration": 1,          # 0-10 分钟，过滤盘点 / 直播类长视频
            "order": "totalrank",
            "page": page,
        }
        headers = dict(config.headers)
        headers["Referer"] = "https://search.bilibili.com/"

        response = _get_session().get(
            SEARCH_API, params=params, headers=headers, timeout=self.timeout
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"搜索接口返回异常: {payload.get('code')} {payload.get('message')}")

        results = []
        for index, item in enumerate(payload.get("data", {}).get("result", []) or [], 1):
            bvid = item.get("bvid")
            if not bvid:
                continue
            raw_title = self._strip_html(item.get("title", ""))
            results.append({
                "bvid": bvid,
                "title": raw_title,                            # 原始标题：黑名单 / 音质词判据
                "clean": title_cleaner.clean_song_title(raw_title),   # 清洗后：显示与文件名
                "duration": self._parse_duration(item.get("duration", "")),
                "typename": item.get("typename", ""),
                "author": item.get("author", ""),
                "play": item.get("play", 0) or 0,
                "rank": index,                                 # 平台原生名次，用于排序先验
            })
        return results

    def _fetch_html(self, page: int = 1) -> List[Dict]:
        """降级方案：解析搜索页 HTML，逐个卡片配对标题与 BV 号。"""
        headers = dict(config.headers)
        headers["Referer"] = "https://search.bilibili.com/"
        response = _get_session().get(
            HTML_SEARCH_URL + quote(self.prompt), headers=headers, timeout=self.timeout
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        for index, card in enumerate(soup.find_all("div", class_=CARD_CLASS), 1):
            link = card.find("a", href=re.compile(r"/video/(BV[0-9A-Za-z]+)"))
            title_tag = card.find(class_=TITLE_CLASS)
            if link is None or title_tag is None:
                continue
            match = re.search(r"/video/(BV[0-9A-Za-z]+)", link["href"])
            raw_title = self._strip_html(title_tag.get("title") or title_tag.get_text())
            if not match or not raw_title:
                continue
            results.append({
                "bvid": match.group(1),
                "title": raw_title,
                "clean": title_cleaner.clean_song_title(raw_title),
                "duration": None,
                "typename": "",
                "author": "",
                "play": 0,
                "rank": index,
            })
        return results

    def _search(self, retry: int = 0) -> List[Dict]:
        """
        获取一页候选（retry 即页码偏移）。

        官方接口优先，失败自动降级到 HTML 解析。
        """
        page = retry + 1
        try:
            items = self._fetch_api(page=page)
            if items:
                return items
        except Exception as exc:
            print(f"搜索接口不可用（{exc}），改用网页解析。")
        return self._fetch_html(page=page)

    # ---------- 打分 ----------

    @staticmethod
    def _strip_html(title: str) -> str:
        """去掉接口返回的高亮标签与首尾空白。"""
        return re.sub(r"<[^>]+>", "", title or "").strip()

    @staticmethod
    def _parse_duration(text: str) -> Optional[int]:
        """把 4:14 / 1:02:03 转成秒。"""
        if not text:
            return None
        parts = str(text).split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            return None
        return None

    @staticmethod
    def _normalize(text: str) -> str:
        """用于相似度比较：只保留中英文与数字。"""
        return re.sub(r"[^\w\u4e00-\u9fff]", "", text or "").lower()

    def _similarity(self, item: Dict) -> float:
        """
        查询词与标题的相似度（0~1）。

        关键点：拿清洗后的「最匹配片段」去比，而不是整条标题。
        例如搜「约会 音乐」，标题 "RADWIMPS - デート - 约会" 里的 "约会" 片段
        能打到满分，而 "约会大作战背景音乐" 只能算部分匹配。
        """
        query = self._normalize(title_cleaner.query_core(self.prompt) or self.prompt)
        if not query:
            return 0.0

        ratio = 0.0
        for segment in title_cleaner.title_segments(item.get("clean") or item.get("title", "")):
            target = self._normalize(segment)
            if not target:
                continue
            value = difflib.SequenceMatcher(None, query, target).ratio()
            if query == target:
                value = 1.0
            elif query in target:
                value = max(value, 0.9)
            elif target in query:
                value = max(value, 0.8)
            ratio = max(ratio, value)
        return ratio

    @staticmethod
    def _fold(text: str) -> str:
        """
        关键词匹配的归一化形式：NFKC 折叠 + 转小写。

        这样标题里的各种花式写法都能被识别：
        - "Hi-res" / "HI-RES" → 匹配词典的 "Hi-Res"
        - "𝐇𝐢-𝐑𝐞𝐬"（花式 Unicode 粗斜体）→ NFKC 折叠后同样命中
        - "Ｒｅｍａｓｔｅｒ"（全角）→ 匹配 "Remaster"
        只用于比对，不改变标题本身。
        """
        return unicodedata.normalize("NFKC", text or "").lower()

    @classmethod
    def _keyword_hits(cls, title: str, words: List[str]) -> List[str]:
        """
        统计标题命中了哪些关键词，重叠部分只算一次。

        若简单地逐个做 `word in title`，词典里有包含关系的词会被重复计分：
        标题 "Hi-Res无损音质" 能同时命中 "Hi-Res"、"无损"、"无损音质" 三条，
        白拿三倍分数，结果就是堆砌音质词的标题压过真正的高质量版本。

        这里按词长降序匹配，命中后将区间标记为已消费，后续更短的词无法重复计入。
        """
        folded = cls._fold(title)
        consumed = bytearray(len(folded))
        hits: List[str] = []

        for word in sorted(words, key=len, reverse=True):
            needle = cls._fold(word)
            if not needle:
                continue
            start = folded.find(needle)
            while start != -1:
                end = start + len(needle)
                if not any(consumed[start:end]):
                    consumed[start:end] = b"\x01" * (end - start)
                    hits.append(word)
                    break
                # 这一处已被更长的词占用了，继续往后找
                start = folded.find(needle, start + 1)
        return hits

    def score(self, item: Dict) -> float:
        """给候选打分，返回 -999 表示淘汰。"""
        title = item.get("title", "")          # 判据用原始标题

        if self._keyword_hits(title, self.BLACKLIST_WORDS):
            return -999

        if item.get("typename") in self.BAD_PARTITIONS:
            return -999

        duration = item.get("duration")
        if duration is not None and duration > self.MAX_DURATION:
            return -999

        similarity = self._similarity(item)
        item["similarity"] = round(similarity, 3)

        value = 100.0 * similarity

        if duration is not None and self.GOOD_DURATION[0] <= duration <= self.GOOD_DURATION[1]:
            value += 15
        if item.get("typename") in self.GOOD_PARTITIONS:
            value += 25

        quality_hits = self._keyword_hits(title, self.HIGH_QUALITY_WORDS)
        item["quality"] = quality_hits                    # 便于排查打分来源
        value += 6 * len(quality_hits)

        play = item.get("play") or 0
        if play > 0:
            value += min(10.0, (play ** 0.25) / 4)      # 播放量越高越可信，收益递减

        # 原生排序：按名次线性衰减，再乘相似度做门控，
        # 保证「排得靠前」只能惠及「本来就和查询相关」的候选。
        native_rank = item.get("rank") or 0
        native_bonus = 0.0
        if native_rank > 0 and self.NATIVE_RANK_WEIGHT > 0:
            native_bonus = max(
                0.0,
                self.NATIVE_RANK_BONUS - self.NATIVE_RANK_DECAY * (native_rank - 1)
            ) * similarity * self.NATIVE_RANK_WEIGHT
            value += native_bonus
        item["native_bonus"] = round(native_bonus, 1)

        item["score"] = round(value, 1)
        return value

    def rank(self, items: List[Dict]) -> List[Dict]:
        """过滤 + 排序，返回可用候选（按分数降序）。"""
        kept = [item for item in items if self.score(item) > -999]
        kept.sort(key=lambda x: x["score"], reverse=True)
        return kept

    # ---------- 对外接口 ----------

    def search(self) -> Tuple[Optional[str], Optional[str], List[Dict]]:
        """
        语音模式：不等待输入，返回 (best_bvid, best_title, full_list)。

        full_list 为排序后的全部可用候选，供「换个版本」使用。
        """
        candidates = self.rank(self._search())
        if not candidates:
            return None, None, []
        best = candidates[0]
        # 对外返回清洗后的标题：直接就能当文件名用
        return best["bvid"], best.get("clean") or best["title"], candidates

    def interactive_search(self, retry: int = 0) -> Tuple[Optional[str], Optional[str]]:
        """手动搜索模式：打印列表并等待选择，支持 r 翻页、q 退出。"""
        print("Please wait a moment, searching...")
        items = self._search(retry=retry)
        candidates = self.rank(items)

        if not candidates:
            print(f"No usable results found (raw: {len(items)}).")
            return None, None

        print("\n" + "=" * 60)
        for index, item in enumerate(candidates, 1):
            shown = item.get("clean") or item["title"]
            extra = []
            if item.get("duration") is not None:
                minutes, seconds = divmod(item["duration"], 60)
                extra.append(f"{minutes}:{seconds:02d}")
            if item.get("typename"):
                extra.append(item["typename"])
            if item.get("play"):
                extra.append(f"播放 {item['play']:,}")
            print(f"{index}. [{item['score']}] {shown}")
            if extra:
                print(f"     {' | '.join(extra)}")
        print("输入编号选择（直接回车用第一条），'r' 翻页，'q' 退出：", end="", flush=True)

        # 埋点：把用户本来就做的动作记下来（记失败也不影响选歌）。
        # picked_index 是 0-based —— 选第 1 个就记 0。
        def remember(action, item=None, index=-1):
            fb.record(self.prompt, action, candidates=candidates,
                      picked_index=index, chosen=item, page=retry,
                      source=self.source)
            if item is None:
                return None, None
            return item["bvid"], item.get("clean") or item["title"]

        while True:
            user_input = input().strip().lower()
            if user_input == "q":
                print("已取消搜索。")
                return remember("cancel")
            if user_input == "r":
                if retry >= 4:
                    print("翻页次数过多，已结束搜索。")
                    return remember("cancel")
                print("加载下一页...")
                remember("page")
                return self.interactive_search(retry=retry + 1)
            if user_input == "":
                best = candidates[0]
                print(f"使用最佳结果：{best.get('clean') or best['title']}")
                return remember("accept", best, 0)
            if user_input.isdigit():
                choice = int(user_input)
                if 1 <= choice <= len(candidates):
                    picked = candidates[choice - 1]
                    print(f"已选择：{picked.get('clean') or picked['title']}")
                    # 选第 1 个和直接回车是一回事 —— 都算认可推荐
                    action = "accept" if choice == 1 else "pick"
                    return remember(action, picked, choice - 1)
                print(f"编号无效，请输入 1-{len(candidates)}。")
            else:
                print("输入无效，请输入数字、'r' 或 'q'。")


if __name__ == "__main__":
    keyword = input("请输入歌名: ").strip()
    searcher = SongSearch(prompt=keyword)
    bvid, title = searcher.interactive_search()
    print(f"\n结果: {bvid} - {title}")
