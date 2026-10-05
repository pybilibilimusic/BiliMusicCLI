"""
B 站 WBI 签名（w_rid）工具。

签名规则（与官方一致）：
1. 从 /x/web-interface/nav 取 wbi_img.img_url 与 sub_url 的文件名作为 img_key / sub_key；
2. 两个 key 拼接后，按 MIXIN_KEY_ENC_TAB 重排并截取前 32 位，得到 mixin_key；
3. 把所有请求参数（含 wts）按 key 升序排列、剔除 !'()* 后 urlencode；
4. w_rid = md5(参数串 + mixin_key)。

要点：参与签名的参数必须等于实际发送的全部参数（w_rid 自身除外），
只签一部分会导致服务端校验不通过。密钥会轮换，因此动态获取并带兜底值。
"""

import hashlib
import time
import urllib.parse
from typing import Dict, Optional, Tuple

import requests

import config

# 官方固定的重排下标表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
PLAYURL = "https://api.bilibili.com/x/player/wbi/playurl"

# 用于密钥自检的样本视频（长期存在的经典投稿）
PROBE_BVID = "BV1GJ411x7h7"

# 官方要求剔除的字符
STRIP_CHARS = "!'()*"

# 接口取不到时的兜底值。
# B 站这两串密钥会轮换，但轮换周期很长（本组于 2026-10 实测仍有效），
# 所以仍然动态获取优先，失败才依次退回这里的历史密钥。
# 轮换后只需把新的 (img_key, sub_key) 追加到最前面，旧的保留做历史候选。
FALLBACK_KEYS = (
    ("7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45"),  # 2026-10-02 实测有效
)

_keys_cache: Optional[Tuple[str, str]] = None
_using_fallback = False          # 当前是否在使用兜底密钥
_fallback_index = 0              # 当前试到第几组兜底


def get_wbi_keys(force_refresh: bool = False, timeout: int = 10) -> Tuple[str, str]:
    """获取 (img_key, sub_key)，优先走接口并缓存，失败则用兜底值。"""
    global _keys_cache, _using_fallback
    if _keys_cache and not force_refresh:
        return _keys_cache

    try:
        response = requests.get(NAV_URL, headers=config.headers, timeout=timeout)
        wbi_img = response.json().get("data", {}).get("wbi_img", {})
        img_key = wbi_img["img_url"].rsplit("/", 1)[-1].split(".")[0]
        sub_key = wbi_img["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        if img_key and sub_key:
            _keys_cache = (img_key, sub_key)
            _using_fallback = False
            return _keys_cache
    except Exception:
        pass

    _using_fallback = True
    if _keys_cache:
        return _keys_cache
    return FALLBACK_KEYS[0]


def set_keys(keys: Tuple[str, str]) -> None:
    """手动指定密钥（某组兜底验证通过后锁住它，避免重复无效请求）。"""
    global _keys_cache, _using_fallback
    _keys_cache = keys
    _using_fallback = tuple(keys) in [tuple(k) for k in FALLBACK_KEYS]


def refresh_wbi_keys(timeout: int = 10) -> Tuple[str, str]:
    """签名被拒（如 -403）时调用，强制重新拉取密钥。"""
    return get_wbi_keys(force_refresh=True, timeout=timeout)


def using_fallback() -> bool:
    """当前是否正依赖兜底密钥（网络不通 / nav 接口异常）。"""
    return _using_fallback


def fallback_warning() -> str:
    """给调用方用的提示文案。"""
    return ("WBI 密钥：nav 接口不可用，已回落内置兜底值。"
            "若请求持续失败，多半是密钥已轮换，请到 generate_params.FALLBACK_KEYS 追加新密钥。")


def probe(keys: Tuple[str, str] = None, timeout: int = 10) -> bool:
    """
    拿一组密钥真打一次 playurl，验证它是否还有效。

    用于自检和测试；不要放在热路径上调用。
    """
    try:
        target = keys or get_wbi_keys(timeout=timeout)
        view = requests.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": PROBE_BVID},
            headers=config.headers,
            timeout=timeout,
        ).json()
        data = view.get("data") or {}
        params = sign_params(
            {
                "avid": data["aid"],
                "cid": data["cid"],
                "fnval": 4048,
                "fnver": 0,
                "fourk": 1,
                "platform": "pc",
                "qn": 30280,
            },
            keys=target,
            timeout=timeout,
        )
        headers = dict(config.headers)
        headers["Referer"] = f"https://www.bilibili.com/video/{PROBE_BVID}"
        payload = requests.get(PLAYURL, params=params, headers=headers, timeout=timeout).json()
        return payload.get("code") == 0
    except Exception:
        return False


def mixin_key(img_key: str, sub_key: str) -> str:
    """按官方下标表重排两个 key，取前 32 位。"""
    raw = img_key + sub_key
    return "".join(raw[index] for index in MIXIN_KEY_ENC_TAB if index < len(raw))[:32]


def sign_params(params: Dict, keys: Tuple[str, str] = None, timeout: int = 10) -> Dict:
    """
    给参数字典加上 wts 与 w_rid，返回可直接用于 requests 的 params。

    注意：调用方必须把「所有」要发送的参数都传进来，否则签名校验会失败。
    """
    img_key, sub_key = keys or get_wbi_keys(timeout=timeout)

    cleaned: Dict = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = "".join(ch for ch in value if ch not in STRIP_CHARS)
        cleaned[key] = value

    cleaned["wts"] = int(time.time())
    ordered = dict(sorted(cleaned.items()))

    query = urllib.parse.urlencode(ordered)
    ordered["w_rid"] = hashlib.md5(
        (query + mixin_key(img_key, sub_key)).encode("utf-8")
    ).hexdigest()
    return ordered


if __name__ == "__main__":
    keys = get_wbi_keys()
    live = not using_fallback()
    print("密钥来源：", "nav 接口" if live else "内置兜底值")
    print("keys:", keys)
    print("生效验证：", "通过" if probe(keys) else "失败")
