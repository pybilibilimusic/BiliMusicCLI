import re

import requests

import config
import downloading
from generate_params import generate_wrid


def downloadMP4(aid: str, cid: str, title: str) -> None:
    """
    Download the MP4 video using the play URL.

    Args:
        aid (str): Video aid.
        cid (str): Content ID.
        title (str): Video title (used as filename).
    """
    # Build parameters for WBI signature
    params = {"aid": aid, "cid": cid}
    w_rid, wts = generate_wrid(params)

    # Construct the play URL request
    play_url = (
        f"https://api.bilibili.com/x/player/wbi/playurl"
        f"?avid={aid}&cid={cid}&qn=16&type=mp4&platform=html5"
        f"&fnver=0&fnval=16&aid={aid}&web_location=1315877"
        f"&w_rid={w_rid}&wts={wts}"
    )

    resp = requests.get(play_url, headers=config.headers, verify=False)
    resp.raise_for_status()
    response = resp.json()

    # Extract the first video segment URL
    video_url = response['data']['durl'][0]['url']

    # Sanitize filename and download
    safe_filename = config.normalize_filename(filename=title)
    downloading.download(video_url, threads=4, filename=f"{safe_filename}.mp4")


class DownloadMP4:
    """
    A class to download Bilibili videos in MP4 format.
    """

    def __init__(self):
        """Initialize the downloader"""
        self.api_url = "https://api.bilibili.com/x/web-interface/view?bvid="
        self.bv_av_pattern = r"(?:BV|av|AV)[0-9A-Za-z]{10,}"

    def _get_video_id(self, bilibili_url: str):
        """
        Extract AV or BV identifier from a Bilibili URL.

        Args:
            bilibili_url (str): The Bilibili video URL.

        Returns:
            str: The extracted AV or BV string.
        """

        try:
            match = re.search(self.bv_av_pattern, bilibili_url)
            return match.group(0)
        except AttributeError:
            print("No valid BV/AV pattern found in the URL.")

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
        aid = data['aid']
        pic = data['pic']
        title = data['title']
        cid = data['cid']
        return aid, pic, title, cid

    def download_video(self, bilibili_url: str) -> str:
        """
        Main entry point: download the video from a Bilibili URL.

        Args:
            bilibili_url (str): The full Bilibili video URL.

        Returns:
            str: The title of the downloaded video.
        """
        # Suppress insecure request warnings (requests version)
        requests.packages.urllib3.disable_warnings()

        video_id = self._get_video_id(bilibili_url)
        aid, pic, title, cid = self._get_video_information(video_id)

        downloadMP4(aid, cid, title)
        return title


if __name__ == '__main__':
    """Test the downloader with user input."""

    url = input("Please enter the Bilibili video URL: ")
    downloader = DownloadMP4()
    downloader.download_video(bilibili_url=url)