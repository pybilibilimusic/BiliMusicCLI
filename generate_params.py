"""
B 站 WBI 签名（w_rid）工具。

签名规则（与官方一致）：
1. 从 /x/web-interface/nav 取 wbi_img.img_url 与 sub_url 的文件名作为 img_key / sub_key；
2. 两个 key 拼接后，按 MIXIN_KEY_ENC_TAB 重排并截取前 32 位，得到 mixin_key；
3. 把所有请求参数（含 wts）按 key 升序排列、剔除 !'()* 后 urlencode；
4. w_rid = md5(参数串 + mixin_key)。

要点：参与签名的参数必须等于实际发送的全部参数（w_rid 自身除外），
只签一部分会导致服务端校验不通过。密钥会轮换，因此动态获取并带兜底值。

2026-10-13 调整：密钥长期稳定（实测一年没变），没必要每次进程启动都现拉
（89ms；网络不稳时还要重试一次，最坏能卡 20 秒）。改成**本地缓存 + TTL**：
正常跑只读一个文件、不联网，过期才去 nav 取。轮换后靠两条路自愈：
TTL 到期自动刷新，或签名被拒时 refresh_wbi_keys() 强制重拉。
"""

import hashlib
import json
import os
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

# 密钥的本地缓存。temp/ 已被 gitignore，写不进去也不影响主流程（最多重新拉一次）。
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "temp", "wbi_keys.json")
CACHE_TTL = 7 * 24 * 3600       # 7 天：够长到几乎不用联网，够短到轮换后能自己跟上
NAV_TIMEOUT = 5                 # 取密钥最多等 5 秒 —— 它失败就用缓存/兜底，
                                # 没必要在"下第一首歌"的关键路径上替它等 10 秒

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
_using_fallback = False          # 当前是否在使用兜底密钥（本地缓存的旧值 / 内置值）
_last_error = ""                 # 最近一次取密钥失败的原因（给排查用）
_keys_source = ""                # nav / cache / fallback，只用于排查和自检显示


# ---------- 本地缓存 ----------

def _read_cache() -> Tuple[Optional[Tuple[str, str]], float]:
    """读本地缓存，返回 (密钥, 存入时刻)。没有或坏了就 (None, 0)。"""
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        img_key, sub_key = data.get("img_key"), data.get("sub_key")
        if not (isinstance(img_key, str) and isinstance(sub_key, str)
                and img_key and sub_key):
            return None, 0.0
        return (img_key, sub_key), float(data.get("fetched_at") or 0.0)
    except Exception:
        return None, 0.0


def _write_cache(keys: Tuple[str, str]) -> None:
    """写本地缓存。写失败无所谓，最多下次重新拉一次。"""
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as handle:
            json.dump({"img_key": keys[0], "sub_key": keys[1],
                       "fetched_at": time.time()}, handle)
    except Exception:
        pass


def _clear_cache() -> None:
    """强制重拉前丢掉缓存，免得又拿到同一把旧钥匙。"""
    try:
        os.remove(CACHE_PATH)
    except Exception:
        pass


