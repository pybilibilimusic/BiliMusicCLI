"""
校验 / 重新生成 micro:bit 固件 hex，保证它和 microbit_remote.py 是同一份代码。

为什么要这个东西：
    hex 是二进制，git diff 看不出内容。改了 .py 忘了重新打包的话，
    hex 会一直停留在旧版本，而这件事没有任何人会发现 —— 刷进板子才发现行为对不上。

用法：
    python microbit/hex_sync.py             # 只校验
    python microbit/hex_sync.py --regen     # 校验不过就重新打包（需要装 uflash）

判据（主）：
    hex 旁边放一个指纹文件 microbit_remote.hex.sha256，记录打包时源码的 sha256。
    校验就是比一次哈希 —— 准、快、不会因为改了几行就漏判。

    之所以要这个而不是直接读 hex 内容比对：脚本在 flash 里按约 400 字节分块
    存放，块头会插进源码行的任意位置（实测见过 `上一首        \x06\x05     |`，
    控制字节插在空格中间），所以「逐行连续匹配」永远到不了 100%；更糟的是
    分块切断带来的噪声比「改了几行」的信号还大 —— 实测旧固件靠行命中率
    也有 81%，跟同步状态的 86% 根本分不开。

判据（辅）：
    顺带算一个「ASCII 行命中率」打印出来，用来确认 hex 里确实嵌着这份源码
    （防止有人只更新了指纹文件却没更新 hex）。同步时通常 85~90%，
    只作参考，不参与判定。

退出码：
    0 同步（或缺 hex 但已用 --regen 生成成功）
    1 不同步
    2 缺少依赖 / 无法完成
"""

import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIRMWARE = os.path.join(HERE, "microbit_remote.py")
HEXFILE = os.path.join(HERE, "microbit_remote.hex")
# 打包时源码的 sha256。文本文件，git diff 看得见 —— 这正是二进制 hex 做不到的。
SIGFILE = HEXFILE + ".sha256"

# 辅助判据的下限。脚本在 flash 里分块存放，块头会切断约 1/7 的行，
# 所以同步状态下也只有 85~90%。这个阈值只用来发现「指纹文件被改了但 hex 没换」
# 这类事故，不用来判断版本新旧 —— 那件事交给指纹。
MIN_HIT_RATIO = 0.70

# uflash 打出来的 MicroPython runtime 版本（用 `python -c "import uflash;
# print(uflash.MICROPYTHON_V2_VERSION)"` 查）。beta.5 是 2021 年的老版本，
# V2.2 板子建议改用官方编辑器粘贴 .py，别刷这个 hex。
UFLASH_RUNTIME_NOTE = "MicroPython 2.0.0-beta.5（uflash 内置）"


def read_hex_payload(path):
    """把 hex 文件里所有数据记录拼成一个 bytes（不管地址，脚本区是连续的）。"""
    import binascii

    out = bytearray()
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith(":"):
                continue
            try:
                raw = binascii.unhexlify(line[1:])
            except Exception:
                continue
            # Intel HEX：00 = 数据记录，其余是地址/结束标记
            if len(raw) >= 4 and raw[3] == 0x00:
                out += raw[4:4 + raw[0]]
    return bytes(out)


def source_digest(path=FIRMWARE):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def line_hit_ratio(hex_path, source):
    """辅助指标：源码里有多少 ASCII 行能在 hex 的脚本区里原样找到。"""
    if not os.path.exists(hex_path):
        return None
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    ascii_lines = [line for line in lines if line.isascii()]
    if not ascii_lines:
        return None

    payload = read_hex_payload(hex_path)
    start = payload.find(ascii_lines[0].encode())
    end = payload.rfind(ascii_lines[-1].encode())
    if start < 0 or end <= start:
        return 0.0
    blob = payload[start:end + len(ascii_lines[-1])]
    hit = sum(1 for line in ascii_lines if line.encode() in blob)
    return hit / len(ascii_lines)


