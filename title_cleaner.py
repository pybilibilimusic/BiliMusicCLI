"""
歌名清洗模块（分层处理）。

B 站视频标题往往堆了一堆装饰：`【Hi-Res无损】`、`「歌词片段」`、`《正式歌名》`、
`... | 录音棚试听 | 4K60FPS`。直接拿去做文件名会出现一长串乱码式尾巴，
拿来算相似度也会因为这些噪声被压低。

分层顺序：
  L1 去掉 HTML 高亮残留（<em class="keyword">）
  L2 删除成对标签连同内容：【】[]〖〗（装饰性标签）
  L2.5 剥掉 UP 主自造的固定前缀：「在XX录音棚大声听」「日推音乐」…
  L3 删除歌词片段：「」『』 “” ‘’ "" ''（引号里通常是歌词，不是歌名）
  L4 展开书名号《》〈〉，保留内部文字（《里往往才是真歌名）
  L5 剔除噪声词：录音棚 / 试听 / 动态歌词 / 无损 / Hi-Res / 4K / 60FPS …
  L6 按分隔符切段，丢掉空段、噪声段、疑似歌词句
  L7 收敛空白与残留符号
  L8 清洗失败（结果为空）时回退原始标题

对外 API：
    clean_song_title(title)        -> 清洗后的歌名（用于显示与文件名）
    title_segments(title, query)   -> 分段列表（用于算相似度）
"""

import re

# ---------------- 规则表（调参改这里） ----------------

# L2：连同内容一起删除的标签括号
TAG_PAIRS = [("【", "】"), ("[", "]"), ("〖", "〗")]

# L3：连同内容一起删除的歌词引号
QUOTE_PAIRS = [("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'")]

# L4：保留内容、只去掉符号的书名号
WRAP_PAIRS = [("《", "》"), ("〈", "〉")]

# L5：噪声词。中文走子串替换，英文按词边界替换（避免误伤单词内部）。
NOISE_WORDS_ZH = [
    "录音棚", "录音室", "录音版", "录音棚录音", "百万级录音棚",
    "试听", "动态歌词", "动态歌词版", "动态歌词PV", "歌词版", "逐字歌词",
    "滚动歌词", "版本",
    "无损音质", "无损", "母带", "高解析度",
    "高清", "超清", "蓝光", "画质增强", "修复版",
    "纯享", "饭拍", "现场版伴奏",
]
NOISE_WORDS_EN = [
    "Hi-Res", "HiRes", "HiFi", "SQ", "MQA", "FLAC", "Remaster", "Remastered",
    "4K", "8K", "60FPS", "120FPS", "HDR", "2160P", "1080P", "720P", "MV", "PV",
]

# L2.5：UP 主自造的固定前缀。
#
# 这类前缀是标题读者的「营销话术」，跟歌名本身毫无关系，不去掉会让文件名平白长一截。
# 只清理**已经观察到的固定写法**，宁可漏也别误伤：
#   - 一律要求以「在 …录音棚/录音室 …听」的完整结构出现，
#     单纯标题以「在」开头的歌（<在> 有《在》，等等）不会被误删
#   - 发现新的固定话术后，往这个列表里追加正则即可
TITLE_PREFIX_PATTERNS = [
    # 「在百万豪装录音棚大声听」「在专业录音棚听」「在录音棚里静静听」…
    r"^在\s*[^\s「」『』【】\[\]《》（）()]{0,12}?(?:录音棚|录音室)\s*(?:里|内)?\s*(?:大声|静静|一起|慢慢)?\s*(?:听|聆听|欣赏)\s*",
    # 「在XX录音棚」但没写「听」的写法
    r"^在\s*[^\s「」『』【】\[\]《》（）()]{0,12}?(?:录音棚|录音室)\s*(?:里|内)?\s*",
    # 日推 / 推荐类话术
    r"^日推音乐\s*",
    r"^每日推荐\s*",
    r"^今日推荐\s*",
    r"^推荐歌曲\s*",
    r"^每日(?:一首)?(?:好)?歌\s*",
]

_COMPILED_PREFIX_PATTERNS = [re.compile(p) for p in TITLE_PREFIX_PATTERNS]

# L6：切分用的分隔符
SEPARATORS = "|｜丨·・•／"

# L6：出现这些标点说明整段是句子（歌词），不是歌名
SENTENCE_PUNCT = "，。！？；、,?!;"

# L7：允许出现在中间、但不允许出现在首尾的符号
EDGE_STRIP = "-_—–~!！?？:：,，.。;；/\\、*#·"

# 查询里的泛类词：算相似度时不参与，例如「晴天 音乐」的核心是「晴天」
QUERY_CATEGORY_WORDS = [
    "音乐", "歌曲", "歌", "单曲", "原声", "伴奏", "完整版", "高清",
    "bgm", "BGM", "MV", "mv", "ost", "OST", "PV", "pv",
]

