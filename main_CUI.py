import inspect
import os
import queue
import subprocess
import threading
import random
import sys
from pathlib import Path

import song_search
import download_audio
import select_file
import transform
from VoiceRecognition import VoiceRecognition
import initial_setup
import lang
from cache_manager import CacheManager
from player import MpvPlayer
from utils import enable_ansi_support, setup_mpv_path

setup_mpv_path()


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

        # Setting language
        self._ = lambda key: lang.language[self.lang_code].get(key, key)

        # Player (auto_play_next callback)
        self.player = MpvPlayer(auto_play_next_callback=self._auto_play_next)

        # Serial port object, used for sending status
        self._microbit_ser = None

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
        searcher = song_search.SongSearch(prompt=song_name, timeout=0)
        best_bvid, best_title, full_list = searcher.search()
        self.current_candidates = full_list
        self.current_index = 0
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
        self.current_index = (self.current_index + 1) % len(self.current_candidates)
        next_item = self.current_candidates[self.current_index]
        print(self._('switching_to_version').format(next_item['title']))
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

        try:
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
            if self.voice_controller:
                self.voice_controller.stop_monitor()
            if self.voice_thread:
                self.voice_thread.join(timeout=2)
            print(self._('voice_exited'))

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
        self.mic_idx = self.initial_setup.config.get('audio', 'input_device_index', fallback=None)
        if self.mic_idx is None:
            need_restart = True
        else:
            mic_idx = int(self.mic_idx)
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

    def _send_microbit_status(self, status):
        """Send play status (play/pause/stop) to micro:bit"""
        if self._microbit_ser is None:
            return
        try:
            self._microbit_ser.write(f"STATUS:{status}\n".encode())
        except Exception:
            pass  # Sending failed doesn't affect the main program

    def _init_microbit_listener(self):
        """Start the micro:bit listening thread (close existing connections first if there are any)"""
        # 1. Try to clear old connections
        if self._microbit_ser is not None:
            try:
                self._microbit_ser.close()
            except Exception:
                pass
            self._microbit_ser = None

        # 2. Check if the port is configured
        if not self.microbit_port:
            return

        # 3. Check if module "pyserial" is installed
        try:
            import serial
        except ImportError:
            print(self._('pyserial_not_installed'))
            return

        # 4. Start the listening thread
        def listener_loop():
            try:
                self._microbit_ser = serial.Serial(self.microbit_port, 115200, timeout=1)
                print(self._('microbit_connected').format(self.microbit_port))
                self._send_microbit_status("stop")
                while True:
                    line = self._microbit_ser.readline().decode('utf-8').strip()
                    if line == "PAUSE":
                        self.cmd_pause([])
                    elif line == "NEXT":
                        self._auto_play_next()
                    elif line == "STOP":
                        self.player.stop()
                        self._send_microbit_status("stop")
            except Exception as e:
                # When the connection is lost or an error occurs, clean up the serial port object so it can be reconnected later.
                print(self._('microbit_error').format(repr(e)))
                if self._microbit_ser:
                    try:
                        self._microbit_ser.close()
                    except:
                        pass
                    self._microbit_ser = None

        thread = threading.Thread(target=listener_loop, daemon=True)
        thread.start()

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
        self.downloader = download_audio.DownloadAudio(threads=self.threads, temp_dir=self.temp_dir, m4s_temp=self.m4s_dir)
        self.microbit_port = self.initial_setup.config.get('microbit', 'port', fallback='')
        if self.microbit_port:
            self._init_microbit_listener()

    def check_first_run(self):
        """Run initial setup if this is the first launch."""
        if self.initial_setup.config_path.exists():
            self.initial_setup.config.read(self.initial_setup.config_path, encoding='utf-8')
        was_first_run = self.initial_setup.config.get('first_run', 'first_run', fallback='0') == '0'
        self.initial_setup.initial_setup()
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

    def main_cui(self):
        """Main event loop for command-line interaction."""
        self.cmd_register()
        enable_ansi_support()
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
            os.system('chcp 65001 > nul')
        self.welcome()
        self.check_first_run()
        self.load_config()
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
                        if cmd_name in ('exit', 'voice'):
                            self.commands[cmd_name](args)
                        else:
                            self.commands[cmd_name](args)
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
