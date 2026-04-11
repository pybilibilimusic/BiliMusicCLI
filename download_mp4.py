import re
import time
import json
import hashlib
import requests
from urllib.parse import urlparse, parse_qs

import config
import downloading

class DownloadAudio:
    """Main class for downloading audio from Bilibili videos."""

    def __init__(self):
        # WBI signature keys (static for now)
        self.img_key = '7cd084941338484aae1ad9425b84077c'
        self.sub_key = '4932caff0ff746eab6f01bf08b70ac45'
        self.play_url = "https://api.bilibili.com/x/player/playurl"

    def wbi_sign(self, aid, cid):
        """
        Generate WBI signature (w_rid) for the given aid and cid.

        Args:
            aid (int): Video aid.
            cid (int): Content ID of the specific part.

        Returns:
            dict: Parameters including the generated 'w_rid' signature.
        """
        params = {
            "avid": aid,
            "cid": cid,
            "fnval": 4048,
            "mid": 0,
            "platform": "pc",
            "qn": 30280,
            "wts": int(time.time()),
        }
        # Sort parameters alphabetically as required by WBI signature
        sorted_params = sorted(params.items())
        query = '&'.join([f"{k}={v}" for k, v in sorted_params])
        sign_str = query + self.img_key + self.sub_key
        params["w_rid"] = hashlib.md5(sign_str.encode()).hexdigest()
        return params

    def get_audio_url(self, aid, cid):
        """
        Request the Bilibili API to obtain the direct audio stream URL.

        Args:
            aid (int): Video aid.
            cid (int): Content ID.

        Returns:
            str: Direct audio stream URL (baseUrl of the first audio stream).
        """
        params = self.wbi_sign(aid, cid)
        resp = requests.get(self.play_url, params=params, headers=config.headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        audio_list = data.get("data", {}).get("dash", {}).get("audio", [])
        if not audio_list:
            raise Exception("No audio stream found in API response")
        return audio_list[0].get("baseUrl")

    def download_video(self, video_url, threads=8):
        """
        Parse the video page, retrieve audio URL, and download the audio file.
        Supports multi-part videos (e.g., ?p=5) by selecting the correct part.

        Args:
            video_url (str): Full Bilibili video URL.
            threads (int, optional): Number of download threads. Defaults to 8.
        """
        page_html = requests.get(video_url, headers=config.headers).text

        # Extract __INITIAL_STATE__ JSON from page
        match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', page_html, re.S)
        if not match:
            raise Exception("Could not find __INITIAL_STATE__ in page")
        data = json.loads(match.group(1))
        video_data = data.get('videoData', {})
        aid = video_data.get('aid')
        if not aid:
            raise Exception("aid not found in videoData")

        # Handle multi-part videos
        pages = video_data.get('pages', [])
        if pages:
            parsed = urlparse(video_url)
            query_params = parse_qs(parsed.query)
            p_str = query_params.get('p', ['1'])[0]
            try:
                p = int(p_str)
            except ValueError:
                p = 1
            p = max(1, min(p, len(pages)))  # clamp to valid range
            cid = pages[p - 1].get('cid')
            part_title = pages[p - 1].get('part', '')
        else:
            cid = video_data.get('cid')
            part_title = ''

        audio_url = self.get_audio_url(aid, cid)

        # Build filename: video title + optional part title
        video_title = video_data.get('title', '未命名')
        if part_title:
            base_name = f"{video_title} _ {part_title}"
        else:
            base_name = video_title
        filename = config.normalize_filename(base_name)

        downloading.download(audio_url, threads=threads, filename=f"{filename}.m4s")


if __name__ == "__main__":
    """The test code"""
    url = input("Please enter the url of your video: ")
    downloader = DownloadAudio()
    downloader.download_video(url, threads=8)