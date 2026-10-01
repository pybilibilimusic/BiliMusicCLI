import json
import logging
import os
import re
import subprocess
import time
import urllib3
from pathlib import Path

import requests

import config
import downloading
from generate_params import generate_wrid

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class VideoDownloader:

    def __init__(self, cookie_file: str = "bilibili_cookies.json",
                 ffmpeg_path: str = "./ffmpeg.exe",
                 threads: int = 4):
        self.cookie_file = Path(cookie_file)
        self.ffmpeg_path = Path(ffmpeg_path)
        self.threads = threads
        self.cookie_dict = {}
        self.headers = config.headers

    def load_cookies(self) -> bool:
        if not self.cookie_file.exists():
            print(f"⚠️ 未找到 Cookie 文件 {self.cookie_file}，无法获取高清流")
            print("   请先运行 browser.py 扫码登录以生成 Cookie")
            return False

        with open(self.cookie_file, 'r', encoding='utf-8') as f:
            cookies = json.load(f)

        self.cookie_dict = {c['name']: c['value'] for c in cookies}
        print(f"✅ 已加载 {len(self.cookie_dict)} 个 Cookie")
        return True

    @staticmethod
    def extract_video_id(url: str) -> str:
        """从 URL 中提取 BV 或 AV 号。"""
        match = re.search(r"(?:BV|av|AV)[0-9A-Za-z]{10,}", url)
        if not match:
            raise ValueError(f"无法从 URL 中提取视频 ID: {url}")
        return match.group(0)

    def fetch_video_info(self, video_id: str) -> dict:
        """
        调用 B站 API 获取视频元信息。

        Returns:
            dict: 包含 aid, cid, title, pic
        """
        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={video_id}"
        resp = requests.get(
            api_url,
            headers=self.headers,
            cookies=self.cookie_dict,
            verify=False,
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()['data']
        return {
            'aid': data['aid'],
            'cid': data['cid'],
            'title': data['title'],
            'pic': data['pic'],
        }

    # ---------- 播放流 ----------

    def fetch_dash_streams(self, aid: int, cid: int,
                           quality: int = 80) -> dict:
        """
        请求 PC 端 playurl 接口，获取 DASH 流信息。

        Args:
            aid: 视频 aid
            cid: 视频 cid
            quality: 目标清晰度 qn 值（默认 80 即 1080P）

        Returns:
            dict: {'video': [...], 'audio': [...]}
        """
        w_rid, wts = generate_wrid({'aid': aid, 'cid': cid})

        url = (
            f"https://api.bilibili.com/x/player/wbi/playurl?"
            f"avid={aid}&cid={cid}&qn={quality}&fnval=4048&fnver=0"
            f"&fourk=1&platform=pc"
            f"&w_rid={w_rid}&wts={wts}"
        )

        headers = self.headers.copy()
        headers['Referer'] = f"https://www.bilibili.com/video/av{aid}"

        resp = requests.get(
            url,
            headers=headers,
            cookies=self.cookie_dict,
            verify=False,
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json().get('data', {})

        dash = data.get('dash')
        if not dash:
            raise RuntimeError(
                "未返回 DASH 流。可能原因：Cookie 过期或该视频不支持 DASH。\n"
                f"返回的 data keys: {list(data.keys())}"
            )

        return {
            'video': dash.get('video', []),
            'audio': dash.get('audio', []),
        }

    # ---------- 流选择 ----------

    @staticmethod
    def select_video_stream(streams: list, quality: int = 80) -> dict:
        """
        选择视频流。

        优先选目标清晰度，若无则选最接近的；
        同一清晰度下优先选 avc1（H.264），兼容性最好。
        """
        if not streams:
            raise RuntimeError("视频流列表为空")

        available_ids = sorted({s['id'] for s in streams}, reverse=True)
        print(f"可用视频清晰度 id：{available_ids}")

        target_id = quality if quality in available_ids else available_ids[0]
        candidates = [s for s in streams if s['id'] == target_id]

        chosen = next(
            (s for s in candidates if 'avc1' in s.get('codecs', '')),
            candidates[0]
        )
        print(f"✅ 选中视频流：id={chosen['id']}, "
              f"{chosen['width']}x{chosen['height']}, "
              f"codec={chosen['codecs']}")
        return chosen

    @staticmethod
    def select_audio_stream(streams: list) -> dict:
        """选择音频流，按 id 降序取最高码率。"""
        if not streams:
            raise RuntimeError("音频流列表为空")

        chosen = sorted(streams, key=lambda s: s['id'], reverse=True)[0]
        print(f"✅ 选中音频流：id={chosen['id']}, "
              f"codec={chosen['codecs']}")
        return chosen

    # ---------- 下载与合并 ----------

    def _download_stream(self, url: str, output_path: Path, label: str):
        """下载单个流，带标签提示。"""
        print("=" * 60)
        print(f"开始下载{label} → {output_path.name}")

        # 让 downloading 模块使用带 Cookie 的 headers
        config.headers.update(self.headers)

        downloading.download(
            url,
            output_path=output_path,
            threads=self.threads,
            resume=True
        )

    def _merge_streams(self, video_path: Path, audio_path: Path,
                       output_path: Path):
        """用 FFmpeg 合并音视频流。"""
        print("=" * 60)
        print("正在合并音视频...")

        ffmpeg = self.ffmpeg_path if self.ffmpeg_path.exists() else Path("ffmpeg")

        cmd = [
            str(ffmpeg),
            "-i", str(video_path),
            "-i", str(audio_path),
            "-c", "copy",
            str(output_path),
            "-y"
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            encoding='utf-8',
            errors='ignore'
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"FFmpeg 合并失败，返回码 {result.returncode}\n"
                f"{result.stderr[-500:] if result.stderr else ''}"
            )

        print(f"✅ 合并完成：{output_path}")

    # ---------- 主入口 ----------

    def download(self, url: str, output_dir: str = "./temp",
                 quality: str = "1080p") -> Path:
        """
        下载 B站 视频（含高清流）。

        Args:
            url: B站 视频 URL
            output_dir: 输出目录
            quality: 清晰度字符串，可选 '1080p', '720p', '480p', '360p'

        Returns:
            Path: 合并后的 MP4 文件路径
        """
        quality_qn = config.quality_map.get(quality.lower(), 80)

        # 1. 加载 Cookie
        self.load_cookies()

        # 2. 获取视频信息
        video_id = self.extract_video_id(url)
        info = self.fetch_video_info(video_id)
        logging.info(f"视频信息：aid={info['aid']}, cid={info['cid']}, "
                     f"title={info['title']}")

        # 3. 获取 DASH 流
        streams = self.fetch_dash_streams(info['aid'], info['cid'],
                                          quality=quality_qn)

        # 4. 选择流
        video_stream = self.select_video_stream(streams['video'],
                                                quality=quality_qn)
        audio_stream = self.select_audio_stream(streams['audio'])

        # 5. 准备临时文件路径
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        safe_title = re.sub(r'[<>:"/\\|?*《》]', '', info['title'])
        output_path = output_dir / f"{safe_title}.mp4"
        temp_video = output_dir / f"{safe_title}.video.m4s"
        temp_audio = output_dir / f"{safe_title}.audio.m4s"

        # 6. 下载
        self._download_stream(video_stream['baseUrl'], temp_video, "视频流")
        self._download_stream(audio_stream['baseUrl'], temp_audio, "音频流")

        # 7. 合并
        self._merge_streams(temp_video, temp_audio, output_path)

        # 8. 清理临时文件
        temp_video.unlink(missing_ok=True)
        temp_audio.unlink(missing_ok=True)

        return output_path


# ---------- 独立运行入口 ----------

def _setup_logging():
    """配置日志（仅在独立运行时使用）。"""
    if os.name == 'nt':
        os.system('chcp 65001 >nul')
    log_dir = Path("./log")
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{int(time.time())}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        filename=log_path
    )


if __name__ == '__main__':
    _setup_logging()
    url = input("请输入视频 URL: ").strip()
    downloader = VideoDownloader()
    result = downloader.download(url)
    print(f"\n最终输出：{result}")
'''    
with open('bilibili_cookies.json', 'r', encoding='utf-8') as f:
    cookie_dict = json.load(f)

resp = requests.get(url, headers=headers, cookies=cookie_dict)
'''