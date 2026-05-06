import inspect
import os
import queue
import subprocess
import threading
import sqlite3
import random
import sys
from pathlib import Path

import readline

import song_search
import download_audio
import select_file
import transform
from VoiceRecognition import VoiceRecognition
import initial_setup
import lang

# ---------- mpv DLL path setting (must be set before importing mpv) ----------
def setup_mpv_path():
    base_dir = Path(__file__).parent
    dll_dir = base_dir / "bin"
    if dll_dir.exists():
        os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")
    else:
        if not (base_dir / "libmpv-2.dll").exists():
            print("Warning: libmpv-2.dll not found, please put it in bin/ folder.")

setup_mpv_path()
import mpv


class MainCui:
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

        # Player related
        self.player = None
        self.current_playing_file = None
        self.auto_play_next = True

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

        # Cache database
        self.db_path = Path("./song_cache.db")
        self._init_db()

        # Initialize mpv player
        self._init_player()

        #Setting language
        self._ = lambda key: lang.language[self.lang_code].get(key, key)

    # ---------- Database operations ----------
    def _init_db(self):
        """Initialize SQLite database and create table if not exists."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS song_cache (
                    bvid TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    download_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_play_time TIMESTAMP,
                    play_count INTEGER DEFAULT 0
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_last_play ON song_cache(last_play_time)')

    def _get_cached_song(self, bvid):
        """Query cache by BV id, return dict or None."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT bvid, title, file_path FROM song_cache WHERE bvid = ?",
                (bvid,)
            )
            row = cursor.fetchone()
            if row:
                return {"bvid": row[0], "title": row[1], "file_path": row[2]}
            return None

    def _insert_song_cache(self, bvid, title, file_path):
        """Insert or update cache entry and trigger cleanup."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO song_cache (bvid, title, file_path, download_time) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (bvid, title, str(file_path))
            )
        self._cleanup_cache()

    def _update_play_stats(self, bvid):
        """Update last play time and play count."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE song_cache SET last_play_time = CURRENT_TIMESTAMP, play_count = play_count + 1 WHERE bvid = ?",
                (bvid,)
            )

    def _cleanup_cache(self, max_entries=None):
        """Keep only the most recently played `max_entries` songs."""
        if max_entries is None:
            max_entries = self.MAX_CACHE_ENTRIES
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                DELETE FROM song_cache
                WHERE bvid NOT IN (
                    SELECT bvid FROM song_cache
                    ORDER BY last_play_time DESC
                    LIMIT ?
                )
            ''', (max_entries,))

    def _get_all_cached_files(self):
        """Retrieve all cached audio file paths."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT file_path FROM song_cache")
            rows = cursor.fetchall()
        return [Path(row[0]) for row in rows]

    # ---------- mpv player management ----------
    def _init_player(self):
        """Initialize mpv player with Windows audio output (wasapi)."""
        self.player = mpv.MPV(vo='null', vid=False, ao='wasapi')
        print('mpv player initialized successfully')

        @self.player.event_callback('end-file')
        def on_end_file(event):
            reason = event.reason if hasattr(event, 'reason') else event['reason']
            if reason == 0:
                print("\n[Playback finished]")
                if self.auto_play_next:
                    threading.Thread(target=self._auto_play_next, daemon=True).start()

    def _safe_stop(self):
        """Stop the current playback without blocking."""
        if self.player:
            try:
                self.player.stop()
            except:
                pass

    def _play_with_mpv(self, file_path, start_pos=0):
        """Play an audio file with mpv, optionally seek to start_pos."""
        if not Path(file_path).exists():
            return False
        self.player.stop()
        self.player.play(str(file_path))
        if start_pos > 0:
            self.player.seek(start_pos, reference="absolute")
        self.current_playing_file = file_path
        print(f"Now playing: {Path(file_path).name}")
        return True

    def _pause_playback(self):
        """Pause the current audio."""
        if self.player:
            self.player.pause = True
            print("Playback paused")

    def _resume_playback(self):
        """Resume paused audio."""
        if self.player:
            self.player.pause = False
            print("Playback resumed")

    def _stop_playback(self):
        """Stop the current audio."""
        self._safe_stop()
        print("Playback stopped")

    def _auto_play_next(self):
        """Pick a random cached song (excluding current) and play it."""
        cached = self._get_all_cached_files()
        if not cached:
            print("No cached songs, cannot auto-play next.")
            return
        candidates = [f for f in cached if f != self.current_playing_file]
        if not candidates:
            candidates = cached   # Only one song, repeat itself
        next_file = random.choice(candidates)
        print(f"Auto-playing next: {next_file.name}")
        self._play_with_mpv(next_file)

    # ---------- Voice recognition callbacks ----------
    def on_voice_detected(self, song_name):
        """Beep and put the recognized song name into queue."""
        print("\a", end='', flush=True)
        if self.song_queue is not None and not self._processing_song:
            self.song_queue.put(song_name)

    def on_voice_stop(self):
        self._stop_playback()

    def on_voice_pause(self):
        self._pause_playback()

    def on_voice_resume(self):
        self._resume_playback()

    def on_voice_next(self):
        print("Switching to next song manually.")
        self._auto_play_next()

    # ---------- Song processing ----------
    def _perform_search(self, song_name):
        """Search Bilibili for song name, return best bvid, title, and full list."""
        searcher = song_search.SongSearch(prompt=song_name, timeout=0)
        best_bvid, best_title, full_list = searcher.search()
        self.current_candidates = full_list
        self.current_index = 0
        return best_bvid, best_title

    def _play_by_bvid(self, bvid, title):
        """Download or play from cache by bvid and title."""
        cached = self._get_cached_song(bvid)
        if cached:
            file_path = Path(cached['file_path'])
            if file_path.exists():
                print(f"Cache hit: {cached['title']}, playing directly.")
                self._play_with_mpv(file_path)
                self._update_play_stats(bvid)
                return
            else:
                print(f"Cache file missing: {file_path}, will re-download.")
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("DELETE FROM song_cache WHERE bvid = ?", (bvid,))
        # First download or cache invalid
        video_url = self.video_prefix + bvid
        print(f"Downloading: {title} ({video_url})")
        paths = self.cmd_download([video_url])
        if not paths:
            return
        m4s_path = paths[0]
        if not m4s_path.exists():
            print(f"Downloaded file not found: {m4s_path}")
            return
        self._insert_song_cache(bvid, title, m4s_path.resolve())
        self._play_with_mpv(m4s_path)

    def _process_song(self, song_name):
        """Main workflow: search -> download/play -> cache."""
        self._processing_song = True
        try:
            best_bvid, best_title = self._perform_search(song_name)
            if not best_bvid:
                print("No matching song found.")
                return
            self._play_by_bvid(best_bvid, best_title)
        except Exception as e:
            print(f"Error processing song: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._processing_song = False

    def _switch_version(self):
        """Switch to the next search result version."""
        if not self.current_candidates or len(self.current_candidates) <= 1:
            print("No other versions available.")
            return
        self.current_index = (self.current_index + 1) % len(self.current_candidates)
        next_item = self.current_candidates[self.current_index]
        print(f"Switching to version: {next_item['title']}")
        self._stop_playback()
        self._play_by_bvid(next_item['bvid'], next_item['title'])

    # ---------- Command: voice ----------
    def cmd_voice(self, args):
        """Start continuous voice recognition for song requests."""
        if self.voice_thread and self.voice_thread.is_alive():
            print("Voice recognition already running.")
            return

        self.song_queue = queue.Queue()
        self.stop_voice_flag = False

        print("Loading voice model, please wait...")

        def run_voice():
            controller = VoiceRecognition(
                wake_words=["小爱同学", "播放"],
                silence_duration=1.5,
                callback=self.on_voice_detected,
                callback_stop=self.on_voice_stop,
                callback_pause=self.on_voice_pause,
                callback_resume=self.on_voice_resume,
                callback_next=self.on_voice_next,
                on_ready=lambda: print("Voice recognition ready, please speak...")
            )
            self.voice_controller = controller
            try:
                controller.start_monitor()
            except Exception as e:
                print(f"Voice recognition thread error: {e}")

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
            print("\nVoice recognition interrupted by user.")
        finally:
            self.stop_voice_flag = True
            if self.voice_controller:
                self.voice_controller.stop_monitor()
            if self.voice_thread:
                self.voice_thread.join(timeout=2)
            print("Voice recognition exited.")

    # ---------- Other commands ----------
    def cmd_cache(self, args):
        """Manage song cache: list or clean by bvid."""
        if not args:
            print("Usage: cache list | clean [bvid]")
            return
        subcmd = args[0].lower()
        if subcmd == "list":
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT bvid, title, file_path FROM song_cache ORDER BY last_play_time DESC")
                rows = cursor.fetchall()
            if not rows:
                print("Cache is empty.")
                return
            print("Cached songs:")
            for bvid, title, file_path in rows:
                print(f"  {bvid} - {title}")
                print(f"     File: {file_path}")
        elif subcmd == "clean":
            if len(args) == 1:
                confirm = input("Delete all cache files and records? (y/N): ").strip().lower()
                if confirm != 'y':
                    print("Cancelled.")
                    return
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.execute("SELECT file_path FROM song_cache")
                    rows = cursor.fetchall()
                    for (file_path,) in rows:
                        try:
                            Path(file_path).unlink()
                        except:
                            pass
                    conn.execute("DELETE FROM song_cache")
                print("All cache cleared.")
            else:
                bvid = args[1]
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.execute("SELECT file_path FROM song_cache WHERE bvid = ?", (bvid,))
                    row = cursor.fetchone()
                    if row:
                        try:
                            Path(row[0]).unlink()
                        except:
                            pass
                        conn.execute("DELETE FROM song_cache WHERE bvid = ?", (bvid,))
                        print(f"Cache cleared for: {bvid}")
                    else:
                        print(f"Cache not found: {bvid}")
        else:
            print("Unknown subcommand, available: list, clean")

    def cmd_exit(self, args):
        """Exit the program."""
        if self.player:
            self.player.terminate()
        sys.exit(0)

    def cmd_help(self, args):
        """Show usage of built-in commands."""
        if not args:
            print("Available commands:")
            for cmd in sorted(self.commands.keys()):
                brief = self.command_brief.get(cmd, "")
                print(f"  {cmd:<12} - {brief}")
            print("\nType 'help <command>' for detailed usage.")
        else:
            cmd_name = args[0]
            if cmd_name in self.command_usage:
                print(inspect.cleandoc(self.command_usage[cmd_name]))
            elif cmd_name in self.commands:
                print(f"No detailed help for command '{cmd_name}'.")
            else:
                print(f"Unknown command: {cmd_name}")

    def cmd_transform(self, args):
        """Convert a single audio/video file via GUI selection."""
        if not args:
            print("This command accepts no arguments, just type 'transform'.")
            return
        video_suffix = ['.mp4', '.mkv', '.avi']
        audio_suffix = ['.mp3', '.wav', '.flac', '.m4a', 'm4s']
        try:
            file_path = select_file.select(title="Select file to convert:", filetypes=[("Video File", video_suffix),
                                                                                       ("Audio File", audio_suffix)])
            if not file_path:
                print("User cancelled conversion.")
                return
            self.transform.convert(input_path=file_path, output_folder="./")
        except subprocess.CalledProcessError as e:
            print(f"FFmpeg conversion failed, error code {e.returncode}")
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
                print(f"Invalid folder: {folder}")
                return
        else:
            # Use GUI to select folder
            print("Please select a folder containing media files...")
            folder_path = select_file.select(title="Select folder to batch convert", mode='folder')
            if not folder_path:
                print("No folder selected, batch conversion cancelled.")
                return
            folder = Path(folder_path)
            print(f"Selected folder: {folder}")

        # Supported source file extensions (excluding .mp3)
        extensions = ['.mp4', '.mkv', '.avi', '.m4s', '.m4a', '.flac', '.wav']
        files = [f for f in folder.glob('*') if f.suffix.lower() in extensions]
        if not files:
            print(f"No supported media files found in {folder}")
            return

        self.mp3_dir.mkdir(parents=True, exist_ok=True)
        print(f"Converted MP3 files will be saved to: {self.mp3_dir}")

        success = 0
        for file in files:
            output_path = self.mp3_dir / (file.stem + ".mp3")
            if output_path.exists():
                print(f"Skipping existing MP3: {output_path.name}")
                continue
            try:
                print(f"Converting: {file.name} -> {output_path.name}")
                self.transform.convert(str(file), str(self.mp3_dir), ".mp3")
                success += 1
            except Exception as e:
                print(f"Conversion failed for {file.name}: {e}")
        print(f"Batch conversion done, {success}/{len(files)} succeeded.")

    def cmd_download(self, args):
        """
        Download one or multiple Bilibili audio streams by URL.
        Returns a list of downloaded file paths (empty on failure).
        """
        if not args:
            print("Usage: download <URL1> [URL2] ...")
            print("URL must start with https://www.bilibili.com/video/BV...")
            return []

        valid_urls = []
        invalid_urls = []
        for idx, url in enumerate(args, start=1):
            if 'bilibili.com/video/' in url and 'BV' in url:
                valid_urls.append(url)
            else:
                invalid_urls.append((idx, url))

        if invalid_urls:
            print("Warning: invalid URLs skipped:")
            for idx, url in invalid_urls:
                print(f"   #{idx}: {url}")
            print()

        if not valid_urls:
            print("No valid Bilibili URLs provided.")
            return []

        downloaded_paths = []
        for idx, url in enumerate(valid_urls, start=1):
            try:
                path = self.downloader.download_audio(url)
                downloaded_paths.append(path)
                print(f"✓ Download successful -> {path}")
            except Exception as e:
                print(f"✗ Download failed: {e}")
        return downloaded_paths

    # ---------- Initialization & config ----------
    def register_command(self, command, function, brief, usage):
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

    def check_first_run(self):
        """Run initial setup if this is the first launch."""
        if self.initial_setup.config_path.exists():
            self.initial_setup.config.read(self.initial_setup.config_path, encoding='utf-8')
        was_first_run = self.initial_setup.config.get('first_run', 'first_run', fallback='0') == '0'
        self.initial_setup.initial_setup()
        if was_first_run:
            print("Initialization done, program will restart to apply configuration...")
            self.restart_program()

    @staticmethod
    def restart_program():
        python = sys.executable
        os.chdir(Path(__file__).parent)
        os.execl(python, python, *sys.argv)

    @staticmethod
    def clear_screen():
        os.system('cls' if os.name == 'nt' else 'clear')

    @staticmethod
    def welcome():
        width = 60
        line = '-' * width
        print(line)
        print(f"{'BiliMusicCLI v1.0.0'.center(width)}")
        print(f"{'Bilibili Audio Downloader & Voice Music Assistant'.center(width)}")
        print(f"{'Author: Mr.Li'.center(width)}")
        print(line)
        print("  Features:")
        print("    • Download Bilibili video audio (saved as .m4s)")
        print("    • Voice command music playback (wake words: 小爱同学 / 播放)")
        print("    • Local cache with auto-playlist and version switching")
        print("    • Console commands: help, download, voice, exit, etc.")
        print(line)
        print("  Getting started: type 'help' or say '小爱同学播放<歌名>'")
        print(line)
        print()

    def cmd_register(self):
        self.register_command('exit', self.cmd_exit, brief="Exit", usage="exit")
        self.register_command('transform', self.cmd_transform, brief="Convert a file", usage="transform")
        self.register_command('help', self.cmd_help, brief="Show help", usage="help [command]")
        self.register_command('download', self.cmd_download, brief="Download by URL", usage="download <URL...>")
        self.register_command('voice', self.cmd_voice, brief="Voice control", usage="voice")
        self.register_command('cache', self.cmd_cache, brief="Cache management", usage="cache list | clean [bvid]")
        self.register_command('batch_transform', self.cmd_batch_transform,
                              brief="Batch convert to MP3", usage="batch_transform [folder]")

    def main_cui(self):
        self.cmd_register()
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
            os.system('chcp 65001 > nul')
        self.welcome()
        self.check_first_run()
        self.load_config()

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
                    print("\nPress Enter to continue...")
                else:
                    cmd_name = parts[0]
                    args = parts[1:]
                    if cmd_name in self.commands:
                        if cmd_name in ('exit', 'voice'):
                            self.commands[cmd_name](args)
                        else:
                            self.commands[cmd_name](args)
                            print("\nPress Enter to continue...")
                            input()
                    else:
                        print(f"Unknown command: {cmd_name}")
                        print("\nPress Enter to continue...")
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
                print(f"Unexpected error: {e}, details written to error.log")
                print("\nPress Enter to continue...")
                input()


if __name__ == '__main__':
    cui = MainCui()
    cui.main_cui()