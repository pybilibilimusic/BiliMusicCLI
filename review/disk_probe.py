"""只读探测：卷落在哪块物理盘、是不是 SSD / HDD。

用途：判断「把模型从 C 盘挪到别的盘能不能变快」。如果两个卷其实在
同一块物理盘上（只是不同分区），搬 900MB 纯属白费力气。

直接用 Win32 DeviceIoControl，纯只读、不需要管理员权限、不装任何依赖：
- IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS -> 卷对应的 PhysicalDrive 编号
- IOCTL_STORAGE_QUERY_PROPERTY(StorageDeviceSeekPenaltyProperty=7)
  -> IncursDeviceSeekPenalty: SSD=False, HDD=True（Windows 判定 SSD 的官方口径）

用法：python review/disk_probe.py
"""
import ctypes
import struct
from ctypes import wintypes

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
STORAGE_DEVICE_SEEK_PENALTY_PROPERTY = 7

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                            wintypes.HANDLE]
k32.CreateFileW.restype = wintypes.HANDLE
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
k32.DeviceIoControl.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [wintypes.HANDLE]


def open_device(path):
    h = k32.CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                        OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE:
        raise OSError("CreateFileW %s 失败, err=%s" % (path, ctypes.get_last_error()))
    return h


def ioctl(h, code, in_buf, out_size):
    out = ctypes.create_string_buffer(out_size)
    ret = wintypes.DWORD()
    ok = k32.DeviceIoControl(h, code, in_buf, len(in_buf) if in_buf else 0,
                             out, out_size, ctypes.byref(ret), None)
    if not ok:
        raise OSError("DeviceIoControl 0x%X 失败, err=%s" %
                      (code, ctypes.get_last_error()))
    return out.raw[:ret.value]


def volume_disks(letter):
    h = open_device("\\\\.\\%s:" % letter)
    try:
        data = ioctl(h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, 4096)
    finally:
        k32.CloseHandle(h)
    count = struct.unpack_from("<I", data, 0)[0]
    return [struct.unpack_from("<I", data, 4 + i * 20)[0]
            for i in range(count)]


def physical_info(disk_no):
    h = open_device("\\\\.\\PhysicalDrive%d" % disk_no)
    try:
        # STORAGE_PROPERTY_QUERY = DWORD PropertyId + DWORD QueryType +
        # UCHAR AdditionalParameters[1]，sizeof 为 12（不是 8），少 4 字节
        # 会返回 ERROR_BAD_LENGTH(24)。
        query = struct.pack("<II4x", STORAGE_DEVICE_SEEK_PENALTY_PROPERTY, 0)
        data = ioctl(h, IOCTL_STORAGE_QUERY_PROPERTY, query, 64)
    finally:
        k32.CloseHandle(h)
    version, size, penalty = struct.unpack_from("<II?", data, 0)
    return ("SSD / 无寻道延迟" if not penalty else "HDD / 有寻道延迟",
            version, size)


def main():
    print("卷 -> 物理盘 -> 介质")
    print("-" * 60)
    cache = {}
    for letter in ("C", "I"):
        try:
            disks = volume_disks(letter)
        except OSError as e:
            print("%s: 查询失败 %s" % (letter, e))
            continue
        for d in disks:
            if d not in cache:
                try:
                    cache[d] = physical_info(d)[0]
                except OSError as e:
                    cache[d] = "未知(%s)" % e
            print("%s:  -> PhysicalDrive%d  -> %s" % (letter, d, cache[d]))
    print("-" * 60)
    cs = set()
    iss = set()
    for letter, target in (("C", cs), ("I", iss)):
        try:
            target.update(volume_disks(letter))
        except OSError:
            pass
    if cs and iss and cs == iss:
        print("结论：C: 和 I: 在【同一块物理盘】上，换分区不会改变读取速度。")
    elif cs and iss:
        print("结论：C: 和 I: 是【不同的物理盘】，换盘才可能改变速度。")


if __name__ == "__main__":
    main()
