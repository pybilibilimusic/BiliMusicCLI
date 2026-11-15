import subprocess
from pathlib import Path

class Transform:
    def __init__(self, ffmpeg=r"./ffmpeg"):
        self.ffmpeg_path = Path(ffmpeg)

    def convert(self, input_path: str, output_folder: str, output_suffix: str = ".mp3") -> None:
        input_path, output_folder = Path(input_path), Path(output_folder)
        output_file = output_folder / (input_path.stem + output_suffix)

        command = [
            str(self.ffmpeg_path), "-i", str(input_path),
            "-q:a", "0", "-map", "a",
            str(output_file), "-y"
        ]

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='ignore'
            )
            stdout, stderr = process.communicate(timeout=60)
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, command, output=stderr)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise Exception(f"ffmpeg conversion timeout for {input_path}")


# ---------------------------------------------------------------------------
# 独立测试入口
#
#   python transform.py            # 跑一遍主要路径
#   python transform.py --keep     # 保留临时产物，方便手工检查
#
# 不新增依赖：用项目自带的 ffmpeg.exe 现场生成测试素材，
# 全程在 tempfile.TemporaryDirectory() 里，退出自动清理（不碰项目文件）。
# ---------------------------------------------------------------------------

def _selftest(keep=False):
    import tempfile

    results = []

    def check(label, cond, extra=""):
        results.append((label, bool(cond)))
        print("  [%s] %s%s" % ("OK  " if cond else "FAIL", label,
                               ("  -> " + str(extra)) if extra else ""))

    ffmpeg = Path(__file__).parent / "ffmpeg.exe"
    if not ffmpeg.exists():
        print("找不到 ffmpeg.exe，无法跑测试入口")
        return 1

    print("=" * 62)
    print("transform.py 测试入口")
    print("=" * 62)
    print("  ffmpeg: %s" % ffmpeg)

    tmp = tempfile.TemporaryDirectory(prefix="bili_transform_")
    work = Path(tmp.name)
    out_dir = work / "out"
    out_dir.mkdir()

    def make_source(name, command):
        """用 lavfi 现场造一个素材，返回路径。"""
        target = work / name
        proc = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error",
             *command, str(target), "-y"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="ignore", timeout=30)
        if proc.returncode != 0:
            print("  [SKIP] 造不出测试素材 %s：%s" % (name, proc.stderr[-200:]))
        return target

    wav = make_source("tone.wav",
                      ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"])
    if not wav.exists():
        tmp.cleanup()
        return 1

    transform = Transform(ffmpeg=str(ffmpeg))

    # 1) 正常路径：wav -> mp3
    try:
        transform.convert(str(wav), str(out_dir), ".mp3")
        mp3 = out_dir / "tone.mp3"
        check("正常转换 wav -> mp3 产出非空文件",
              mp3.exists() and mp3.stat().st_size > 0,
              mp3.stat().st_size if mp3.exists() else "无文件")
    except Exception as exc:
        check("正常转换 wav -> mp3", False, repr(exc))

    # 2) 自定义输出后缀（同一份素材再转一次）
    try:
        transform.convert(str(wav), str(out_dir), ".wav")
        check("自定义 output_suffix 生效",
              (out_dir / "tone.wav").exists())
    except Exception as exc:
        check("自定义 output_suffix", False, repr(exc))

    # 3) 覆盖写：再转一次同名文件，不应因已存在而失败
    try:
        transform.convert(str(wav), str(out_dir), ".mp3")
        check("同名文件覆盖写（-y）不报错", True)
    except Exception as exc:
        check("同名文件覆盖写", False, repr(exc))

    # 4) 失败路径：输入文件不存在 -> ffmpeg 非 0，应抛异常且带原因
    try:
        transform.convert(str(work / "missing.wav"), str(out_dir), ".mp3")
        check("不存在的输入应抛异常", False, "竟然没抛")
    except Exception as exc:
        check("不存在的输入应抛异常", True, type(exc).__name__)

    # 5) 失败路径：输出目录不存在 -> 应抛异常（convert 本身不建目录）
    try:
        transform.convert(str(wav), str(work / "no_such_dir"), ".mp3")
        check("输出目录不存在应抛异常", False, "竟然没抛")
    except Exception as exc:
        check("输出目录不存在应抛异常", True, type(exc).__name__)

    # 6) -map a 路径：纯视频素材没有音频流，应失败
    silent_video = make_source(
        "silent.mp4",
        ["-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p"])
    if silent_video.exists():
        try:
            transform.convert(str(silent_video), str(out_dir), ".mp3")
            check("无音频流素材应失败（-map a）", False, "竟然成功了")
        except Exception as exc:
            check("无音频流素材应失败（-map a）", True, type(exc).__name__)
    else:
        print("  [SKIP] 无音频流用例（造不出纯视频素材）")

    # 7) 超时分支不在这里测：convert 写死 60s，造一个必然超时的输入会拖慢验证。
    print("  [SKIP] 60 秒超时分支（需可注入 timeout 才能离线测，本轮未改生产逻辑）")

    print("\n" + "=" * 62)
    passed = sum(1 for _, ok in results if ok)
    print("通过 %d / %d" % (passed, len(results)))
    if keep:
        print("临时产物保留在：%s" % work)
    else:
        tmp.cleanup()
    return 0 if passed == len(results) else 1


if __name__ == '__main__':
    import sys
    sys.exit(_selftest(keep="--keep" in sys.argv))