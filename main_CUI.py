import inspect
import json
import os
import queue
import subprocess
import threading
import random
import sys
import time
import urllib3
from pathlib import Path

import requests

import config
import feedback as fb
import song_search
import download_audio
import select_file
import transform
from VoiceRecognition import VoiceRecognition
import initial_setup
import lang
from cache_manager import CacheManager
from player import MpvPlayer
import progress_bar
from utils import enable_ansi_support, setup_mpv_path

setup_mpv_path()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class BiliLogin:
    """
    B 站扫码登录（二维码方案，不需要浏览器）。

    登录后才拿得到 320kbps / Hi-Res 音质；不登录也能用，只是码率低一些。
    原 get_cookies.py 已合并到这里，凭据保存在 bilibili_cookies.json。
    """

    QRCODE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
    HOME_URL = "https://www.bilibili.com/"

    def __init__(self, cookie_path="bilibili_cookies.json", qrcode_path="qrcode.png",
                 timeout=10):
        self.cookie_path = Path(cookie_path)
        self.qrcode_path = Path(qrcode_path)
        self.timeout = timeout
        self.session = requests.Session()
        headers = dict(config.headers)
        headers.update({
            'Accept-Encoding': 'gzip, deflate',
            'Referer': 'https://passport.bilibili.com/login',
            'Origin': 'https://passport.bilibili.com',
        })
        self.session.headers.update(headers)
        self.username = None

    # ---------- 二维码 ----------

    def _new_qrcode(self):
        """申请一张新二维码，保存图片并返回 qrcode_key；失败返回 None。"""
        try:
            self.session.get(self.HOME_URL, timeout=self.timeout)   # 先拿 buvid3
            payload = self.session.get(self.QRCODE_URL, timeout=self.timeout).json()
            data = payload.get("data") or {}
            key, url = data.get("qrcode_key"), data.get("url")
            if not key or not url:
                return None
        except Exception as exc:
            print(f"申请二维码失败：{exc}")
            return None

        try:
            import qrcode
            qrcode.make(url).save(self.qrcode_path)
        except Exception as exc:
            print(f"二维码图片保存失败（可忽略）：{exc}")

        return key

    @staticmethod
    def _open_image(path: Path):
        """尽量用系统默认程序把二维码显示出来。"""
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            pass

    def _poll(self, qrcode_key, max_wait=180):
        """轮询扫码状态，返回 'success' / 'expired' / 'timeout'。"""
        waited = 0
        while waited < max_wait:
            try:
                payload = self.session.get(
                    self.POLL_URL,
                    params={"qrcode_key": qrcode_key},
                    timeout=self.timeout,
                ).json()
                code = (payload.get("data") or {}).get("code")
            except Exception:
                time.sleep(2)
                waited += 2
                continue

            if code == 0:
                return 'success'
            if code == 86038:
                return 'expired'
            if code == 86090:
                print("已扫描，请在手机上确认登录...")
            elif code == 86101:
                print("等待扫码...", end="\r")
            else:
                print(f"未知状态：{code}")

            time.sleep(2)
            waited += 2
        return 'timeout'

    def _save_cookies(self):
        """把 Session 里的 Cookie 存成列表格式（与其它模块兼容）。"""
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self.session.cookies
        ]
        with open(self.cookie_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        return len(cookies)

    # ---------- 对外方法 ----------

    def login(self, max_wait=180, open_image=True):
        """走完一次扫码登录流程，成功返回 True。"""
        try:
            import qrcode  # noqa: F401
        except ImportError:
            print("缺少 qrcode 库，无法生成二维码。请执行：pip install qrcode")
            return False

        while True:
            key = self._new_qrcode()
            if key is None:
                return False

            print("=" * 56)
            print(f"二维码已保存：{self.qrcode_path}")
            print("请用手机 B 站 App 扫码，并在手机上确认登录")
            print("=" * 56)
            if open_image and self.qrcode_path.exists():
                self._open_image(self.qrcode_path)

            result = self._poll(key, max_wait=max_wait)
            if result == 'success':
                self._save_cookies()
                self.username = self.current_user()
                print(f"登录成功，Cookie 已保存到 {self.cookie_path}")
                return True
            if result == 'expired':
                print("二维码已失效，正在重新生成...")
                continue
            print("扫码超时。")
            return False

    def current_user(self):
        """用本地 Cookie 查当前登录用户名，未登录返回 None。"""
        cookies = config.load_cookie_dict(self.cookie_path)
        if not cookies:
            return None
        try:
            headers = dict(config.headers)
            headers["Cookie"] = "; ".join(f"{n}={v}" for n, v in cookies.items())
            payload = requests.get(self.NAV_URL, headers=headers, timeout=self.timeout).json()
            if payload.get("code") == 0 and payload.get("data", {}).get("isLogin"):
                return payload["data"].get("uname")
        except Exception:
            pass
        return None


class MainCui:
    """Main command-line interface controller for BiliMusicCLI."""

    MAX_CACHE_ENTRIES = 100

    def __init__(self):
        self.video_prefix = "https://www.bilibili.com/video/"
        self.video_pattern = r'^https?://(?:www\.)?bilibili\.com/video/(BV[a-zA-Z0-9]+)'
        self.config_path = Path("config.ini")

        self.initial_setup = initial_setup.InitialSetup()
        self.transform = transform.Transform(ffmpeg='./ffmpeg.exe')
        self.downloader = None

        self.commands = {}
        self.command_brief = {}
        self.command_usage = {}

        # Voice recognition related
        self.voice_controller = None
        self.voice_thread = None
        self.song_queue = None
        self.stop_voice_flag = False
        self._processing_song = False

        # Version switching
        self.current_candidates = []
        self.current_index = -1
        self.current_query = ""

        # 搜索来源（只用于反馈埋点区分：cli / voice / batch）
        self.search_source = "cli"

        # Configuration parameters
        self.threads = None
        self.temp_dir = None
        self.m4s_dir = None
        self.mp3_dir = None
        self.log_dir = None
        self.timeout = None
        self.lang_code = 'zh'
        self.mic_index = -1
        self.microbit_port = None

        # Cache database
        self.cache = CacheManager()

        # 扫码登录（原 get_cookies.py 的功能，已合并进本文件）
        self.login_helper = BiliLogin()

        # 语言在 load_config() 里按 config.ini 重新设置
        self._ = lambda key: lang.translate(self.lang_code, key)

        # Player (auto_play_next callback)
        self.player = MpvPlayer(auto_play_next_callback=self._auto_play_next)

        # micro:bit 遥控桥（microbit_bridge.MicrobitBridge）
        self._microbit = None
        # 当前播放状态：play / pause / stop，micro:bit 会来查
        self._playback_status = "stop"

        # 播放进度条（命令模式默认关，语音模式开，连着板子时交给点阵屏）
        self.progress_bar = None

    # ---------- Database operations (delegated to cache manager) ----------
    def _get_cached_song(self, bvid):
        """Retrieve cached song info by bvid."""
        return self.cache.get(bvid)

    def _insert_song_cache(self, bvid, title, file_path):
        """Insert a new song into the cache."""
        self.cache.insert(bvid, title, file_path)

    def _update_play_stats(self, bvid):
        """Update play statistics for a cached song."""
        self.cache.update_stats(bvid)

    def _cleanup_cache(self, max_entries=None):
        """Remove oldest entries to keep cache size under limit."""
        self.cache.cleanup(max_entries)

    def _get_all_cached_files(self):
        """Get all cached file paths."""
        return self.cache.get_all_files()

    # ---------- Player callbacks ----------
    def _auto_play_next(self):
        """Pick a random cached song (excluding current) and play it."""
        cached = self.cache.get_all_files()
        if not cached:
            print(self._('no_cached_songs'))
            return
        candidates = [f for f in cached if f != self.player.current_file]
        if not candidates:
            candidates = cached   # Only one song, repeat itself
        next_file = random.choice(candidates)
        print(self._('auto_playing_next').format(next_file.name))
        self.player.play(next_file)
        self._send_microbit_status("play")

    # ---------- Voice recognition callbacks ----------
    def on_voice_detected(self, song_name):
        """Beep and put the recognized song name into queue."""
        print("\a", end='', flush=True)
        if self.song_queue is not None and not self._processing_song:
            self.song_queue.put(song_name)

    def on_voice_stop(self):
        """Stop current playback via voice command."""
        self.player.stop()
        self._send_microbit_status("stop")

    def on_voice_pause(self):
        """Pause current playback via voice command."""
        self.player.pause()
        self._send_microbit_status("pause")

    def on_voice_resume(self):
        """Resume current playback via voice command."""
        self.player.resume()
        self._send_microbit_status("play")

    def on_voice_next(self):
        """Skip to next song via voice command."""
        print(self._('switching_to_next_song'))
        self._auto_play_next()

    # ---------- Song processing ----------
    def _perform_search(self, song_name):
        """
        Search Bilibili for the given song name.

        Returns:
            tuple: (best_bvid, best_title, full_list)
        """
        searcher = song_search.SongSearch(prompt=song_name, timeout=0,
                                          source=self.search_source)
        best_bvid, best_title, full_list = searcher.search()
        self.current_candidates = full_list
        self.current_index = 0
        self.current_query = song_name
        # 语音 / 批量模式没办法当场问用户「是不是这首」，所以这里记一条「默认认可」；
        # 真不满意的话，用户会喊「换个版本」，那条记录会把这条抵消掉（-1/+1 配平）。
        if best_bvid:
            fb.record(song_name, "auto", candidates=full_list, picked_index=0,
                      chosen=full_list[0] if full_list else None,
                      source=self.search_source)
        return best_bvid, best_title, full_list

    def _play_by_bvid(self, bvid, title):
        """
        Download or play from cache by bvid and title.

        If cached file exists, play it directly; otherwise download then play.
        """
        cached = self.cache.get(bvid)
        if cached:
            file_path = Path(cached['file_path'])
            if file_path.exists():
                print(self._('cache_hit').format(cached['title']))
                self.player.play(file_path)
                self._send_microbit_status("play")
                self.cache.update_stats(bvid)
                return
            else:
                print(self._('cache_file_missing').format(file_path))
                self.cache.delete_by_bvid(bvid)
        # First download or cache invalid
        video_url = self.video_prefix + bvid
        print(self._('downloading').format(title, video_url))
        paths = self.cmd_download([video_url])
        if not paths:
            return
        m4s_path = paths[0]
        if not m4s_path.exists():
            print(self._('downloaded_file_not_found').format(m4s_path))
            return
        self.cache.insert(bvid, title, m4s_path.resolve())
        self.player.play(m4s_path)
        self._send_microbit_status("play")

    def _process_song(self, song_name):
        """
        Main workflow: search -> download/play -> cache.

        This is called when a song name is recognized from voice input.
        """
        self._processing_song = True
        try:
            best_bvid, best_title, _ = self._perform_search(song_name)
            if not best_bvid:
                print(self._('no_matching_song'))
                return
            self._play_by_bvid(best_bvid, best_title)
        except Exception as e:
            print(self._('error_processing_song').format(e))
            import traceback
            traceback.print_exc()
        finally:
            self._processing_song = False

    def _switch_version(self):
        """Switch to the next search result version."""
        if not self.current_candidates or len(self.current_candidates) <= 1:
            print(self._('no_other_versions'))
            return
        rejected_index = self.current_index
        rejected = self.current_candidates[rejected_index]
        self.current_index = (self.current_index + 1) % len(self.current_candidates)
        next_item = self.current_candidates[self.current_index]
        print(self._('switching_to_version').format(next_item['title']))
        # 用户嫌这个版本不对 —— 最强的负样本信号，一定要记下来
        fb.record(self.current_query or "", "switch",
                  candidates=self.current_candidates,
                  picked_index=rejected_index, chosen=rejected,
                  source="voice")
        self.player.stop()
        self._play_by_bvid(next_item['bvid'], next_item['title'])

    # ---------- Command: voice ----------
    def cmd_voice(self, args):
        """Start continuous voice recognition for song requests."""
        if self.voice_thread and self.voice_thread.is_alive():
            print(self._('voice_already_running'))
            return

        self.song_queue = queue.Queue()
        self.stop_voice_flag = False
        self.search_source = "voice"      # 埋点：区分语音点歌和手动搜索

        print(self._('loading_voice_model'))

        def run_voice():
            controller = VoiceRecognition(
                wake_words=["小爱同学", "播放"],
                silence_duration=1.5,
                callback=self.on_voice_detected,
                callback_stop=self.on_voice_stop,
                callback_pause=self.on_voice_pause,
                callback_resume=self.on_voice_resume,
                callback_next=self.on_voice_next,
                on_ready=lambda: print(self._('voice_ready')),
                input_device_index=self.mic_index)
            self.voice_controller = controller
            try:
                controller.start_monitor()
            except Exception as e:
                print(self._('voice_recognition_thread_error').format(e))

        self.voice_thread = threading.Thread(target=run_voice, daemon=True)
        self.voice_thread.start()

        # 语音模式不用键盘输入，进度条顶不到人，可以放心开
        bar_on = False
        try:
            reason = self._progress_bar_blocked()
            if reason is None:
                bar_on = self._start_progress_bar()
            else:
                print(self._(reason))

            while not self.stop_voice_flag:
                try:
                    song = self.song_queue.get(timeout=0.5)
                    if song in ("换个版本", "换版本", "切换版本"):
                        self._switch_version()
                    else:
                        self._process_song(song)
                except queue.Empty:
                    continue
        except KeyboardInterrupt:
            print(self._('voice_recognition_interrupted'))
        finally:
            self.stop_voice_flag = True
            if bar_on:
                self._stop_progress_bar()
            if self.voice_controller:
                self.voice_controller.stop_monitor()
            if self.voice_thread:
                self.voice_thread.join(timeout=2)
            print(self._('voice_exited'))
            self.search_source = "cli"

    # ---------- Other commands ----------
    def cmd_cache(self, args):
        """Manage song cache: list or clean by bvid."""
        if not args:
            print(self._('cache_usage'))
            return
        subcmd = args[0].lower()
        if subcmd == "list":
            rows = []
            import sqlite3
            with sqlite3.connect(self.cache.db_path) as conn:
                cursor = conn.execute("SELECT bvid, title, file_path FROM song_cache ORDER BY last_play_time DESC")
                rows = cursor.fetchall()
            if not rows:
                print(self._('cache_empty'))
                return
            print(self._('cached_songs'))
            for bvid, title, file_path in rows:
                print(f"{bvid} - {title}")
                print(self._('file_label').format(file_path))
        elif subcmd == "clean":
            if len(args) == 1:
                confirm = input(self._('delete_all_cache')).strip().lower()
                if confirm != 'y':
                    print(self._('cancelled'))
                    return
                self.cache.delete_all()
                print(self._('all_cache_cleared'))
            else:
                bvid = args[1]
                if self.cache.delete_by_bvid(bvid):
                    print(self._('cache_cleared_for').format(bvid))
                else:
                    print(self._('cache_not_found').format(bvid))
        else:
            print(self._('invalid_subcommand'))

    def cmd_exit(self, args):
        """Exit the program."""
        self._stop_progress_bar()      # 别让刷新线程活着去写已经关掉的终端
        self.player.terminate()
        sys.exit(0)

    def cmd_help(self, args):
        """Show usage of built-in commands."""
        if not args:
            print(self._('available_commands'))
            for cmd in sorted(self.commands.keys()):
                brief = self.command_brief.get(cmd, "")
                print(f"  {cmd:<12} - {brief}")
            print(self._('type_help'))
        else:
            cmd_name = args[0]
            if cmd_name in self.command_usage:
                print(inspect.cleandoc(self.command_usage[cmd_name]))
            elif cmd_name in self.commands:
                print(self._('no_detailed_help').format(cmd_name))
            else:
                print(self._('unknown_command').format(cmd_name))

    def cmd_transform(self, args):
        """Convert a single audio/video file via GUI selection."""
        video_suffix = ['.mp4', '.mkv', '.avi']
        audio_suffix = ['.mp3', '.wav', '.flac', '.m4a', 'm4s']
        try:
            file_path = select_file.select(title=self._('select_file_title'),
                                           filetypes=[(self._('video_file_label'), video_suffix),
                                                      (self._('audio_file_label'), audio_suffix)])
            if not file_path:
                print(self._('user_cancelled'))
                return
            self.transform.convert(input_path=file_path, output_folder="./")
        except subprocess.CalledProcessError as e:
            print(self._('conversion_failed').format(e.returncode))
            if e.stderr:
                print(e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr)
        except Exception as e:
            print(e)

    def cmd_batch_transform(self, args):
        """
        Batch convert all supported audio/video files in a folder to MP3.
        Output directory is configured mp3 folder (./mp3).
        If no folder is given, a folder selection dialog will appear.
        """
        if args:
            folder = Path(args[0])
            if not folder.exists() or not folder.is_dir():
                print(self._('invalid_folder').format(folder))
                return
        else:
            print(self._('select_folder_title'))
            folder_path = select_file.select(title=self._('select_folder_title'), mode='folder')
            if not folder_path:
                print(self._('user_cancelled'))
                return
            folder = Path(folder_path)
            print(self._('saving_to_directory').format(folder))

        extensions = ['.mp4', '.mkv', '.avi', '.m4s', '.m4a', '.flac', '.wav']
        all_files = [f for f in folder.glob('*') if f.suffix.lower() in extensions]
        if not all_files:
            print(self._('no_files_found').format(folder))
            return

        self.mp3_dir.mkdir(parents=True, exist_ok=True)
        print(self._('saving_to_directory').format(self.mp3_dir))

        to_convert = []
        for file in all_files:
            output_path = self.mp3_dir / (file.stem + ".mp3")
            if output_path.exists():
                print(self._('skip_existing_mp3').format(output_path.name))
            else:
                to_convert.append(file)

        if not to_convert:
            print(self._('no_files_to_convert'))
            return

        print(self._('converting').format(len(to_convert), len(to_convert)))  # placeholder, will be overwritten by loop
        success = 0
        for idx, file in enumerate(to_convert, 1):
            print(f"\n[{idx}/{len(to_convert)}] {file.name}")
            try:
                self.transform.convert(str(file), str(self.mp3_dir), ".mp3")
                success += 1
            except Exception as e:
                print(self._('batch_failed').format(repr(e)))
        print(self._('batch_done').format(success, len(to_convert)))

    def cmd_download(self, args):
        """
        Download one or multiple Bilibili audio streams by URL.
        Returns a list of downloaded file paths (empty on failure).
        """
        if not args:
            print(self._('usage_download'))
            return []

        valid_urls = []
        invalid_urls = []
        for idx, url in enumerate(args, start=1):
            if 'bilibili.com/video/' in url and 'BV' in url:
                valid_urls.append(url)
            else:
                invalid_urls.append((idx, url))

        if invalid_urls:
            print(self._('warning_invalid_urls'))
            for idx, url in invalid_urls:
                print(f"   #{idx}: {url}")
            print()

        if not valid_urls:
            print(self._('no_valid_urls'))
            return []

        downloaded_paths = []
        for idx, url in enumerate(valid_urls, start=1):
            try:
                path = self.downloader.download_audio(url)
                downloaded_paths.append(path)
                print(self._('download_success').format(path))
            except Exception as e:
                print(self._('download_failed').format(e))
        return downloaded_paths

    def cmd_search(self, args):
        """Manually search for a song and play it interactively."""
        if not args:
            print(self._('search_usage'))
            return
        query = ' '.join(args)
        print(self._('searching_for').format(query))
        searcher = song_search.SongSearch(prompt=query, timeout=0)
        bvid, title = searcher.interactive_search()
        if bvid is None:
            print(self._('user_cancelled'))
            return
        confirm = input(self._('prompt_play_now')).strip().lower()
        if confirm == 'y':
            self._play_by_bvid(bvid, title)
        else:
            print(self._('cancelled'))

    def cmd_feedback(self, args):
        """Show (or clear) the search feedback collected while using the app."""
        if args and args[0].lower() == "clear":
            try:
                import sqlite3

                with sqlite3.connect(fb.DB_PATH) as conn:
                    conn.execute("DELETE FROM search_events")
                fb.reset()                      # 让它下次重新读配置
                print(self._('feedback_cleared'))
            except Exception as exc:
                print(self._('feedback_clear_failed').format(exc))
            return

        if not fb.config_enabled():
            print(self._('feedback_disabled'))
            return
        fb.store().summarize(lang_code=self.lang_code)
        print()
        print(self._('feedback_hint'))

    def cmd_reconnect(self, args):
        """Manually reconnect micro:bit"""
        if not self.microbit_port:
            print(self._('microbit_port_not_configured'))
            return
        print(self._('microbit_reconnecting'))
        self._init_microbit_listener()

    def cmd_batch_search(self, args):
        """
        Batch download .m4s files from a text file without conversion.

        Reads a text file (one entry per line, support song names or Bilibili URLs),
        downloads each as .m4s and stores them in m4s_temp/ directory.
        No MP3 conversion is performed, and original .m4s files are kept.
        """
        try:
            txt_path = select_file.select(title="Select txt file to batch search:",
                                          filetypes=[("txt files", "*.txt")],
                                          mode='file')
            if not txt_path:
                print(self._('user_cancelled'))
                return
            with open(txt_path, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            if not lines:
                print(self._('no_files_to_convert'))
                return
            print(self._('batch_search_start').format(len(lines)))
            self.search_source = "batch"   # 埋点：批量模式的自动选择单独归类
            try:
                for idx, line in enumerate(lines, 1):
                    print(self._('processing_item').format(idx, len(lines), line))
                    if 'bilibili.com/video/' in line and 'BV' in line:
                        print(self._('detected_url_downloading'))
                        self.cmd_download([line])
                        print(self._('download_finished'))
                        continue
                    while True:
                        choice = input(self._('prompt_auto_mode')).strip().lower()
                        if choice == 'y':
                            self._process_song(line)
                            break
                        elif choice == 'n':
                            parts = line.split()
                            self.cmd_search(parts)
                            break
                        elif choice == 's':
                            print(self._('cancelled'))
                            break
                        else:
                            print(self._('invalid_input_yns'))
            finally:
                self.search_source = "cli"
        except Exception as e:
            print(self._('unexpected_error').format(e))

    def _download_single(self, entry):
        """
        Download a single entry (song name or URL), return the .m4s path.

        If entry is a URL, download directly. Otherwise search and download best result.
        """
        if 'bilibili.com/video/' in entry and 'BV' in entry:
            # 直接下载链接
            return self.downloader.download_audio(entry)
        else:
            # 搜索歌名，获取最佳结果
            searcher = song_search.SongSearch(prompt=entry, timeout=0)
            bvid, title, _ = searcher.search()
            if not bvid:
                raise Exception(self._('no_matching_song'))
            video_url = self.video_prefix + bvid
            return self.downloader.download_audio(video_url)

    def cmd_batch_extract(self, args):
        """
        Batch download and convert songs to MP3 with auto-cleanup.

        Reads a text file (one entry per line, support song names or Bilibili URLs),
        downloads each as .m4s, converts to MP3 (saved to mp3/ directory),
        then deletes the original .m4s file.
        """
        if not args:
            txt_path = select_file.select(title="Select text file with song name",
                                          filetypes=[("txt files", "*.txt")],
                                          mode='file')
            if not txt_path:
                print(self._('user_cancelled'))
                return
        else:
            txt_path = Path(args[0])
            if not txt_path.exists():
                print(self._('cache_not_found').format(txt_path))
                return

        with open(txt_path, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        if not lines:
            print(self._('no_files_to_convert'))
            return

        print(self._('processing_entries').format(len(lines)))
        success = 0
        for idx, entry in enumerate(lines, 1):
            print(f"\n[{idx}/{len(lines)}] {entry}")
            try:
                m4s_path = self._download_single(entry)
                if not m4s_path or not m4s_path.exists():
                    print(self._('skipping_conversion'))
                    continue
                output_mp3 = self.mp3_dir / (m4s_path.stem + ".mp3")
                if output_mp3.exists():
                    m4s_path.unlink(missing_ok=True)
                    print(self._('already_mp3_skip'))
                else:
                    self.transform.convert(str(m4s_path), str(self.mp3_dir), ".mp3")
                    m4s_path.unlink(missing_ok=True)
                    print(self._('converted_to_mp3'))
                success += 1
            except Exception as e:
                print(self._('batch_extract_failed').format(type(e).__name__,repr(e)))
        print(self._('batch_done').format(success, len(lines)))

    # ---------- Command: login ----------
    def cmd_login(self, args):
        """
        扫码登录 B 站；`login status` 查看当前登录状态。
        登录后才能拿到 320kbps / Hi-Res 音质。
        """
        sub = args[0].lower() if args else ""

        if sub == "status":
            user = self.login_helper.current_user()
            if user:
                print(self._('login_status_ok').format(user))
            else:
                print(self._('login_status_none'))
            return

        print(self._('login_tip'))
        ok = self.login_helper.login(max_wait=180)
        if not ok:
            print(self._('login_failed'))
            return

        # Cookie 变了，重建下载器让新 Cookie 立刻生效
        self.downloader = download_audio.DownloadAudio(
            threads=self.threads or 4,
            temp_dir=self.temp_dir or Path("./temp"),
            m4s_temp=self.m4s_dir or Path("./m4s_temp"),
            timeout=self.timeout or 10,
        )
        print(self._('login_success').format(self.login_helper.username or ''))

    def cmd_pause(self, args):
        """Toggle pause/resume of current playback."""
        if self.player and self.player.player:
            if self.player.player.pause:
                self.player.resume()
                self._send_microbit_status("play")
            else:
                self.player.pause()
                self._send_microbit_status("pause")

    # ---------- Initialization & config ----------
    def _verify_mic_device(self):
        """Check if the configured microphone device is working, if not, reconfigure it and restart."""
        mic_cfg = self.initial_setup.config.get('audio', 'input_device_index', fallback=None)
        if mic_cfg is None:
            need_restart = True
        else:
            mic_idx = int(mic_cfg)
            import pyaudio
            p = pyaudio.PyAudio()
            valid = False
            if mic_idx == -1:
                valid = True
            else:
                try:
                    info = p.get_device_info_by_index(mic_idx)
                    if info['maxInputChannels'] > 0:
                        valid = True
                except:
                    pass
            p.terminate()
            need_restart = not valid
            if valid:
                # 之前这里写成了 self.mic_idx，导致语音线程始终用默认设备
                self.mic_index = mic_idx

        if need_restart:
            print(self._('mic_invalid_reconfig'))
            self.initial_setup.configure_microphone()
            # Save the configuration file again
            with open(self.initial_setup.config_path, 'w', encoding='utf-8') as f:
                self.initial_setup.config.write(f)
            print(self._('initialization_done_restart'))
            sys.stdout.flush()
            sys.stderr.flush()
            self.restart_program()

    # ---------- micro:bit 遥控 ----------

    def _microbit_state(self) -> dict:
        """给 micro:bit 用的当前状态快照。"""
        state = {"status": self._playback_status}
        if self.player:
            state["volume"] = self.player.get_volume()
            position = self.player.get_time_pos()
            duration = self.player.get_duration()
            state["time"] = (position, duration)
            if self.player.current_file:
                state["title"] = Path(self.player.current_file).stem
        return state

    def _send_microbit_status(self, status):
        """记录并推送播放状态给 micro:bit（未连接时静默跳过）。"""
        self._playback_status = status
        if self._microbit is None:
            return
        try:
            self._microbit.send_status(status)
            if status == "play":
                # 切歌后顺带把歌名和进度推一次，板端屏幕能立刻跟上
                state = self._microbit_state()
                if state.get("title"):
                    self._microbit.send_title(state["title"])
        except Exception:
            pass

    def _adjust_volume(self, delta):
        """调整音量并同步给 micro:bit。"""
        if not self.player:
            return None
        value = self.player.adjust_volume(delta)
        print(self._('microbit_volume').format(value))
        if self._microbit is not None:
            self._microbit.send_volume(value)
        return value

    def _play_prev_track(self):
        """上一首：等同于随机换一首（当前还没有真正的播放队列）。"""
        self._auto_play_next()

    # ---------- 播放进度条 ----------

    def _progress_bar_blocked(self):
        """
        进度条为什么开不了。返回 None 就是能开。

        连着 micro:bit 时不刷命令行：那种情况下进度本来就会由 bridge 的 keepalive
        推到点阵屏上，两边同时刷只会互相干扰。
        """
        if self._microbit is not None:
            return 'progress_bar_microbit'
        return None

    def _start_progress_bar(self):
        """开进度条。已经在跑就什么都不做。"""
        if self.progress_bar is not None or self.player is None:
            return False
        self.progress_bar = progress_bar.ProgressBar(self.player)
        self.progress_bar.start()
        return True

    def _stop_progress_bar(self):
        """关进度条并擦掉屏幕上那一行。"""
        if self.progress_bar is None:
            return
        self.progress_bar.stop()
        self.progress_bar = None

    def cmd_progress(self, args):
        """
        手动开关进度条。

        命令模式默认关着 —— 后台刷新会把 input() 里正在敲的内容顶掉，
        跑 `python progress_bar_verify.py --demo` 能看到现场效果。
        语音模式没人敲键盘，那边会自动开。
        """
        if self.progress_bar is not None:
            self._stop_progress_bar()
            print(self._('progress_bar_off'))
            return

        reason = self._progress_bar_blocked()
        if reason and args and args[0] in ('force', '强制'):
            reason = None            # 明说了要开就开
        if reason:
            print(self._(reason))
            return

        if self._start_progress_bar():
            print(self._('progress_bar_on'))
        else:
            print(self._('progress_bar_unavailable'))

    def _init_microbit_listener(self):
        """启动 micro:bit 监听（先关掉旧连接）。"""
        if self._microbit is not None:
            self._microbit.stop()
            self._microbit = None

        if not self.microbit_port:
            return

        try:
            import microbit_bridge
        except ImportError:
            print(self._('pyserial_not_installed'))
            return

        self._microbit = microbit_bridge.MicrobitBridge(
            port=self.microbit_port,
            callbacks={
                "toggle_pause": lambda: self.cmd_pause([]),
                "next_track": self._auto_play_next,
                "prev_track": self._play_prev_track,
                "stop": lambda: (self.player.stop(), self._send_microbit_status("stop")),
                "adjust_volume": self._adjust_volume,
                "set_volume": self._adjust_volume_absolute,
                "get_state": self._microbit_state,
                "log": self._microbit_log,
            },
        )
        if not self._microbit.start():
            # start() 内部已打印失败原因；留着对象，reconnect 时可重试
            self._microbit = None
            return
        # 连接就绪后立刻把当前状态同步过去
        self._microbit.send_state()

    def _microbit_log(self, message):
        """把 bridge 的内部提示接回语言包，避免中英文混着打。"""
        if "connected on" in message:
            print(self._('microbit_connected').format(self.microbit_port))
        else:
            print(self._('microbit_error').format(message))

    def _adjust_volume_absolute(self, value):
        """micro:bit 直接给绝对值时的入口（adjust_volume 需要的是增量）。"""
        if not self.player:
            return None
        current = self.player.get_volume()
        return self._adjust_volume(int(value) - current)

    def register_command(self, command, function, brief, usage):
        """Register a command with its brief description and usage."""
        self.commands[command] = function
        self.command_brief[command] = brief
        self.command_usage[command] = usage

    def load_config(self):
        """Read config.ini and set parameters."""
        self.initial_setup.config.read('config.ini', encoding='utf-8')
        self.threads = self.initial_setup.config.getint('threads', 'threads', fallback=4)
        self.temp_dir = Path(self.initial_setup.config.get('paths', 'temporary_save_location', fallback="./temp"))
        self.m4s_dir = Path(self.initial_setup.config.get('paths', 'm4s_temp', fallback="./m4s_temp"))
        self.mp3_dir = Path(self.initial_setup.config.get('paths', 'mp3', fallback="./mp3"))
        self.log_dir = Path(self.initial_setup.config.get('paths', 'logs', fallback="./log"))
        self.timeout = self.initial_setup.config.getint('timeout', 'timeout', fallback=5)
        self.mic_index = self.initial_setup.config.getint('audio', 'input_device_index', fallback=-1)
        self.downloader = download_audio.DownloadAudio(
            threads=self.threads, temp_dir=self.temp_dir, m4s_temp=self.m4s_dir, timeout=self.timeout
        )
        self.microbit_port = self.initial_setup.config.get('microbit', 'port', fallback='')
        # 语言：兼容 "English" / "中文" 等历史写法
        self.lang_code = lang.normalize(
            self.initial_setup.config.get('language', 'language', fallback='zh'), 'zh'
        )
        self._ = lambda key: lang.translate(self.lang_code, key)

        if not config.load_cookie_dict():
            print(self._('login_not_logged_in'))

        if self.microbit_port:
            self._init_microbit_listener()

    def check_first_run(self):
        """Run initial setup if this is the first launch."""
        if self.initial_setup.config_path.exists():
            self.initial_setup.config.read(self.initial_setup.config_path, encoding='utf-8')
        was_first_run = self.initial_setup.config.get('first_run', 'first_run', fallback='0') == '0'
        self.initial_setup.initial_setup()

        # 初始化向导里若选择了「现在登录」，这里紧接着走扫码流程
        if getattr(self.initial_setup, 'pending_login', False):
            print(self._('login_tip'))
            self.login_helper.login(max_wait=180)

        if was_first_run:
            print(self._('initialization_done_restart'))
            sys.stdout.flush()
            sys.stderr.flush()
            self.restart_program()

    @staticmethod
    def restart_program():
        """Restart the current program."""
        python = sys.executable
        os.chdir(Path(__file__).parent)
        os.execl(python, python, *sys.argv)

    @staticmethod
    def clear_screen():
        """Clear the terminal screen."""
        os.system('cls' if os.name == 'nt' else 'clear')

    def welcome(self):
        """Print the welcome banner."""
        width = 60
        line = '-' * width
        print(line)
        print(self._('welcome_title').center(width))
        print(self._('welcome_subtitle').center(width))
        print(self._('welcome_author').center(width))
        print(line)
        print("  " + self._('features_title'))
        for feat in self._('features'):
            print(f"    {feat}")
        print(line)
        print("  " + self._('getting_started'))
        print(line)
        print()

    def cmd_register(self):
        """Register all built-in commands with their brief and usage."""
        self.register_command('exit', self.cmd_exit,
                              brief=self._('cmd_exit_brief'), usage=self._('cmd_exit_usage'))
        self.register_command('transform', self.cmd_transform,
                              brief=self._('cmd_transform_brief'), usage=self._('cmd_transform_usage'))
        self.register_command('help', self.cmd_help,
                              brief=self._('cmd_help_brief'), usage=self._('cmd_help_usage'))
        self.register_command('download', self.cmd_download,
                              brief=self._('cmd_download_brief'), usage=self._('cmd_download_usage'))
        self.register_command('voice', self.cmd_voice,
                              brief=self._('cmd_voice_brief'), usage=self._('cmd_voice_usage'))
        self.register_command('cache', self.cmd_cache,
                              brief=self._('cmd_cache_brief'), usage=self._('cmd_cache_usage'))
        self.register_command('search', self.cmd_search,
                              brief=self._('cmd_search_brief'), usage=self._('cmd_search_usage'))
        self.register_command('reconnect', self.cmd_reconnect,
                              brief=self._('cmd_reconnect_brief'), usage=self._('cmd_reconnect_usage'))
        self.register_command('batch_transform', self.cmd_batch_transform,
                              brief=self._('cmd_batch_transform_brief'), usage=self._('cmd_batch_transform_usage'))
        self.register_command('batch_search', self.cmd_batch_search,
                              brief=self._('cmd_batch_search_brief'), usage=self._('cmd_batch_search_usage'))
        self.register_command('batch_extract', self.cmd_batch_extract,
                              brief=self._('cmd_batch_extract_brief'), usage=self._('cmd_batch_extract_usage'))
        self.register_command('pause', self.cmd_pause,
                              brief=self._('cmd_pause_brief'), usage=self._('cmd_pause_usage'))
        self.register_command('login', self.cmd_login,
                              brief=self._('cmd_login_brief'), usage=self._('cmd_login_usage'))
        self.register_command('progress', self.cmd_progress,
                              brief=self._('cmd_progress_brief'), usage=self._('cmd_progress_usage'))
        self.register_command('feedback', self.cmd_feedback,
                              brief=self._('cmd_feedback_brief'), usage=self._('cmd_feedback_usage'))

    def main_cui(self):
        """Main event loop for command-line interaction."""
        enable_ansi_support()
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
            os.system('chcp 65001 > nul')
        self.check_first_run()
        self.load_config()      # 先载入配置（含语言）
        self.cmd_register()     # 语言确定后再注册，帮助文案才会跟着切换
        self.welcome()
        self._verify_mic_device()

        while True:
            try:
                self.clear_screen()
                cwd = os.getcwd()
                dir_name = os.path.basename(cwd) or os.path.splitdrive(cwd)[0] + os.sep
                prompt = f"{dir_name} $ "
                line = input(prompt).strip()
                if not line:
                    continue

                parts = line.split()
                valid_urls = [p for p in parts if 'bilibili.com/video/' in p and 'BV' in p]
                if valid_urls:
                    self.cmd_download(valid_urls)
                    print(self._('press_enter'))
                    input()
                else:
                    cmd_name = parts[0]
                    args = parts[1:]
                    if cmd_name in self.commands:
                        self.commands[cmd_name](args)
                        # exit / voice 会自己接管交互，不需要额外暂停
                        if cmd_name not in ('exit', 'voice'):
                            print(self._('press_enter'))
                            input()
                    else:
                        print(self._('unknown_command').format(cmd_name))
                        print(self._('press_enter'))
                        input()
            except KeyboardInterrupt:
                print()
                continue
            except EOFError:
                print()
                break
            except Exception as e:
                import traceback
                with open('error.log', 'a', encoding='utf-8') as f:
                    f.write(f"{type(e).__name__}: {e}\n")
                    traceback.print_exc(file=f)
                print(self._('unexpected_error').format(e))
                print(self._('press_enter'))
                input()


if __name__ == '__main__':
    cui = MainCui()
    cui.main_cui()
