"""诊断 voice 模型加载：分段计时 + 抓网络连接 + 卡住时打调用栈。

用法：
  python temp/voice_load_probe.py            # 照抄主程序参数（模型 ID）
  python temp/voice_load_probe.py --local    # 改传本地缓存绝对路径
  python temp/voice_load_probe.py --stack 20 # 每 20 秒打一次栈（默认 25）
"""
import argparse
import faulthandler
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import model_paths  # noqa: E402

_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo
_net = []


def _addr(a):
    try:
        return "%s:%s" % (a[0], a[1])
    except Exception:
        return str(a)


def _connect(self, addr):
    t0 = time.perf_counter()
    try:
        return _real_connect(self, addr)
    finally:
        dt = (time.perf_counter() - t0) * 1000
        _net.append((_addr(addr), dt))
        print("  [NET connect] %-42s %8.1f ms" % (_addr(addr), dt), flush=True)


def _getaddrinfo(host, port, *a, **kw):
    t0 = time.perf_counter()
    try:
        return _real_getaddrinfo(host, port, *a, **kw)
    finally:
        print("  [NET dns    ] %-42s %8.1f ms"
              % (str(host), (time.perf_counter() - t0) * 1000), flush=True)


def stage(label, fn):
    print("\n>>> %s" % label, flush=True)
    t0 = time.perf_counter()
    try:
        r = fn()
    except Exception as e:
        print("    !! 失败 (%s): %r" % (type(e).__name__, e), flush=True)
        return None, (time.perf_counter() - t0) * 1000
    dt = (time.perf_counter() - t0) * 1000
    print("    -> %.1f ms" % dt, flush=True)
    return r, dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="传本地缓存路径")
    ap.add_argument("--stack", type=int, default=25, help="每 N 秒打一次栈，0=关")
    ap.add_argument("--profile", action="store_true",
                    help="用 cProfile 剖析 AutoModel，打印函数级耗时 top")
    ap.add_argument("--no-vad", action="store_true",
                    help="不传 vad_model，用它分离 fsmn-vad 的加载开销")
    args = ap.parse_args()

    socket.socket.connect = _connect
    socket.getaddrinfo = _getaddrinfo
    if args.stack > 0:
        faulthandler.dump_traceback_later(args.stack, repeat=True, file=sys.stderr)

    print("本机缓存:", flush=True)
    for mid in ("iic/SenseVoiceSmall", "fsmn-vad"):
        print("  " + model_paths.describe(mid), flush=True)
    print("模式: %s" % ("本地绝对路径（修复后）"
                       if args.local else "模型 ID（修复前，每次在线校验）"),
          flush=True)
    print("代理变量: %s" % ({k: v for k, v in os.environ.items()
                          if "proxy" in k.lower()} or "(无)"), flush=True)

    _, t_import = stage("import funasr.AutoModel",
                        lambda: __import__("funasr", fromlist=["AutoModel"]))
    from funasr import AutoModel  # noqa: F401

    kw = dict(device="cpu", disable_pbar=True,
              vad_kwargs={"max_single_segment_time": 30000},
              disable_update=True)
    if args.local:
        kw.update(model=model_paths.resolve_model_path("iic/SenseVoiceSmall"),
                  vad_model=model_paths.resolve_model_path("fsmn-vad"))
    else:
        kw.update(model="iic/SenseVoiceSmall", vad_model="fsmn-vad")
    if args.no_vad:
        kw.pop("vad_model", None)
        kw.pop("vad_kwargs", None)
        print("  (--no-vad：本次不加载 fsmn-vad)", flush=True)

    label = "AutoModel(...)  <-- 主程序 _load_models 第 80 行"
    if args.profile:
        import cProfile
        import io
        import pstats
        print("\n>>> %s [cProfile]" % label, flush=True)
        pr = cProfile.Profile()
        t0 = time.perf_counter()
        pr.enable()
        try:
            AutoModel(**kw)
        finally:
            pr.disable()
        print("    -> %.1f ms" % ((time.perf_counter() - t0) * 1000),
              flush=True)
        buf = io.StringIO()
        pstats.Stats(pr, stream=buf).sort_stats("cumulative").print_stats(28)
        print(buf.getvalue(), flush=True)
    else:
        stage(label, lambda: AutoModel(**kw))

    stage("silero_vad.load_silero_vad()",
          lambda: __import__("silero_vad").load_silero_vad())

    faulthandler.cancel_dump_traceback_later()
    print("\n=== 网络汇总 ===", flush=True)
    if not _net:
        print("  零网络连接", flush=True)
    else:
        total = sum(d for _, d in _net)
        print("  connect 次数 %d，累计阻塞 %.1f ms" % (len(_net), total), flush=True)
        for a, d in sorted(_net, key=lambda x: -x[1])[:8]:
            print("    %-42s %8.1f ms" % (a, d), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
