import re
import time
import hashlib
import requests
from urllib.parse import urlparse, parse_qs
from pathlib import Path

import config
import downloading


class DownloadAudio:
    """Main class for downloading audio from Bilibili videos."""

    def __init__(self, threads=4, temp_dir='./temp',m4s_temp='./m4s_temp'):
        self.img_key = '7cd084941338484aae1ad9425b84077c'
        self.sub_key = '4932caff0ff746eab6f01bf08b70ac45'
        self.play_url = "https://api.bilibili.com/x/player/playurl"
        self.api_url = "https://api.bilibili.com/x/web-interface/view?bvid="
        self.bv_av_pattern = r'(BV[a-zA-Z0-9]+)'

        self.m4s_temp = Path(m4s_temp)
        self.threads = threads
        self.temp_dir = Path(temp_dir)

    def _get_video_information(self, video_id: str):
        """
        Fetch video metadata from the Bilibili API.

        Args:
            video_id (str): BV or AV identifier of the video.

        Returns:
            tuple: (aid, pic, title, cid)
                - aid (str): Video aid (For internal program use only).
                - pic (str): Cover image URL (Interface for later development).
                - title (str): Video title (For internal program use only).
                - cid (str): Content ID (For internal program use only).
        """
        api_url = self.api_url + video_id
        json_data = requests.get(api_url, headers=config.headers, verify=False).json()
        data = json_data['data']
        aid,cid,title,pages = data['aid'],data['cid'],data['title'],data['pages']
        return aid,cid,title,pages

    def _wbi_sign(self, aid, cid):
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
        params = self._wbi_sign(aid, cid)
        resp = requests.get(self.play_url, params=params, headers=config.headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        audio_list = data.get("data", {}).get("dash", {}).get("audio", [])
        if not audio_list:
            raise Exception("No audio stream found in API response")
        return audio_list[0].get("baseUrl")

    def download_audio(self, video_url):
        """
        Parse the video page, retrieve audio URL, and download the audio file.
        Supports multi-part videos (e.g., ?p=5) by selecting the correct part.

        Args:
            video_url (str): Full Bilibili video URL.
        """
        #Get aid,cid
        match = re.search(self.bv_av_pattern, video_url)
        video_id = match.group(0)
        aid,first_cid,title,pages = self._get_video_information(video_id)

        # Determine correct cid and part title for multi-part videos
        parsed = urlparse(video_url)
        query_params = parse_qs(parsed.query)
        p_str = query_params.get('p', ['1'])[0]
        try:
            p = int(p_str)
        except ValueError:
            p = 1
        if pages and 1 <= p <= len(pages):
            cid = pages[p - 1].get('cid')
            part_title = pages[p - 1].get('part', '')
        else:
            cid = first_cid
            part_title = ''

        # Get audio stream URL
        audio_url = self.get_audio_url(aid, cid)

        # Build filename: video title + optional part title
        if part_title and part_title != title:
            filename = f"{title} - {part_title}"
        else:
            filename = title
        filename = config.normalize_filename(filename)
        output_path = self.m4s_temp / f"{filename}.m4s"

        config.headers["Referer"] = video_url
        downloading.download(audio_url,
                             threads=self.threads,
                             output_path=output_path,
                             resume=True)
        return output_path


if __name__ == "__main__":
    """The test code"""
    url = input("Please enter the url of your video: ")
    downloader = DownloadAudio(threads=16, temp_dir='./temp')
    downloader.download_audio(url)