"""
冒烟测试：把所有能自动调用的功能实跑一遍。

    python smoke_test.py

分组：
  [P] 纯函数，不需要网络
  [N] 需要网络
  [X] 需要硬件 / 交互，自动测试里会跳过

无法自动验证的功能在脚本末尾统一列出，需要手动确认。
"""

import functools
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

RESULTS = []


def check(name, tag, fn):
    """跑一个用例，成功打 [OK]，失败打 [FAIL] 并打印异常。"""
    try:
        detail = fn()
        RESULTS.append((name, tag, True, detail))
        print(f"  [OK  ] {name}  {detail}")
    except Exception as exc:
        RESULTS.append((name, tag, False, repr(exc)))
        print(f"  [FAIL] {name}  {type(exc).__name__}: {exc}")
    except AssertionError as exc:
        RESULTS.append((name, tag, False, str(exc)))
        print(f"  [FAIL] {name}  断言失败: {exc}")


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    return True


# ------------------------------------------------------------------
# [P] 语言包
# ------------------------------------------------------------------

def test_lang():
    import lang
    assert_true(lang.normalize('English') == 'en', "'English' 应映射到 en")
    assert_true(lang.normalize('中文') == 'zh', "'中文' 应映射到 zh")
    assert_true(lang.normalize(None, 'zh') == 'zh', "空值应回落默认语言")
    assert_true(lang.translate('zh', 'download_success') != 'download_success',
                "中文包缺少 download_success")
    assert_true(lang.translate('en', 'download_success') != 'download_success',
                "英文包缺少 download_success")
    missing = {key for key in lang.language['zh'] if key not in lang.language['en']}
    assert_true(not missing or missing == {'features'}, f"英文包缺键: {missing}")
    return f"zh={len(lang.language['zh'])} en={len(lang.language['en'])}"


# ------------------------------------------------------------------
# [P] 配置与 Cookie
# ------------------------------------------------------------------

def test_config_cookie():
    import config
    cookies = config.load_cookie_dict()
    header = config.cookie_header()
    if cookies:
        assert_true('Cookie' in header, "有 Cookie 时应生成 Cookie 请求头")
        return f"{len(cookies)} 个 Cookie 字段"
    assert_true(header == {}, "没有 Cookie 时应返回空字典")
    return "未登录（无 Cookie 文件）"


# ------------------------------------------------------------------
# [P] 文件名清洗
# ------------------------------------------------------------------

def test_filename():
    import utils
    cases = {
        "《花海》周杰伦丨百万级录音棚试听丨【Hi-Res无损】": "花海 - 周杰伦",
        "晴天/周杰伦: 无损": "晴天_周杰伦_ 无损",
        "孤勇者「爱你孤身走暗巷」": "孤勇者",
    }
    for raw, _ in cases.items():
        result = utils.normalize_filename(raw)
        for banned in "《》「」『』【】":
            assert_true(banned not in result, f"{raw!r} 清洗后仍含 {banned}: {result!r}")
    illegal = '<>:"/\\|?*'
    for ch in illegal:
        out = utils.normalize_filename(f"a{ch}b")
        assert_true(ch not in out, f"非法字符 {ch!r} 未被替换")
    return "符号与非法字符均已处理"


# ------------------------------------------------------------------
# [P] 歌名分层清洗（含标题 -> 文件名的完整链路）
# ------------------------------------------------------------------

def test_title_cleaner():
    import title_cleaner
    from clean_title_verify import CASES, BANNED_FILENAME_CHARS

    failures = []
    for raw, must_have, forbidden in CASES:
        result = title_cleaner.clean_song_title(raw)
        for keyword in must_have:
            if keyword not in result:
                failures.append(f"{raw!r} 丢了 {keyword!r}")
        for char in forbidden:
            if char in result:
                failures.append(f"{raw!r} 残留 {char!r}")
        for char in BANNED_FILENAME_CHARS:
            if char in result:
                failures.append(f"{raw!r} 落地名含 {char!r}")
    assert_true(not failures, "; ".join(failures[:3]))
    assert_true(title_cleaner.query_core('约会 音乐') == '约会', "泛类词未被剔除")
    segs = title_cleaner.title_segments("RADWIMPS - デート《约会》")
    assert_true('约会' in segs, f"分段异常: {segs}")
    return f"{len(CASES)} 个用例全部通过"


