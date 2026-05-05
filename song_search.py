import sys
import time
from urllib.parse import quote
import select
from bs4 import BeautifulSoup
import requests
import re
import config

class SongSearch:
    def __init__(self,prompt,timeout):
        self.keyword_url = 'https://search.bilibili.com/all?keyword='
        self.video_id_pattern = r'href="//www\.bilibili\.com/video/([^/]+)/'
        self.title_pattern = r'title="(.*?)">'
        self.blacklist_word = ["纯享","循环"]
        self.high_quality_keywords = ["Hi-Res无损", "录音棚", "官方", "母带", "24bit", "无损音质"]

        self.prompt = quote(prompt)
        self.timeout = timeout

    def _search(self):
        search_url = self.keyword_url + self.prompt
        response = requests.get(url = search_url,headers=config.headers)
        search_html = BeautifulSoup(response.text,'html.parser')
        search_list = str(search_html.find_all('div', class_='bili-video-card__info--right'))
        video_title = re.findall(self.title_pattern, search_list)
        video_id = re.findall(self.video_id_pattern, search_list)
        final_video_title = video_title[0:5]
        final_video_id = video_id[0:5]
        return final_video_title,final_video_id

    def _get_priority_score(self, title):
        score = 0
        for kw in self.high_quality_keywords:
            if kw in title:
                score += 10  # 每个关键词加10分
        # 黑名单词减分
        for bw in self.blacklist_word:
            if bw in title:
                score -= 5
        return score

    def _filter_video(self):
        """Get user input for song name and search for matching videos。"""
        print("Please wait a moment, searching...")
        video_title, video_id = self._search()
        # Find the best result (avoid "循环" and "纯享" versions)
        best_index = 0
        best_score = -1
        flag = True

        for i in range(5):
            current_title = video_title[i]
            print(str(i + 1) + "." + current_title)
            score = self._get_priority_score(current_title)
            if score > best_score:
                best_score = score
                best_index = i
        if best_score == -1:
            best_index = 0

        print(f"Please enter a number to make a selection. If no selection is made, the optimal result will be automatically chosen after {self.timeout} seconds...")
        print(f"The best result is: {best_index + 1}.{video_title[best_index]}")

        user_choice = -1
        start_time = time.time()

        sys.stdout.write("Please choose (1-5): ")
        sys.stdout.flush()

        # Wait for user input with certain timeout
        while time.time() - start_time < self.timeout:
            if sys.platform == "win32":
                # Windows platform input handling
                try:
                    import msvcrt
                    if msvcrt.kbhit():
                        user_input = input()
                        try:
                            user_choice = int(user_input)
                            if 1 <= user_choice <= 5:
                                break
                            else:
                                print("Invalid input, please enter a number between 1 and 5.")
                                sys.stdout.write("Please choose (1-5):")
                                sys.stdout.flush()
                        except ValueError:
                            print("The input is not a valid number, please enter it again.")
                            sys.stdout.write("Please choose (1-5): ")
                            sys.stdout.flush()
                except ImportError:
                    time.sleep(1)
            else:
                # Unix/Linux platform input handling
                if select.select([sys.stdin], [], [], 1)[0]:
                    user_input = sys.stdin.readline().strip()
                    try:
                        user_choice = int(user_input)
                        if 1 <= user_choice <= 5:
                            break
                        else:
                            print("Please enter a number between 1 and 5")
                            sys.stdout.write("Please choose (1-5): ")
                            sys.stdout.flush()
                    except ValueError:
                        print("The input is not a valid number, please enter it again.")
                        sys.stdout.write("Please choose (1-5): ")
                        sys.stdout.flush()

            # Update remaining time display
            remaining = self.timeout - int(time.time() - start_time)
            sys.stdout.write(f"\rRemaining time: {remaining} seconds | Please choose (1-5): ")
            sys.stdout.flush()

        print()

        # Determine final selection
        if 1 <= user_choice <= 5:
            final_choice = user_choice - 1
            print(f"已选择: {user_choice}.{video_title[final_choice]}")
        else:
            final_choice = best_index
            print(f"Timeout, automatically select the optimal result: {best_index + 1}.{video_title[best_index]}")

        return video_id[final_choice], video_title[final_choice]

    def search(self):
        video_titles, video_ids = self._search()
        if not video_ids:
            return None, None, []

        best_index = 0
        best_score = -1
        for i in range(len(video_titles)):
            score = self._get_priority_score(video_titles[i])
            if score > best_score:
                best_score = score
                best_index = i
        if best_score == -1:
            best_index = 0

        best_id = video_ids[best_index]
        best_title = video_titles[best_index]

        full_list = []
        for i in range(len(video_ids)):
            full_list.append({'bvid': video_ids[i], 'title': video_titles[i]})
        return best_id, best_title, full_list