def check(hex_path=HEXFILE, firmware_path=FIRMWARE):
    """
    返回 (是否同步, 说明文字, 行命中率)。
    hex 或指纹文件不存在时返回 (None, ...)，交给调用方决定是报错还是跳过。
    """
    if not os.path.exists(hex_path):
        return None, "hex 不存在：%s" % os.path.relpath(hex_path, ROOT), 0.0
    if not os.path.exists(firmware_path):
        return None, "源码不存在：%s" % firmware_path, 0.0

    with open(firmware_path, encoding="utf-8") as handle:
        source = handle.read()

    # 主判据：打包时记录的源码指纹
    sigfile = hex_path + ".sha256"
    if not os.path.exists(sigfile):
        return None, "没有指纹文件 %s，无法判定（--regen 会补上）" % os.path.basename(sigfile), 0.0
    with open(sigfile, encoding="utf-8") as handle:
        recorded = handle.read().split()
    recorded = recorded[0] if recorded else ""
    actual = source_digest(firmware_path)

    ratio = line_hit_ratio(hex_path, source) or 0.0
    if recorded != actual:
        return False, ("源码指纹不一致（打包时 %s...，现在 %s...）"
                       % (recorded[:12], actual[:12])), ratio

    # 指纹一致，再用行命中率确认 hex 里真的嵌着这份代码
    if ratio < MIN_HIT_RATIO:
        return False, ("指纹一致，但 hex 内容对不上（行命中 %.0f%%）—— hex 可能被换过"
                       % (ratio * 100)), ratio
    return True, "指纹一致，行命中 %.0f%%" % (ratio * 100), ratio


def regen(hex_path=HEXFILE, firmware_path=FIRMWARE):
    """用 uflash 重新打包。uflash 固定输出到目标目录的 micropython.hex。"""
    try:
        import uflash  # noqa: F401
    except ImportError:
        return False, "没装 uflash，先 pip install uflash"

    outdir = os.path.join(ROOT, "temp", "hexout")
    os.makedirs(outdir, exist_ok=True)
    cmd = [sys.executable, "-m", "uflash", firmware_path, outdir]
    proc = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    if proc.returncode != 0:
        return False, "uflash 失败：%s" % (proc.stderr or proc.stdout).strip()[:200]

    built = os.path.join(outdir, "micropython.hex")
    if not os.path.exists(built):
        return False, "uflash 没产出文件：%s" % built

    with open(built, "rb") as handle:
        data = handle.read()
    with open(hex_path, "wb") as handle:
        handle.write(data)
    os.remove(built)
    try:
        os.rmdir(outdir)
    except OSError:
        pass

    # 指纹必须跟着 hex 一起写，否则下次校验无从判断
    digest = source_digest(firmware_path)
    with open(hex_path + ".sha256", "w", encoding="utf-8") as handle:
        handle.write("%s  %s\n" % (digest, os.path.basename(firmware_path)))

    return True, "已重新生成 %s（%d 字节，runtime %s），指纹 %s..." % (
        os.path.relpath(hex_path, ROOT), len(data), UFLASH_RUNTIME_NOTE, digest[:12])


def main():
    args = [a for a in sys.argv[1:]]
    regen_mode = "--regen" in args

    print("=" * 72)
    print("micro:bit 固件 hex 同步校验")
    print("源码：%s" % os.path.relpath(FIRMWARE, ROOT))
    print("固件：%s" % os.path.relpath(HEXFILE, ROOT))
    print("=" * 72)

    ok, note, _ = check()
    if ok is None:
        print("[-] 跳过：%s" % note)
        if not regen_mode:
            print("    想生成就加 --regen")
            return 2
    elif ok:
        print("[OK] hex 与源码同步 —— %s" % note)
        print("    runtime：%s" % UFLASH_RUNTIME_NOTE)
        if regen_mode:
            print("    （--regen：已是最新，无需重打包）")
        return 0
    else:
        print("[X]  hex 与源码不同步 —— %s" % note)

    if not regen_mode:
        print("\n建议：python microbit/hex_sync.py --regen")
        return 1

    print("\n正在重新打包...")
    done, msg = regen()
    print(("[OK] " if done else "[X]  ") + msg)
    if not done:
        return 2

    ok, note, _ = check()
    print("[%s] 复验：%s" % ("OK" if ok else "X ", note))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
