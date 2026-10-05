import requests
import time
from pathlib import Path

import config
import threading
import math
from concurrent.futures import ThreadPoolExecutor, as_completed


class DownloadManager:
    """Download manager supporting resumable downloads and multi-threading"""

    # 单个分片的最小字节数：避免小文件被切成大量空分片
    MIN_PART_SIZE = 256 * 1024

    def __init__(self, url, output_path, chunk_size=8192, threads=4, resume=False,
                 temp_dir='./temp', headers=None):
        """
        Initialize download manager

        :param url: URL of the file to download
        :param output_path: Full path (including filename) where the final file will be saved
        :param chunk_size: Size of each chunk for streaming
        :param threads: Number of download threads
        :param resume: Whether to resume partially downloaded files
        :param headers: 请求头；为 None 时使用 config.headers（不再修改全局字典）
        """
        self.url = url
        self.output_path = Path(output_path)
        self.filename = self.output_path.name
        self.chunk_size = chunk_size
        self.threads = max(1, int(threads))
        self.resume = resume
        self.headers = headers if headers is not None else config.headers
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.total_size = 0
        self.downloaded = 0
        self.lock = threading.Lock()

    @staticmethod
    def _get_filename_from_url(url):
        filename = url.split('/')[-1]
        if not filename or '.' not in filename:
            filename = f"download_{int(time.time())}.bin"
        return filename

    def _get_file_size(self):
        """Get the total file size from the server. Fallback to GET if HEAD fails."""
        # 尝试 HEAD 请求
        try:
            head_response = requests.head(self.url, allow_redirects=True, headers=self.headers)
            if head_response.status_code == 200 and 'content-length' in head_response.headers:
                return int(head_response.headers['content-length'])
        except Exception:
            pass

        # HEAD 失败，使用 GET 请求但只获取头部（不下载 body）
        try:
            # 发送一个 Range 请求只取第一个字节，服务器通常返回 content-range 和 content-length
            headers = dict(self.headers)
            headers['Range'] = 'bytes=0-0'
            response = requests.get(self.url, stream=True, headers=headers)
            if response.status_code in (200, 206):
                # 206 Partial Content 时，content-length 可能是整个文件大小
                if 'content-range' in response.headers:
                    # content-range: bytes 0-0/12345
                    total = int(response.headers['content-range'].split('/')[-1])
                    return total
                elif 'content-length' in response.headers:
                    return int(response.headers['content-length'])
        except Exception:
            pass

        return None

    def _get_resume_info(self):
        if not self.resume:
            return 0, []

        part_files = list(self.temp_dir.glob(f"{self.filename}.part_*"))
        downloaded = 0
        parts = []
        for part_file in part_files:
            try:
                size = part_file.stat().st_size
                downloaded += size
                parts.append(str(part_file))
            except:
                pass
        return downloaded, parts

    def _download_part(self, start_byte, end_byte, part_num, progress_callback=None):
        part_filename = self.temp_dir / f"{self.filename}.part_{part_num}"

        resume_from = 0
        if self.resume and part_filename.exists():
            resume_from = part_filename.stat().st_size
            start_byte += resume_from

        if start_byte > end_byte:
            return part_filename, True

        headers_copy = dict(self.headers)
        headers_copy['Range'] = f'bytes={start_byte}-{end_byte}'

        try:
            response = requests.get(self.url, stream=True, headers=headers_copy)
            response.raise_for_status()

            mode = 'ab' if resume_from > 0 else 'wb'
            with open(part_filename, mode) as f:
                for chunk in response.iter_content(chunk_size=self.chunk_size):
                    if chunk:
                        f.write(chunk)
                        with self.lock:
                            self.downloaded += len(chunk)
                        if progress_callback:
                            progress_callback()

            return part_filename, True
        except Exception as e:
            print(f"\nFailed to download part {part_num}: {e}")
            return part_filename, False

    def _merge_parts(self, part_files, final_path):
        try:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            with open(final_path, 'wb') as final_file:
                for part_file in sorted(part_files,
                                        key=lambda x: int(str(x).split('_')[-1]) if 'part_' in str(x) else 0):
                    with open(part_file, 'rb') as part:
                        while True:
                            chunk = part.read(self.chunk_size)
                            if not chunk:
                                break
                            final_file.write(chunk)
                    part_file.unlink(missing_ok=True)
            return True
        except Exception as e:
            print(f"Failed to merge parts: {e}")
            return False

    def download(self):
        self.total_size = self._get_file_size()
        if not self.total_size:
            print("Unable to get file size, falling back to single-thread download")
            return self._single_thread_download()

        print(f"File size: {self.total_size:,} bytes ({self.total_size / 1024 / 1024:.2f} MB)")
        print("-" * 60)

        resume_downloaded, _ = self._get_resume_info()
        self.downloaded = resume_downloaded
        if resume_downloaded > 0:
            print(f"Found already downloaded parts: {resume_downloaded:,} bytes ({resume_downloaded / self.total_size:.1%})")

        # 文件太小时减少分片数，避免出现长度为 0 的分片
        max_parts = max(1, self.total_size // self.MIN_PART_SIZE)
        self.threads = max(1, min(self.threads, max_parts))

        part_size = math.ceil(self.total_size / self.threads)
        ranges = []
        for i in range(self.threads):
            start = i * part_size
            if start > self.total_size - 1:
                continue
            end = min((i + 1) * part_size - 1, self.total_size - 1)
            ranges.append((start, end, i))

        start_time = time.time()
        bar_width = 40

        last_print = [0.0]

        def update_progress(force=False):
            # 每个 chunk 都打印会把控制台刷爆，这里做 0.1 秒节流
            nonlocal start_time, bar_width
            now = time.time()
            if not force and now - last_print[0] < 0.1:
                return
            last_print[0] = now
            elapsed_time = now - start_time
            with self.lock:
                progress = self.downloaded / self.total_size
                filled_length = int(bar_width * progress)
                bar = '█' * filled_length + '-' * (bar_width - filled_length)
                speed = self.downloaded / elapsed_time if elapsed_time > 0 else 0
                if speed > 0 and progress < 1:
                    remaining_time = (self.total_size - self.downloaded) / speed
                    time_str = f"Remaining: {remaining_time:.1f}s"
                else:
                    time_str = "Remaining: calculating..."
                print(f'\r[{bar}] {progress:.1%} | {self.downloaded:,}/{self.total_size:,} | {speed / 1024 / 1024:.2f} MB/s | {time_str}',
                      end='', flush=True)

        part_files = []
        success = True
        completed = 0

        with ThreadPoolExecutor(max_workers=self.threads) as executor:
            future_to_part = {
                executor.submit(self._download_part, start, end, part_num, update_progress): part_num
                for start, end, part_num in ranges
            }
            for future in as_completed(future_to_part):
                part_num = future_to_part[future]
                try:
                    part_file, part_success = future.result()
                    part_files.append(part_file)
                    completed += 1
                    if not part_success:
                        success = False
                except Exception as e:
                    print(f"\nException in thread {part_num}: {e}")
                    success = False

        update_progress(force=True)
        print()

        if success and completed == len(ranges):
            if self._merge_parts(part_files, self.output_path):
                total_time = time.time() - start_time
                print(f"✓ Download complete! Total time: {total_time:.2f}s, Average speed: {self.downloaded / total_time / 1024 / 1024:.2f} MB/s")
                return True
            else:
                return False
        else:
            print("✗ Download failed or incomplete")
            return False

    def _single_thread_download(self):
        print("Switching to single-thread download mode...")
        return _original_download(self.url, self.output_path, self.chunk_size, self.headers)


def download(url, output_path=None, chunk_size=8192, threads=1, resume=False, headers=None):
    """统一下载入口：单线程或多线程分片，返回是否成功。"""
    if threads == 1 and not resume:
        return _original_download(url, output_path, chunk_size, headers)

    manager = DownloadManager(url, output_path, chunk_size, threads, resume, headers=headers)
    return manager.download()


def _original_download(url, output_path, chunk_size=8192, headers=None):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    request_headers = headers if headers is not None else config.headers

    total_size = None
    # 尝试 HEAD 获取大小，失败则忽略
    try:
        head_resp = requests.head(url, allow_redirects=True, headers=request_headers)
        if head_resp.status_code == 200 and 'content-length' in head_resp.headers:
            total_size = int(head_resp.headers['content-length'])
    except Exception:
        pass

    try:
        # 发送 GET 请求（实际下载）
        response = requests.get(url, stream=True, allow_redirects=True, headers=request_headers)
        response.raise_for_status()

        # 如果 HEAD 未能获取大小，从 GET 响应头获取
        if total_size is None and 'content-length' in response.headers:
            total_size = int(response.headers['content-length'])

        if total_size:
            print(f"File size: {total_size:,} bytes ({total_size / 1024 / 1024:.2f} MB)")
        print("-" * 60)

        bar_width = 40
        downloaded = 0
        start_time = time.time()
        last_print = 0.0

        with open(output_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    file.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()
                    if now - last_print < 0.1:
                        continue
                    last_print = now

                    if total_size:
                        progress = downloaded / total_size
                        filled_length = int(bar_width * progress)
                        bar = '█' * filled_length + '-' * (bar_width - filled_length)
                        elapsed_time = time.time() - start_time
                        speed = downloaded / elapsed_time if elapsed_time > 0 else 0
                        if speed > 0:
                            remaining_time = (total_size - downloaded) / speed
                            time_str = f"Remaining: {remaining_time:.1f}s"
                        else:
                            time_str = "Remaining: calculating..."
                        print(f'\r[{bar}] {progress:.1%} | {downloaded:,}/{total_size:,} | {speed / 1024 / 1024:.2f} MB/s | {time_str}',
                              end='', flush=True)
                    else:
                        elapsed_time = time.time() - start_time
                        speed = downloaded / elapsed_time if elapsed_time > 0 else 0
                        print(f'\rDownloaded: {downloaded:,} bytes | Speed: {speed / 1024 / 1024:.2f} MB/s',
                              end='', flush=True)

        total_time = time.time() - start_time
        print()
        if total_size:
            if downloaded == total_size:
                print(f"✓ Download complete! Total time: {total_time:.2f}s, Average speed: {downloaded / total_time / 1024 / 1024:.2f} MB/s")
            else:
                print(f"⚠ Download completed but size mismatch: expected {total_size:,}, got {downloaded:,}")
        else:
            print(f"✓ Download complete! Total size: {downloaded:,} bytes, Time: {total_time:.2f}s")
        return True

    except Exception as e:
        print(f"\n✗ Download failed: {e}")
        if output_path.exists():
            output_path.unlink()
            print(f"Deleted incomplete file: {output_path}")
        return False


if __name__ == '__main__':
    url = input("Please input url: ")
    filename = url.split('/')[-1]
    if not filename or '.' not in filename:
        filename = f"download_{int(time.time())}.bin"
    output_path = Path.cwd() / filename
    download(url=url, output_path=output_path, threads=8, resume=True)