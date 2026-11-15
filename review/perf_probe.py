"""
性能打点工具（可复测，不改动任何生产逻辑）。

    python review/perf_probe.py --startup                # 启动导入耗时
    python review/perf_probe.py --model                  # 语音模型加载耗时
    python review/perf_probe.py --download "晴天 周杰伦"  # 搜索/view/取流/下载分段
    python review/perf_probe.py --all                    # 全部

设计要点：
- 每段都在**自己的子进程**里计时，互不污染（尤其模型加载会灌满内存）。
- 先报告有没有透明代理（PITFALLS C10）。WorkBuddy 会向子进程注入
  http_proxy/https_proxy，那会让网络段明显偏慢，不能代表你的真实体感。
  想测真实体感，在自己的 cmd/PowerShell 里跑，且确认没有代理变量。
- 下载请求一律带 timeout，并在失败时把原因带出来（C3 禁止 except: pass）。
- 语音输入环节**不在这里测**：没有麦克风，也不拿理论值替代，以你的实测为准。
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXY_KEYS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY")


def report_proxy():
    found = {k: os.environ[k] for k in PROXY_KEYS if k in os.environ}
    print("=" * 64)
    print("环境检查")
    print("=" * 64)
    if found:
        print("  ⚠️ 检测到代理变量，网络段数据会偏慢（C10）：")
        for k, v in found.items():
            print("     %s = %s" % (k, v))
        print("  要测真实体感：关掉它们再跑，或直接在自己的终端里跑。")
    else:
        print("  代理变量：无（直连）")
    print("  Python：%s" % sys.executable)
    print()


def run_child(label, code, timeout=600):
    """在独立子进程里跑一段代码并计时。"""
    print("=" * 64)
    print(label)
    print("=" * 64)
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, r'%s'); %s"
             % (ROOT, code)],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        print("  ⏱ 超过 %d 秒，已终止（这段可能卡住了，值得单独查）" % timeout)
        return
    dt = time.perf_counter() - t0
    if proc.stdout.strip():
        for line in proc.stdout.strip().splitlines()[-12:]:
            print("  " + line)
    if proc.returncode != 0:
        print("  ✗ 子进程退出码 %d" % proc.returncode)
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        for line in tail:
            print("    " + line)
    print("  墙钟耗时：%.2f s" % dt)
    print()


def probe_startup():
    run_child(
        "启动：import main_CUI（到模块可用）",
        "import time; t=time.perf_counter(); import main_CUI; "
        "print('IMPORT_SEC = %.2f' % (time.perf_counter()-t))",
        timeout=180)
    # importtime 找最重的几个模块
    print("=" * 64)
    print("启动：累计 import 耗时最大的模块（-X importtime）")
    print("=" * 64)
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "importtime", "-c",
             "import sys; sys.path.insert(0, r'%s'); import main_CUI" % ROOT],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
        rows = []
        for line in (proc.stderr or "").splitlines():
            if line.startswith("import time:") and "|" in line:
                parts = line.split("|")
                try:
                    rows.append((int(parts[1].strip()), parts[2].strip()))
                except ValueError:
                    continue
        for us, name in sorted(rows, reverse=True)[:8]:
            print("  %8.2f s  %s" % (us / 1e6, name))
    except subprocess.TimeoutExpired:
        print("  ⏱ importtime 超时")
    print()


def probe_model():
    run_child(
        "语音模型：VoiceRecognition._load_models()",
        "import time; from VoiceRecognition import VoiceRecognition; "
        "v=VoiceRecognition(wake_words=['播放']); "
        "t=time.perf_counter(); v._load_models(); "
        "print('MODEL_LOAD_SEC = %.2f' % (time.perf_counter()-t))",
        timeout=900)
    cache = Path.home() / ".cache" / "modelscope" / "hub" / "models" / "iic"
    if cache.exists():
        print("  模型缓存目录：%s" % cache)
        for child in sorted(cache.iterdir()):
            if child.is_dir():
                total = sum(f.stat().st_size for f in child.rglob("*")
                            if f.is_file())
                print("    %-50s %7.1f MB" % (child.name, total / 1048576))
    print()


def probe_download(query):
    """分段测：WBI 密钥 / 搜索 / view / playurl / 下载。"""
    print("=" * 64)
    print("点歌链路：%s（不含语音识别）" % query)
    print("=" * 64)
    code = r'''
import time, sys, os
from pathlib import Path
import config, download_audio, song_search
from generate_params import get_wbi_keys, source

def stage(label, fn):
    t0 = time.perf_counter()
    try:
        out = fn()
        print("  %-24s %9.1f ms" % (label, (time.perf_counter()-t0)*1000))
        return out
    except Exception as exc:
        print("  %-24s %9.1f ms  失败 %s: %s" %
              (label, (time.perf_counter()-t0)*1000, type(exc).__name__, exc))
        raise

config.load_cookie_dict()
get_wbi_keys()
print("  WBI 来源: %s" % source())
q = os.environ.get("PERF_QUERY", "晴天 周杰伦")
bvid, title, _ = stage("搜索 search/type",
    lambda: song_search.SongSearch(prompt=q, timeout=0, source="cli").search())
print("  命中: %s %s" % (bvid, title))
page = "https://www.bilibili.com/video/" + bvid
dl = download_audio.DownloadAudio(threads=1, temp_dir="./temp",
                                  m4s_temp="./m4s_temp", timeout=10)
info = stage("视频信息 view", lambda: dl._get_video_information(bvid, referer=page))
au = stage("取流 playurl",
           lambda: dl.get_audio_url(info["aid"], info["cid"], referer=page))
import requests
out = Path("./m4s_temp/_perf_probe.m4s")
if out.exists(): out.unlink()
def fetch():
    r = requests.get(au, headers=dl._headers(referer=page),
                     stream=True, timeout=30)
    r.raise_for_status()
    n = 0
    with open(out, "wb") as f:
        for chunk in r.iter_content(65536):
            if chunk:
                f.write(chunk); n += len(chunk)
    return n
stage("下载(裸请求, 30s 超时)", fetch)
print("  文件大小: %.2f MB" % (out.stat().st_size/1048576 if out.exists() else 0))
if out.exists(): out.unlink()
'''
    os.environ["PERF_QUERY"] = query
    run_child("点歌链路分段（环境变量：见上方代理检查）", code, timeout=300)


def main():
    ap = argparse.ArgumentParser(description="BiliMusicCLI 性能打点")
    ap.add_argument("--startup", action="store_true")
    ap.add_argument("--model", action="store_true")
    ap.add_argument("--download", metavar="歌名", default=None)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    if not (args.startup or args.model or args.download or args.all):
        ap.print_help()
        return 0

    report_proxy()
    if args.startup or args.all:
        probe_startup()
    if args.model or args.all:
        probe_model()
    if args.download or args.all:
        # 用环境变量传歌名，避免子进程 -c 的 sys.argv 坑
        os.environ["PERF_QUERY"] = args.download or "晴天 周杰伦"
        # run_child 的 code 里读环境变量
        probe_download(os.environ["PERF_QUERY"])
    print("=" * 64)
    print("说明：语音输入/识别延迟不在此工具范围内，以你自己的实测为准。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