# ------------------------------------------------------------------
# [P] 缓存数据库
# ------------------------------------------------------------------

def test_cache_manager():
    from cache_manager import CacheManager

    tmp = Path(tempfile.mkdtemp(prefix="cache_test_"))
    db = tmp / "test.db"
    cache = CacheManager(db_path=db)

    def make_file(name):
        path = tmp / name
        path.write_bytes(b"0" * 10)
        return path

    # --- 1. 增删改查（放在容量测试之前，否则可能被上限清理删掉） ---
    path = make_file("a.m4s")
    cache.insert("BV1", "歌一", path)
    row = cache.get("BV1")
    assert_true(row and row["title"] == "歌一", "缓存写入失败")
    cache.update_stats("BV1")
    row = cache.get("BV1")
    assert_true(row is not None, "更新播放统计后查不到记录")

    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT play_count FROM song_cache WHERE bvid='BV1'").fetchone()[0]
    assert_true(count == 1, f"播放次数应为 1，实际 {count}")

    # 重新写入不应清零播放次数
    cache.insert("BV1", "歌一改", path)
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT play_count FROM song_cache WHERE bvid='BV1'").fetchone()[0]
    assert_true(count == 1, f"重复写入后播放次数被清零: {count}")

    # 删除
    assert_true(cache.delete_by_bvid("BV1"), "按 BV 删除失败")
    assert_true(cache.get("BV1") is None, "删除后仍能查到")

    # --- 2. 容量上限 + NULL 排序保护 ---

    # 上限清理：新下载的歌（last_play_time 为 NULL）不能被删
    cache.MAX_ENTRIES = 100
    for i in range(100):
        p = tmp / f"old{i}.m4s"
        p.write_bytes(b"0")
        cache.insert(f"BVold{i}", f"老歌{i}", p)
        cache.update_stats(f"BVold{i}")
    new_song = tmp / "new.m4s"
    new_song.write_bytes(b"0")
    cache.insert("BVNEW", "刚下载的歌", new_song)

    with sqlite3.connect(db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM song_cache").fetchone()[0]
        survived = conn.execute(
            "SELECT bvid FROM song_cache WHERE bvid LIKE '%new%'").fetchall()
    assert_true(total <= 100, f"缓存超出上限: {total}")
    assert_true(bool(survived), "刚下载的歌被误删（NULL 排序问题）")

    cache.delete_all()
    assert_true(len(cache.get_all_files()) == 0, "清空缓存失败")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return "CRUD + 上限清理 + NULL 保护均正常"


# ------------------------------------------------------------------
# [P] 多线程下载（本地 HTTP 服务，覆盖大文件分片与小文件保护）
# ------------------------------------------------------------------

class _RangeRequestHandler(SimpleHTTPRequestHandler):
    """
    支持 HTTP Range 的静态文件服务。

    标准库的 SimpleHTTPRequestHandler 不认 Range，会忽略请求头直接返回整份文件，
    那样测不出分片下载的真实行为，所以这里自己实现一个简单的 206 响应。
    """

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().do_GET()

        with open(path, "rb") as f:
            data = f.read()
        start, end, status = 0, len(data) - 1, 200
        header_range = self.headers.get("Range")
        if header_range:
            matched = re.match(r"bytes=(\d*)-(\d*)", header_range)
            if matched:
                start = int(matched.group(1)) if matched.group(1) else 0
                end = int(matched.group(2)) if matched.group(2) else len(data) - 1
                end = min(end, len(data) - 1)
                status = 206
        body = data[start:end + 1]

        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.end_headers()
        self.wfile.write(body)


def _start_server(directory):
    # Python 3.7+ 的 SimpleHTTPRequestHandler 支持 directory 参数，不需要切工作目录
    handler = functools.partial(_RangeRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    hostname = f"http://127.0.0.1:{server.server_port}"
    return server, hostname


def test_downloading():
    import downloading

    work = Path(tempfile.mkdtemp(prefix="dl_test_"))
    serve_dir = work / "serve"
    serve_dir.mkdir(parents=True)
    out_dir = work / "out"
    out_dir.mkdir()

    big = serve_dir / "big.bin"
    big.write_bytes(os.urandom(700 * 1024))          # 700KB，够分多片
    small = serve_dir / "small.bin"
    small.write_bytes(b"x" * 100)                     # 100B，小于单个分片下限

    server, base = _start_server(serve_dir)
    try:
        # 大文件多线程
        out_big = out_dir / "big.bin"
        ok = downloading.download(f"{base}/big.bin", output_path=out_big,
                                  threads=4, resume=True)
        assert_true(ok, "多线程下载返回 False")
        assert_true(out_big.exists() and out_big.stat().st_size == big.stat().st_size,
                    f"大小不一致: {out_big.stat().st_size} vs {big.stat().st_size}")

        # 小文件（分片数远大于字节数）也不能失败
        out_small = out_dir / "small.bin"
        ok = downloading.download(f"{base}/small.bin", output_path=out_small,
                                  threads=8, resume=True)
        assert_true(ok, "小文件多线程下载返回 False")
        assert_true(out_small.exists() and out_small.stat().st_size == 100,
                    f"小文件结果异常: {out_small.stat().st_size}")

        # 单线程路径
        out_single = out_dir / "single.bin"
        ok = downloading.download(f"{base}/big.bin", output_path=out_single, threads=1)
        assert_true(ok and out_single.stat().st_size == big.stat().st_size,
                    "单线程下载失败")

        # 断点续传：先写半份半成品，resume 后应补齐
        partial = out_dir / "resume.bin"
        partial.write_bytes(big.read_bytes()[:300 * 1024])
        ok = downloading.download(f"{base}/big.bin", output_path=partial,
                                  threads=2, resume=True)
        assert_true(ok and partial.stat().st_size == big.stat().st_size,
                    f"断点续传失败: {partial.stat().st_size}")
    finally:
        server.shutdown()

    import shutil
    shutil.rmtree(work, ignore_errors=True)
    return "分片 / 小文件 / 单线程 / 断点续传均正常"


# ------------------------------------------------------------------
# [P] ffmpeg 转换
# ------------------------------------------------------------------

def test_transform():
    from transform import Transform

    ffmpeg = Path("ffmpeg.exe")
    if not ffmpeg.exists():
        raise AssertionError("项目根目录没有 ffmpeg.exe，跳过")

    work = Path(tempfile.mkdtemp(prefix="conv_test_"))
    wav = work / "tone.wav"
    subprocess.run(
        [str(ffmpeg), "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-y", str(wav)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=60,
    )
    assert_true(wav.exists() and wav.stat().st_size > 0, "ffmpeg 生成测试音频失败")

    transform = Transform(ffmpeg=str(ffmpeg))
    transform.convert(str(wav), str(work))
    mp3 = work / "tone.mp3"
    assert_true(mp3.exists() and mp3.stat().st_size > 0, "wav -> mp3 转换失败")

    import shutil
    size = mp3.stat().st_size
    shutil.rmtree(work, ignore_errors=True)
    return f"wav -> mp3 成功（{size} 字节）"


# ------------------------------------------------------------------
# [N] WBI 签名
# ------------------------------------------------------------------

def test_generate_params():
    import generate_params
    keys = generate_params.get_wbi_keys()
    assert_true(len(keys) == 2 and all(keys), "密钥获取失败")
    fallback = generate_params.using_fallback()
    import hashlib
    import urllib.parse

    signed = generate_params.sign_params({"avid": 1, "cid": 2, "fnval": 4048})
    assert_true("w_rid" in signed and "wts" in signed, "签名结果缺少字段")

    # 用官方算法独立复算一次，确认签名可复现
    params = {k: v for k, v in signed.items() if k != "w_rid"}
    query = urllib.parse.urlencode(dict(sorted(params.items())))
    expected = hashlib.md5(
        (query + generate_params.mixin_key(*keys)).encode("utf-8")
    ).hexdigest()
    assert_true(expected == signed["w_rid"], "独立复算的签名与 sign_params 不一致")

    ok = generate_params.probe(keys)
    source = "兜底值" if fallback else "nav 接口"
    return f"来源={source}，自检验活={'通过' if ok else '失败'}"


# ------------------------------------------------------------------
# [N] 搜索：两个目标用例
# ------------------------------------------------------------------

SEARCH_CASES = [
    ("约会 音乐", "BV1JW411D7rX", "RADWIMPS - デート"),
    ("花海", "BV1MwYg6tEsR", "周杰伦《花海》"),
]


def test_search_targets():
    from song_search import SongSearch

    report = []
    for keyword, expect_bvid, desc in SEARCH_CASES:
        searcher = SongSearch(prompt=keyword, timeout=0)
        bvid, title, candidates = searcher.search()
        assert_true(bool(candidates), f"{keyword!r} 没有任何候选")
        rank = next((i + 1 for i, item in enumerate(candidates)
                     if item["bvid"] == expect_bvid), None)
        assert_true(rank is not None, f"{keyword!r} 结果里找不到目标视频 {expect_bvid}")
        assert_true(bvid == expect_bvid,
                    f"{keyword!r} Top1 是 {bvid}(聚类应指向 {expect_bvid}，实际排第 {rank})")
        report.append(f"{keyword}->Top1={bvid}（{rank}）")
    return "；".join(report)


def test_search_filters():
    from song_search import SongSearch

    searcher = SongSearch(prompt="花海", timeout=0)
    bvid, title, candidates = searcher.search()
    for item in candidates:
        assert_true(item.get("duration") is None or item["duration"] <= SongSearch.MAX_DURATION,
                    f"超长视频未被过滤: {item['title']}")
        for word in SongSearch.BLACKLIST_WORDS:
            assert_true(word not in item["title"], f"黑名单词 {word} 未被过滤")
        assert_true(item.get("typename") not in SongSearch.BAD_PARTITIONS,
                    f"无关分区未被过滤: {item['typename']}")
    return f"{len(candidates)} 条候选全部通过过滤规则"


# ------------------------------------------------------------------
# [N] 音频下载（端到端）
# ------------------------------------------------------------------

def test_download_audio():
    from download_audio import DownloadAudio

    work = Path(tempfile.mkdtemp(prefix="audio_dl_"))
    downloader = DownloadAudio(threads=4, temp_dir=work / "temp", m4s_temp=work / "out")

    info = downloader._get_video_information("BV1JW411D7rX")
    assert_true(info["aid"] and info["cid"], "视频信息解析失败")

    url = downloader.get_audio_url(info["aid"], info["cid"],
                                   referer="https://www.bilibili.com/video/BV1JW411D7rX")
    assert_true(bool(url), "未取到音频流地址")

    path = downloader.download_audio("https://www.bilibili.com/video/BV1JW411D7rX")
    assert_true(path.exists(), f"文件不存在: {path}")
    size = path.stat().st_size
    assert_true(size > 100 * 1024, f"文件过小，可能下载不完整: {size}")

    name = path.name
    for banned in "《》「」『』【】〈〉":
        assert_true(banned not in name, f"文件名残留 {banned}: {name}")

    import shutil
    shutil.rmtree(work, ignore_errors=True)
    return f"{name}（{size / 1024 / 1024:.2f} MB）"


def test_download_audio_errors():
    from download_audio import DownloadAudio

    downloader = DownloadAudio(threads=2, temp_dir=Path("./temp"), m4s_temp=Path("./m4s_temp"))
    try:
        downloader.download_audio("这不是一个链接")
        raise AssertionError("非法链接竟然没有抛异常")
    except ValueError:
        pass
    return "非法链接正确抛出 ValueError"


# ------------------------------------------------------------------
# [N] 视频下载模块（只验证元数据与流选择，不真的下完整视频）
# ------------------------------------------------------------------

def test_download_video():
    from download_video import VideoDownloader

    downloader = VideoDownloader(cookie_file="bilibili_cookies.json",
                                 ffmpeg_path="./ffmpeg.exe", threads=2)
    loaded = downloader.load_cookies()
    video_id = downloader.extract_video_id("https://www.bilibili.com/video/BV1JW411D7rX")
    assert_true(video_id == "BV1JW411D7rX", f"BV 号提取错误: {video_id}")

    info = downloader.fetch_video_info(video_id)
    assert_true(info["aid"] and info["cid"], "视频信息获取失败")

    streams = downloader.fetch_dash_streams(info["aid"], info["cid"], quality=64)
    video = downloader.select_video_stream(streams["video"], quality=64)
    audio = downloader.select_audio_stream(streams["audio"])
    assert_true(video["baseUrl"] and audio["baseUrl"], "流地址为空")
    return f"Cookie={'已加载' if loaded else '无'}，视频流 {video['width']}x{video['height']}，音频 id={audio['id']}"


# ------------------------------------------------------------------
# [X] 主控：登录状态与命令注册
# ------------------------------------------------------------------

def test_main_cui():
    try:
        import main_CUI
    except ImportError as exc:
        raise AssertionError(f"缺少依赖（{exc}），需用项目 venv 运行")

    login = main_CUI.BiliLogin()
    user = login.current_user()
    detail = f"当前登录用户={user or '无'}"

    cui = main_CUI.MainCui()
    cui.load_config()
    cui.cmd_register()
    expected = {'help', 'download', 'voice', 'search', 'cache', 'login', 'exit',
                'pause', 'transform', 'batch_transform', 'batch_search',
                'batch_extract', 'reconnect'}
    missing = expected - set(cui.commands)
    assert_true(not missing, f"命令缺失: {missing}")
    # help 命令能正常出结果
    cui.cmd_help([])
    cui.cmd_help(['login'])
    detail += f"，命令 {len(cui.commands)} 个"
    return detail


def test_initial_setup_login_prompt():
    """用假的 input 验证初始化里的登录引导走的是 _inquiry。"""
    import initial_setup
    from unittest import mock

    setup = initial_setup.InitialSetup()
    with mock.patch("builtins.input", side_effect=[""]):
        result = setup._record_login_choice(default='N')
    assert_true(result is False, f"默认 N 时应返回 False，实际 {result}")
    with mock.patch("builtins.input", side_effect=["Y"]):
        result = setup._record_login_choice(default='N')
    assert_true(result is True, f"输入 Y 时应返回 True，实际 {result}")
    return "_inquiry 引导逻辑正常（N 跳过 / Y 登录）"


# ------------------------------------------------------------------

GROUPS = [
    ("语言包 lang.py", "P", test_lang),
    ("配置与 Cookie", "P", test_config_cookie),
    ("文件名清洗 utils", "P", test_filename),
    ("歌名分层清洗 title_cleaner", "P", test_title_cleaner),
    ("缓存 CacheManager", "P", test_cache_manager),
    ("多线程下载 downloading", "P", test_downloading),
    ("ffmpeg 转换 transform", "P", test_transform),
    ("登录引导 _record_login_choice", "P", test_initial_setup_login_prompt),
    ("WBI 签名 generate_params", "N", test_generate_params),
    ("搜索命中目标视频", "N", test_search_targets),
    ("搜索过滤规则", "N", test_search_filters),
    ("音频端到端下载", "N", test_download_audio),
    ("音频链接异常处理", "N", test_download_audio_errors),
    ("视频模块元数据/选流", "N", test_download_video),
    ("主控命令注册 + 登录态", "N", test_main_cui),
]

MANUAL_CHECKS = [
    "语音点歌（voice 命令）：需要麦克风，涉及麦克风索引、VAD、FunASR 模型加载",
    "语音指令识别：暂停 / 继续 / 下一首 / 停止 / 换个版本 的实际识别率",
    "实际播放声音：mpv 是否出声、播完是否自动切下一首",
    "micro:bit 遥控：串口 PAUSE / NEXT / STOP 与状态回传",
    "扫码登录全流程：二维码弹出 -> 手机扫码 -> Cookie 落盘 -> 高码率能否取到",
    "文件选择对话框（transform / batch_* 的文件与文件夹选择）",
    "首次运行初始化向导：包含新增的登录引导步骤",
    "batch_search / batch_extract 的交互式流程（依赖上一条的对话框）",
]


def main():
    print("=" * 78)
    print("BiliMusicCLI 冒烟测试")
    print("=" * 78)

    for index, (name, tag, fn) in enumerate(GROUPS, 1):
        print(f"[{index:02d}/{len(GROUPS)}] [{tag}] {name}")
        check(name, tag, fn)
        print()

    passed = sum(1 for _, _, ok, _ in RESULTS if ok)
    failed = [item for item in RESULTS if not item[2]]

    print("=" * 78)
    print(f"结果：{passed}/{len(RESULTS)} 通过")
    for name, tag, _, detail in failed:
        print(f"  [FAIL] [{tag}] {name}: {detail}")
    print("-" * 78)
    print("以下功能无法自动测试，需要手动验证：")
    for item in MANUAL_CHECKS:
        print(f"  · {item}")
    print("=" * 78)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
