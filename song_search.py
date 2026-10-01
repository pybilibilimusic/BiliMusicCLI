from urllib.parse import quote
from bs4 import BeautifulSoup
import requests
import re
import config

class SongSearch:
    def __init__(self, prompt, timeout):
        self.keyword_url = 'https://search.bilibili.com/all?keyword='
        self.video_id_pattern = r'href="//www\.bilibili\.com/video/([^/]+)/'
        self.title_pattern = r'title="(.*?)">'
        self.blacklist_word = ["纯享", "循环"]
        self.high_quality_keywords = ["Hi-Res无损", "录音棚", "官方", "母带", "24bit", "无损音质"]
        self.prompt = quote(prompt)
        self.timeout = timeout if timeout > 0 else 5

    def _search(self,retry=0):
        search_url = self.keyword_url + self.prompt
        response = requests.get(url=search_url, headers=config.headers,timeout=5)
        search_html = BeautifulSoup(response.text, 'html.parser')
        search_list = str(search_html.find_all('div', class_='bili-video-card__info--right'))
        video_title = re.findall(self.title_pattern, search_list)
        video_id = re.findall(self.video_id_pattern, search_list)
        final_video_title = video_title[retry*5:5+retry*5]
        final_video_id = video_id[retry*5:5+retry*5]
        return final_video_title, final_video_id

    def _get_priority_score(self, title):
        for bw in self.blacklist_word:
            if bw in title:
                return -999
        score = 0
        for kw in self.high_quality_keywords:
            if kw in title:
                score += 10
        return score

    def _get_best_index(self, titles):
        best_idx = 0
        best_score = -1
        for i, t in enumerate(titles):
            s = self._get_priority_score(t)
            if s == -999:
                continue
            if s > best_score:
                best_score = s
                best_idx = i
        if best_score == -1:
            return -1
        return best_idx

    def search(self):
        """语音模式：不等待输入，返回 (best_bvid, best_title, full_list)"""
        video_titles, video_ids = self._search()
        if not video_ids:
            return None, None, []
        best_index = self._get_best_index(video_titles)
        if best_index == -1:
            return None, None, []
        best_id = video_ids[best_index]
        best_title = video_titles[best_index]
        full_list = [{'bvid': video_ids[i], 'title': video_titles[i]} for i in range(len(video_ids))]
        return best_id, best_title, full_list

    def interactive_search(self,retry=0):
        """手动搜索模式：打印列表，阻塞等待用户输入数字，支持 r 刷新、q 退出。返回 (bvid, title)"""
        print("Please wait a moment, searching...")
        video_titles, video_ids = self._search(retry=retry)
        if not video_ids:
            print("No results found.")
            return None, None

        best_index = self._get_best_index(video_titles)
        print("\n" + "=" * 50)
        for i in range(len(video_titles)):
            print(f"{i+1}. {video_titles[i]}")
        print(f"Best: {best_index+1}. {video_titles[best_index]}")
        print("Enter number (1-5) to select(or press Enter to use the best value), 'r' to refresh, 'q' to quit: ", end='', flush=True)

        while True:
            user_input = input().strip().lower()
            if user_input == 'q':
                print("User quit.")
                return None, None
            elif user_input == 'r':
                print("Refreshing...")
                if retry > 3:
                    print("Too many retries,return nothing.")
                else:
                    return self.interactive_search(retry=retry+1)
            elif user_input == '':
                print(f'Use best values:{best_index+1}. {video_titles[best_index]}')
                return video_ids[best_index],video_titles[best_index]
            elif user_input.isdigit():
                choice = int(user_input)
                if 1 <= choice <= len(video_titles):
                    final_choice = choice - 1
                    print(f"Selected: {choice}. {video_titles[final_choice]}")
                    return video_ids[final_choice], video_titles[final_choice]
                else:
                    print(f"Invalid number, please choose 1-{len(video_titles)}.")
            else:
                print("Invalid input. Enter number, 'r' or 'q'.")
