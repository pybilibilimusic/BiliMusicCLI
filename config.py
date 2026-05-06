import configparser
import re
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
    '【': '[',
    '】': ']',
    '《': '<',
    '》': '>',
    '·': '.',
    '‧': '.',
    '「': '[',
    '」': ']',
    '『': '[',
    '』': ']',
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
}

windows_illegal_chars = r'[<>:"/\\|?*\x00-\x1f]'

temp_dir = Path("./temp")
logging_path = Path("./log")

def normalize_filename(filename):
    for old_char, new_char in char_map.items():
        filename = filename.replace(old_char, new_char)
    filename = re.sub(windows_illegal_chars, "_",filename)
    filename = re.sub(r'_+', '_', filename)
    filename = filename.strip(' _.')
    return filename