"""
把运行期二进制依赖打包成 runtime_library，供别人 clone 后一键补齐环境。

为什么会需要它：
    bin/libmpv-2.dll   118 MB   播放用，缺失直接启动不了
    ffmpeg.exe          98 MB   格式转换（transform / batch_extract）
这两个加起来 216 MB，国内从官方源分别下载又慢又容易断。
打包后 LZMA2 极限压到 ~60 MB（实测 ffmpeg 单文件 98 MB -> 26 MB），
一次性下载解压即可，比分开下省心得多。

用法：
    python make_runtime_pack.py                  # 生成 runtime_library.7z
    python make_runtime_pack.py --format zip     # 生成 runtime_library.zip（体积大约两倍）
    python make_runtime_pack.py --dry-run        # 只看会打包什么，不真的压缩

他人还原：
    7z x runtime_library.7z -o<项目目录>
"""

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# (源路径, 压缩包内路径)：保持相对结构，解压到项目根目录就能直接用
RUNTIME_FILES = [
    (Path("ffmpeg.exe"), "ffmpeg.exe"),
    (Path("bin") / "libmpv-2.dll", "bin/libmpv-2.dll"),
]

# 缺失但不影响启动的可选件，缺失就跳过
OPTIONAL_FILES = [
    (Path("ffprobe.exe"), "ffprobe.exe"),
]


def human(size: float) -> str:
    return f"{size / 1024 / 1024:.1f} MB"


def find_7z() -> str:
    """找本机 7-Zip：PATH -> 常见安装目录 -> 注册表。"""
    found = shutil.which("7z") or shutil.which("7za")
    if found:
        return found

    candidates = [
        Path("C:/Program Files/7-Zip/7z.exe"),
        Path("C:/Program Files (x86)/7-Zip/7z.exe"),
        Path("D:/7-Zip/7z.exe"),
        Path("D:/Program Files/7-Zip/7z.exe"),
    ]
    for path in candidates:
        if path.exists():
            return str(path)

    if sys.platform == "win32":
        try:
            output = subprocess.run(
                ["reg", "query", r"HKLM\SOFTWARE\7-Zip", "/v", "Path"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in output.splitlines():
                if "REG_SZ" in line:
                    guess = Path(line.split("REG_SZ")[-1].strip()) / "7z.exe"
                    if guess.exists():
                        return str(guess)
        except Exception:
            pass
    return ""


def collect_items():
    """返回 [(源路径, 包内路径, 大小, 是否必需)]，只收实际存在的文件。"""
    items, missing = [], []
    for source, target in RUNTIME_FILES:
        if source.exists():
            items.append((source, target, source.stat().st_size, True))
        else:
            missing.append((str(source), True))
    for source, target in OPTIONAL_FILES:
        if source.exists():
            items.append((source, target, source.stat().st_size, False))
    return items, missing


def main():
    parser = argparse.ArgumentParser(description="打包运行期二进制依赖")
    parser.add_argument("--format", choices=["7z", "zip"], default="7z",
                        help="压缩格式，默认 7z（体积约为 zip 的一半）")
    parser.add_argument("--output", default=None, help="输出文件名")
    parser.add_argument("--dry-run", action="store_true", help="只列清单不打包")
    parser.add_argument("--7z-path", dest="seven_zip", default=None, help="指定 7z.exe 路径")
    args = parser.parse_args()

    items, missing = collect_items()
    if not items:
        print("没有找到任何需要打包的运行时文件，无需执行。")
        return 1

    total = sum(size for _, _, size, _ in items)
    print("=" * 66)
    print("运行时依赖清单")
    print("=" * 66)
    for source, target, size, required in items:
        print(f"  {human(size):>10}  {source}  ->  {target}"
              f"{'' if required else '  （可选）'}")
    for name, required in missing:
        print(f"  [缺失] {name}{'（必需，请自己放一份）' if required else '（可选，跳过）'}")
    print("-" * 66)
    print(f"  合计 {human(total)}")
    print("=" * 66)

    if args.dry_run:
        print("\n[dry-run] 未执行压缩。7z 实测参考：ffmpeg.exe 98 MB -> 26 MB。")
        return 0

    output = Path(args.output or f"runtime_library.{args.format}")
    if output.exists():
        output.unlink()

    if args.format == "zip":
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for source, target, _, _ in items:
                zf.write(source, target)
        with zipfile.ZipFile(output) as zf:
            assert zf.testzip() is None, "zip 完整性校验失败"
        print(f"已生成 {output}（{human(output.stat().st_size)}）")
        print(f"压缩率：{(1 - output.stat().st_size / total) * 100:.1f}%")
        return 0

    seven_zip = args.seven_zip or find_7z()
    if not seven_zip:
        print("没找到 7-Zip，无法生成 7z。可改用 --format zip，或安装 7-Zip 后用 --7z-path 指定。")
        return 1

    print(f"使用 7-Zip：{seven_zip}")
    cmd = [seven_zip, "a", "-t7z", "-mx=9", "-m0=LZMA2", "-md=256m",
           "-mfb=273", "-ms=on", "-mqs=on", "-mmt=on",
           str(output)] + [str(source) for source, _, _, _ in items]
    # 包内保持 bin/ 这样的相对结构：切到项目根目录再压
    result = subprocess.run(cmd, cwd=str(Path(__file__).parent), text=True,
                            capture_output=True, encoding="utf-8", errors="ignore")
    if result.returncode != 0 or not output.exists():
        print(result.stdout)
        print(result.stderr)
        print("压缩失败。")
        return 1

    verify = subprocess.run([seven_zip, "t", str(output)], text=True,
                            capture_output=True, encoding="utf-8", errors="ignore")
    ok = "Everything is Ok" in verify.stdout
    print(f"已生成 {output}（{human(output.stat().st_size)}）")
    print(f"压缩率：{(1 - output.stat().st_size / total) * 100:.1f}%")
    print(f"完整性校验：{'通过' if ok else '未通过，请检查'}")
    print("\n他人使用：7z x runtime_library.7z -o<项目目录>")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