def _fetch_keys(timeout: int, gzip_only: bool = False) -> Tuple[Optional[Tuple[str, str]], str]:
    """
    打一次 nav 取密钥。返回 (密钥, 失败原因)，成功时失败原因是空串。

    gzip_only=True 时只接受 gzip/deflate —— 用来在「响应没解开」的情况下自愈。
    以前这里全是 `except Exception: pass`，取不到只知道"失败了"，不知道为什么，
    排查时只能靠猜（2026-10-12 那次其实是 br 没解压库）。
    """
    headers = dict(config.headers)
    if gzip_only:
        headers["Accept-Encoding"] = "gzip, deflate"

    try:
        response = requests.get(NAV_URL, headers=headers, timeout=timeout)
    except Exception as exc:
        return None, "nav 请求失败：%r" % (exc,)

    try:
        payload = response.json()
    except Exception:
        return None, ("nav 返回的解不开：HTTP %s | Content-Encoding=%s | %d 字节不是 JSON"
                      % (response.status_code,
                         response.headers.get("Content-Encoding") or "无",
                         len(response.content)))

    wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
    try:
        img_key = wbi_img["img_url"].rsplit("/", 1)[-1].split(".")[0]
        sub_key = wbi_img["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    except (KeyError, AttributeError):
        return None, ("nav 没给 wbi_img（code=%s message=%s）"
                      % (payload.get("code"), payload.get("message")))

    if img_key and sub_key:
        return (img_key, sub_key), ""
    return None, "wbi_img 字段是空的"


def get_wbi_keys(force_refresh: bool = False, timeout: int = NAV_TIMEOUT) -> Tuple[str, str]:
    """
    获取 (img_key, sub_key)。顺序：本地缓存 -> nav 接口 -> 过期缓存 -> 内置兜底。

    正常情况只走第一步（读文件，不联网）。只有缓存过期或不存在才打 nav。
    """
    global _keys_cache, _using_fallback, _last_error, _keys_source
    if _keys_cache and not force_refresh:
        return _keys_cache

    # 1) 本地缓存，没过期就直接用
    if not force_refresh:
        cached, saved_at = _read_cache()
        if cached and time.time() - saved_at < CACHE_TTL:
            _keys_cache = cached
            _using_fallback = False
            _last_error = ""
            _keys_source = "cache"
            return _keys_cache

    # 2) 打 nav。超时按 NAV_TIMEOUT 封顶 —— 取密钥不值得等那么久，
    #    真的超时就走下面的缓存/兜底，别卡住整个下载。
    limit = max(1, min(int(timeout or NAV_TIMEOUT), NAV_TIMEOUT))
    keys, err = _fetch_keys(limit)
    if not keys:
        # 第一把没成 —— 最常见的就是响应没解开（br/zstd 缺解压库）。
        # 退成只收 gzip 再试一次，这一把能自愈绝大多数情况。
        keys, err2 = _fetch_keys(limit, gzip_only=True)
        err = err2 or err

    if keys:
        _write_cache(keys)
        _keys_cache = keys
        _using_fallback = False
        _last_error = ""
        _keys_source = "nav"
        return _keys_cache

    # 3) 网络取不到：先用本地那份（哪怕过期），再不行才用内置的
    cached, saved_at = _read_cache()
    if cached:
        _keys_cache = cached
        _using_fallback = True
        _last_error = ("nav 取不到（%s），用的是 %.1f 天前的本地缓存"
                       % (err, (time.time() - saved_at) / 86400.0))
        _keys_source = "cache-stale"
        return _keys_cache

    _last_error = err or "未知原因"
    _using_fallback = True
    _keys_source = "fallback"
    return FALLBACK_KEYS[0]


def refresh_wbi_keys(timeout: int = NAV_TIMEOUT) -> bool:
    """
    签名被拒（如 -403）时调用：丢掉缓存重新拉一把。

    返回 True 表示拿到了密钥、值得拿它重试一次请求。
    ⚠️ 这是"密钥轮换"唯一的自愈入口 —— 本地缓存让"拿到过期钥匙"变成可能，
    所以这一步比加缓存之前**更重要**，不能省。
    """
    _clear_cache()
    keys = get_wbi_keys(force_refresh=True, timeout=timeout)
    return bool(keys and all(keys))


def using_fallback() -> bool:
    """当前是否正依赖兜底密钥（网络不通 / nav 接口异常）。"""
    return _using_fallback


def last_error() -> str:
    """上一次取密钥失败的具体原因。空的就说明没失败过。"""
    return _last_error


def source() -> str:
    """这把密钥从哪来的：nav（刚拉的）/ cache（本地没过期）/ fallback（兜底）。"""
    return _keys_source or ("fallback" if _using_fallback else "cache")


def fallback_warning() -> str:
    """给调用方用的提示文案。带上具体原因，别只说"失败了"。"""
    base = ("WBI 密钥：nav 接口不可用，已回落内置兜底值。"
            "若请求持续失败，多半是密钥已轮换，请到 generate_params.FALLBACK_KEYS 追加新密钥。")
    if _last_error:
        return base + "\n  取密钥失败的原因：" + _last_error
    return base


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
    print("密钥来源：", source())
    print("keys:", keys)
    print("生效验证：", "通过" if probe(keys) else "失败")
