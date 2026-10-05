import json
from pathlib import Path

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
    'Connection': 'keep-alive',
    'sec-ch-ua': '"Microsoft Edge";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'document',
    'sec-fetch-mode': 'navigate',
    'sec-fetch-site': 'none',
    'upgrade-insecure-requests': '1',
}

char_map = {
    '——': '--',
    '―': '-',
    '—': '-',
    '：': ':',
    '；': ';',
    '，': ',',
    '。': '.',
    '！': '!',
    '？': '?',
    '（': '(',
    '）': ')',
    # 书名号 / 引号类：这些是标题装饰符号，清洗后不应出现在文件名里，直接删除
    '《': '',
    '》': '',
    '〈': '',
    '〉': '',
    '【': '',
    '】': '',
    '〖': '',
    '〗': '',
    '「': '',
    '」': '',
    '『': '',
    '』': '',
    '·': '.',
    '‧': '.',
    '﹑': ',',
    '﹔': ';',
    '﹕': ':',
    '﹖': '?',
    '﹗': '!',
    '＂': '"',
    '＇': "'",
    '＼': r'\\',
    '＃': '#',
    '＄': '$',
    '％': '%',
    '＆': '&',
    '＊': '*',
    '＋': '+',
    '－': '-',
    '／': '/',
    '＜': '<',
    '＝': '=',
    '＞': '>',
    '＠': '@',
    '＾': '^',
    '＿': '_',
    '｀': '`',
    '｛': '{',
    '｜': '|',
    '｝': '}',
    '～': '~',
    '｟': '(',
    '｠': ')',
    '｡': '.',
    '｢': '[',
    '｣': ']',
    '､': ',',
    '･': '.',
    '￣': '_',
    '￤': '|',

    '\u200B': '',
    '\uFEFF': '',
    '\u00A0': ' ',
    '\u2000': ' ',
    '\u2001': ' ',
    '\u2002': ' ',
    '\u2003': ' ',
    '\u2004': ' ',
    '\u2005': ' ',
    '\u2006': ' ',
    '\u2007': ' ',
    '\u2008': ' ',
    '\u2009': ' ',
    '\u200A': ' ',
    '\u202F': ' ',
    '\u205F': ' ',
    '\u3000': ' ',
    '𝐇𝐢-𝐑𝐞𝐬':'Hi-Res'
}
quality_map = {
    '1080p': 80,
    '720p': 64,
    '480p': 32,
    '360p': 16,
}

windows_illegal_chars = r'[<>:"/\\|?*\x00-\x1f]'

temp_dir = Path("./temp")
logging_path = Path("./log")

# Cookie 文件（扫码登录后生成，已被 .gitignore 忽略，切勿提交）
cookie_path = Path("bilibili_cookies.json")


def load_cookie_dict(path=None) -> dict:
    """
    读取本地 Cookie 文件，返回 {name: value}。

    兼容两种格式：浏览器导出的列表形式，以及简单的键值字典。
    文件不存在或损坏时返回空字典（未登录状态下程序仍可下载低码率流）。
    """
    cookie_file = Path(path) if path else cookie_path
    if not cookie_file.exists():
        return {}
    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}

    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    if isinstance(data, list):
        return {
            str(item.get("name")): str(item.get("value"))
            for item in data
            if item.get("name")
        }
    return {}


def cookie_header(path=None) -> dict:
    """把 Cookie 文件转成请求头；没有 Cookie 时返回空字典。"""
    cookies = load_cookie_dict(path)
    if not cookies:
        return {}
    return {"Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items())}