_MATH_BOLD_MAP = str.maketrans("𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳",
                               "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


# ---------------- 各层实现 ----------------


def _normalize_symbols(text: str) -> str:
    """把数学粗体字母（𝐇𝐢-𝐑𝐞𝐬 这类花样式标题）换回普通 ASCII。"""
    return text.translate(_MATH_BOLD_MAP)


def _strip_html(text: str) -> str:
    """L1：去掉搜索接口返回的高亮标签。"""
    return re.sub(r"<[^>]+>", "", text or "")


def _remove_pairs(text: str, pairs) -> str:
    """删除成对符号「连同内部内容」。"""
    for left, right in pairs:
        text = re.sub(
            re.escape(left) + r"[^" + re.escape(left + right) + r"]*" + re.escape(right),
            " ",
            text,
        )
    return text


def _unwrap_pairs(text: str, pairs) -> str:
    """去掉书名号本身，保留内部文字，接缝处补一个分隔符。"""
    for left, right in pairs:
        text = re.sub(
            re.escape(left) + r"\s*([^" + re.escape(left + right) + r"]+?)\s*" + re.escape(right),
            r" - \1 - ",
            text,
        )
    return text


def _remove_noise_words(text: str) -> str:
    """L5：剔除噪声词。"""
    for word in sorted(NOISE_WORDS_ZH, key=len, reverse=True):
        text = text.replace(word, " ")
    for word in sorted(NOISE_WORDS_EN, key=len, reverse=True):
        text = re.sub(
            r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])",
            " ",
            text,
            flags=re.IGNORECASE,
        )
    return text


def _looks_like_lyric(part: str) -> bool:
    """整段像歌词句子：含句中标点，或过长。"""
    if any(ch in SENTENCE_PUNCT for ch in part):
        return True
    return len(part) > 24


def _strip_prefixes(text: str) -> str:
    """
    L2.5：剥掉 UP 主自造的固定前缀。

    反复剥离，防止叠加多个话术（比如「在XX录音棚大声听」+「日推音乐」同时出现）。
    剥离后如果整串都被吃光，会返回空串，交由 L8 回退到原始标题。
    """
    for _ in range(3):
        stripped = text.lstrip()
        for pattern in _COMPILED_PREFIX_PATTERNS:
            matched = pattern.match(stripped)
            if matched:
                stripped = stripped[matched.end():]
                break
        else:
            return text.lstrip()
        text = stripped
    return text.lstrip()


def _split_and_filter(text: str) -> str:
    """L6：切分 -> 丢掉无效段 -> 用 " - " 重新拼接。"""
    parts = re.split("[" + re.escape(SEPARATORS) + "]", text)
    kept = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 纯符号 / 纯数字，不是歌名
        if not re.search(r"[\w\u3040-\u30ff\u4e00-\u9fff]", part):
            continue
        kept.append(part)

    # 疑似歌词段：只有在还有别的段剩下时才丢
    if len(kept) > 1:
        survivors = [p for p in kept if not _looks_like_lyric(p)]
        if survivors:
            kept = survivors

    if not kept:
        return text.strip()

    seen, unique = set(), []
    for part in kept:
        if part not in seen:
            seen.add(part)
            unique.append(part)
    return " - ".join(unique)


def _collapse_dashes(text: str) -> str:
    """把各种连续短横（- - / — / -—）收敛成一个分隔符。"""
    text = re.sub(r"\s*[-—–_]\s*", " - ", text)
    text = re.sub(r"(\s*-\s*){2,}", " - ", text)
    return text


def _tidy(text: str) -> str:
    """L7：去空括号、收敛空白与残留符号。"""
    empty_pairs = [("(", ")"), ("（", "）"), ("【", "】"), ("[", "]")]
    for left, right in empty_pairs:
        text = re.sub(re.escape(left) + r"\s*" + re.escape(right), " ", text)
    text = _collapse_dashes(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(EDGE_STRIP + " ")
    return re.sub(r"\s+", " ", text).strip()


def _strip_leftover_symbols(text: str) -> str:
    """兜底：最终结果里不允许出现书名号 / 引号类杂符号。"""
    symbols = "".join(c for pair in TAG_PAIRS + QUOTE_PAIRS + WRAP_PAIRS for c in pair)
    text = text.translate({ord(ch): None for ch in symbols})
    return re.sub(r"\s+", " ", text).strip()


# ---------------- 对外接口 ----------------


def clean_song_title(title: str) -> str:
    """
    分层清洗歌名，返回可直接用于显示和文件名的字符串。

    清洗失败（结果为空）时回退原始标题（但仍会去掉杂符号）。
    """
    original = _strip_html(_normalize_symbols(title)).strip()

    text = original
    text = _remove_pairs(text, TAG_PAIRS)      # L2
    text = _strip_prefixes(text)               # L2.5
    text = _remove_pairs(text, QUOTE_PAIRS)    # L3
    text = _unwrap_pairs(text, WRAP_PAIRS)     # L4
    text = _remove_noise_words(text)           # L5
    text = _split_and_filter(text)             # L6
    text = _tidy(text)                         # L7

    if not text:                               # L8
        return _strip_leftover_symbols(original)
    return _strip_leftover_symbols(text)


def query_core(query: str) -> str:
    """去掉查询里的泛类词，留下真正用于匹配的核心词。"""
    if not query:
        return ""
    core = _normalize_symbols(query).strip()
    for word in QUERY_CATEGORY_WORDS:
        core = re.sub(
            r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])",
            " ",
            core,
        )
    core = re.sub(r"\s+", "", core)
    return core if core else _normalize_symbols(query).strip()


def title_segments(title: str, query: str = None) -> list:
    """
    把清洗后的标题切成若干段，返回用于相似度比较的列表。

    例如 "RADWIMPS - デート - 约会" -> ["RADWIMPS", "デート", "约会"]
    这样拿查询去比单个段落，比直接比整条标题准得多。
    """
    cleaned = clean_song_title(title)
    parts = re.split(r"\s+-\s+|[" + re.escape(SEPARATORS) + "]", cleaned)
    segments = [p.strip() for p in parts if p.strip()]
    return segments or ([cleaned] if cleaned else [])
