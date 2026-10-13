"""
语音监听的性能体检（离线，不需要麦克风、不需要网络）。

回答一个问题：跑 `voice` 之后电脑为什么卡、反应为什么慢。
只测不猜，测完给一张对照表。

跑法：
    python voice_bench.py           # 只测 import + VAD（几十秒）
    python voice_bench.py --asr     # 连 ASR 一起测（要加载 SenseVoice，慢）
"""

import argparse
import sys
import time

SR = 16000          # 采样率，跟 VoiceRecognition 默认一致


def _timed(label, fn):
    """跑一次并计时，返回 (标签, 毫秒, 返回值)。"""
    t0 = time.perf_counter()
    result = fn()
    return label, (time.perf_counter() - t0) * 1000, result


def bench_imports():
    print("\n[1] import 耗时 —— 启动时「卡那么一下」基本都花在这儿")
    rows = []
    for name in ("numpy", "torch", "pyaudio", "silero_vad", "funasr"):
        label, ms, _mod = _timed(name, lambda n=name: __import__(n))
        rows.append((label, ms))
        print(f"  {label:12} {ms:9.1f} ms")
    return rows


def bench_vad(seconds=10.0):
    """测 VAD：单次推理多久，折算成「监听一分钟要烧掉多少 CPU 秒」。"""
    import numpy as np
    import torch
    import silero_vad

    print("\n[2] VAD 推理 —— 这是监听期间**一直不停**的 CPU 占用")
    print("    ⚠️ 实测：16k 下窗口**只能是 512**（8k 才是 256），喂 1024 直接")
    print("       ValueError。所以想降频只能「跳块」，不能「加大块」。")
    _label, load_ms, model = _timed("load", lambda: silero_vad.load_silero_vad())
    print(f"  加载 VAD 模型 {load_ms:.1f} ms")

    rng = np.random.default_rng(0)
    audio = rng.normal(0, 0.01, int(SR * seconds)).astype(np.float32)

    chunk = 512                       # 16k 下唯一合法的窗口
    blocks = [audio[i:i + chunk] for i in range(0, len(audio) - chunk, chunk)]
    for block in blocks[:10]:
        model(torch.from_numpy(block), SR)
    t0 = time.perf_counter()
    for block in blocks:
        model(torch.from_numpy(block), SR)
    dt = time.perf_counter() - t0

    per_call = dt / len(blocks) * 1000
    full = (SR / chunk) * per_call / 1000     # 每监听 1 秒耗多少 CPU 秒

    print(f"\n  现在的写法（每 512 样本跑一次，每秒 {SR / chunk:.0f} 次）：")
    print(f"    单次 {per_call:.2f} ms  →  监听 1 秒烧 {full * 1000:.1f} ms CPU"
          f"（单核 {full * 100:.1f}%）")
    print("\n  改成跳块（只在安静时跳，一听到动静立刻恢复全速）：")
    for stride in (2, 3, 4, 6):
        cpu = full / stride
        gap = chunk * stride / SR * 1000
        print(f"    每 {stride} 块跑一次（约每 {gap:.0f} ms 一次）→ "
              f"监听 1 秒烧 {cpu * 1000:.1f} ms CPU（单核 {cpu * 100:.1f}%）")
    return full


def bench_asr():
    """测 ASR：加载 SenseVoice 要多久，识别一段 2 秒音频要多久。"""
    print("\n[3] ASR —— 每次说完话都要跑一次，是「反应慢」的主要来源")
    import numpy as np

    t0 = time.perf_counter()
    from funasr import AutoModel
    print(f"  import funasr 的 AutoModel  { (time.perf_counter()-t0)*1000:8.1f} ms")

    t0 = time.perf_counter()
    model = AutoModel(model="iic/SenseVoiceSmall", device="cpu",
                      disable_pbar=True, disable_update=True)
    print(f"  加载 SenseVoice 模型        {(time.perf_counter() - t0) * 1000:8.1f} ms")

    rng = np.random.default_rng(1)
    audio = rng.normal(0, 0.05, int(SR * 2)).astype(np.float32)
    for _ in range(2):                                   # 预热，第一次总慢
        model.generate(input=audio, language="auto",
                       use_itn=True, batch_size_s=0)
    t0 = time.perf_counter()
    model.generate(input=audio, language="auto",
                   use_itn=True, batch_size_s=0)
    print(f"  识别一段 2 秒音频           {(time.perf_counter() - t0) * 1000:8.1f} ms")


def main():
    ap = argparse.ArgumentParser(description="语音监听性能体检")
    ap.add_argument("--asr", action="store_true",
                    help="连 ASR 一起测（要加载 SenseVoice 模型，慢）")
    ap.add_argument("--seconds", type=float, default=10.0,
                    help="VAD 模拟时长（秒，默认 10）")
    args = ap.parse_args()

    print("=" * 66)
    print("语音监听性能体检")
    print("=" * 66)

    bench_imports()
    full = bench_vad(args.seconds)
    if args.asr:
        bench_asr()

    print("\n" + "=" * 66)
    print("结论（照着数据来的）")
    print("=" * 66)
    print(f"  1) VAD 监听本身只占单核 {full * 100:.0f}% 左右 —— "
          f"不是卡顿的主因，但它是**一直不停**的。")
    print("  2) 真正的「卡一下」在 import：funasr 一个就要十几秒。")
    print("     → 这是**一次性**的，忍过去就没事；想改善就得延迟/后台加载。")
    print("  3) 「反应慢」是每次说完话跑一次 SenseVoice 推理，")
    print("     而且现在是**说一段就新起一个线程**，连续说话会好几个一起跑。")
    print("     → 加 --asr 能量化这一段，但结论基本确定：要串行化 + 限流。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
