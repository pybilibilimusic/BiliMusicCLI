"""
B 站音频流下载。

流程：解析 BV 号 → 取视频元信息（aid/cid/分P/时长）→ WBI 签名请求 playurl
→ 取最高码率音频流 → 多线程分片下载为 .m4s。
"""

import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

import config
import downloading
import title_cleaner
import utils
from generate_params import FALLBACK_KEYS, fallback_warning, refresh_wbi_keys, \
    set_keys, sign_params, using_fallback


class DownloadAudio:
    """Main class for downloading audio from Bilibili videos."""

    PLAY_URL = "https://api.bilibili.com/x/player/wbi/playurl"
    VIEW_URL = "https://api.bilibili.com/x/web-interface/view?bvid="
    BV_PATTERN = r"(BV[a-zA-Z0-9]+)"

    MAX_DURATION = 900          # 超过 15 分钟基本不是单曲（循环版 / 直播录像）
    DEFAULT_TIMEOUT = 10

    def __init__(self, threads=4, temp_dir="./temp", m4s_temp="./m4s_temp",
                 cookie_file=None, timeout=0):
        self.m4s_temp = Path(m4s_temp)
        self.threads = max(1, int(threads))
        self.temp_dir = Path(temp_dir)
        self.timeout = timeout if timeout and timeout > 0 else self.DEFAULT_TIMEOUT
        # 登录后才能拿到高码率（320k / Hi-Res）音频流
        self._cookie_header = config.cookie_header(cookie_file)

    # ---------- 工具 ----------

    def _headers(self, referer: str = None) -> dict:
        """构造请求头：不再污染全局 config.headers，每次返回副本。"""
        headers = dict(config.headers)
        headers.update(self._cookie_header)
        if referer:
            headers["Referer"] = referer
        return headers

    # ---------- 接口 ----------

    def _get_video_information(self, video_id: str, referer: str = None) -> dict:
        """
        获取视频元信息。

        Returns:
            dict: aid / cid / title / pages / duration
        """
        response = requests.get(
            self.VIEW_URL + video_id,
            headers=self._headers(referer),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(
                f"获取视频信息失败: {payload.get('code')} {payload.get('message')}"
            )

        data = payload["data"]
        return {
            "aid": data["aid"],
            "cid": data["cid"],
            "title": data["title"],
            "pages": data.get("pages", []),
            "duration": data.get("duration", 0),
        }

    def get_audio_url(self, aid, cid, referer: str = None) -> str:
        """用 WBI 签名请求 playurl，返回码率最高的音频流地址。"""
        base_params = {
            "avid": aid,
            "cid": cid,
            "fnver": 0,
            "fnval": 4048,     # DASH 格式
            "fourk": 1,
            "platform": "pc",
            "qn": 30280,       # 目标：320kbps
        }

        payload = self._request_playurl(base_params, referer)

        if payload.get("code") != 0:
            # 多半是密钥过期，强制刷新后再试一次
            refresh_wbi_keys(timeout=self.timeout)
            payload = self._request_playurl(base_params, referer)

        if payload.get("code") != 0:
            # 还是不行就依次退化到内置的历史密钥，谁通过就用谁
            for candidate in FALLBACK_KEYS:
                set_keys(candidate)
                payload = self._request_playurl(base_params, referer)
                if payload.get("code") == 0:
                    break

        if payload.get("code") == 0 and using_fallback():
            print(fallback_warning())

        if payload.get("code") != 0:
            raise RuntimeError(
                f"获取音频流失败: {payload.get('code')} {payload.get('message')}"
            )

        audio_list = (payload.get("data") or {}).get("dash", {}).get("audio") or []
        if not audio_list:
            raise RuntimeError("接口未返回音频流（可能需要登录 Cookie）")

        best = max(audio_list, key=lambda item: item.get("bandwidth") or item.get("id") or 0)
        return best.get("baseUrl")

    def _request_playurl(self, base_params: dict, referer: str = None) -> dict:
        """签名并请求 playurl 接口。"""
        params = sign_params(base_params, timeout=self.timeout)
        response = requests.get(
            self.PLAY_URL,
            params=params,
            headers=self._headers(referer),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    # ---------- 主流程 ----------

    def download_audio(self, video_url: str) -> Path:
        """
        下载视频的音频流为 .m4s。

        支持多 P 视频（?p=5 会下载对应分 P）。失败时抛异常，不返回无效路径。
        """
        match = re.search(self.BV_PATTERN, video_url)
        if not match:
            raise ValueError(f"无法从链接中解析 BV 号: {video_url}")
        video_id = match.group(1)

        info = self._get_video_information(video_id, referer=video_url)
        duration = info["duration"] or 0
        if duration > self.MAX_DURATION:
            raise RuntimeError(
                f"视频时长 {duration}s 超过上限 {self.MAX_DURATION}s，可能是循环版或长视频"
            )

        # 多 P 视频：按 ?p= 取对应的 cid 与分 P 标题
        page = self._parse_page(video_url)
        pages = info["pages"] or []
        if pages and 1 <= page <= len(pages):
            cid = pages[page - 1].get("cid")
            part_title = pages[page - 1].get("part", "")
        else:
            cid = info["cid"]
            part_title = ""

        audio_url = self.get_audio_url(info["aid"], cid, referer=video_url)
        if not audio_url:
            raise RuntimeError("未取到音频流地址")

        raw_title = info["title"]
        # 标题 -> 干净歌名：去【】标签、去引号歌词、去噪声词，失败回退原始标题
        title = title_cleaner.clean_song_title(raw_title)
        part = title_cleaner.clean_song_title(part_title) if part_title else ""
        filename = f"{title} - {part}" if part and part != title else title
        output_path = self.m4s_temp / f"{utils.normalize_filename(filename)}.m4s"

        success = downloading.download(
            audio_url,
            threads=self.threads,
            output_path=output_path,
            resume=True,
            headers=self._headers(referer=video_url),
        )
        if not success or not output_path.exists():
            raise RuntimeError(f"音频下载失败: {output_path}")

        return output_path

    @staticmethod
    def _parse_page(video_url: str) -> int:
        """解析 ?p= 参数，默认第 1 个分 P。"""
        query = parse_qs(urlparse(video_url).query)
        try:
            return int(query.get("p", ["1"])[0])
        except ValueError:
            return 1


if __name__ == "__main__":
    url = input("Please enter the url of your video: ").strip()
    downloader = DownloadAudio(threads=16, temp_dir="./temp")
    print(downloader.download_audio(url))
