import os, sys, ctypes
import re
from pathlib import Path
from ctypes import wintypes

import config


def enable_ansi_support():
    """Enable ANSI escape sequence support on Windows 10+ console."""
    if sys.platform != 'win32':
        return
    try:
        kernel32 = ctypes.windll.kernel32
        h_out = kernel32.GetStdHandle(-11)
        if h_out == -1 or h_out is None:
            return
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(h_out, ctypes.byref(mode)):
            return
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING == 0:
            new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(h_out, new_mode)
    except Exception:
        pass

def setup_mpv_path():
    base_dir = Path(__file__).parent
    dll_dir = base_dir / "bin"
    if dll_dir.exists():
        os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")
    else:
        if not (base_dir / "libmpv-2.dll").exists():
            print("Warning: libmpv-2.dll not found, please put it in bin/ folder.")

# 清洗后仍可能漏网的装饰符号，兜底删除，保证最终文件名干净
RESIDUAL_SYMBOLS = "《》〈〉【】〖〗「」『』"


def normalize_filename(filename):
    """把标题转成合法且干净的文件名。"""
    for old_char, new_char in config.char_map.items():
        filename = filename.replace(old_char, new_char)
    # 兜底：删掉清洗阶段可能漏下的书名号 / 引号类符号
    filename = re.sub("[" + RESIDUAL_SYMBOLS + "]", "", filename)
    filename = re.sub(config.windows_illegal_chars, "_", filename)
    filename = re.sub(r'_+', '_', filename)
    filename = re.sub(r'\s+', ' ', filename)
    filename = filename.strip(' _.')
    return filename