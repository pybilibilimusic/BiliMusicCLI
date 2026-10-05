import threading
from pathlib import Path

class MpvPlayer:
    def __init__(self, auto_play_next_callback=None):
        self.player = None
        self.current_file = None
        self.auto_play_next = True
        self._auto_play_next_callback = auto_play_next_callback
        self._init_player()

    def _init_player(self):
        import mpv
        self.player = mpv.MPV(vo='null', vid=False, ao='wasapi')
        print("mpv player initialized successfully")  # 未翻译，保持原样
        @self.player.event_callback('end-file')
        def on_end_file(event):
            reason = event.reason if hasattr(event, 'reason') else event['reason']
            if reason == 0:
                print("\n[Playback finished]")  # 未翻译
                if self.auto_play_next and self._auto_play_next_callback:
                    threading.Thread(target=self._auto_play_next_callback, daemon=True).start()

    def play(self, file_path, start_pos=0):
        if not Path(file_path).exists():
            return False
        if self.player is None:
            self._init_player()
        self.player.stop()
        self.player.play(str(file_path))
        if start_pos > 0:
            self.player.seek(start_pos, reference="absolute")
        self.current_file = file_path
        print(f"Now playing: {Path(file_path).name}")  # 未翻译
        return True

    def pause(self):
        if self.player:
            self.player.pause = True
            print("Playback paused")  # 未翻译

    def resume(self):
        if self.player:
            self.player.pause = False
            print("Playback resumed")  # 未翻译

    def stop(self):
        if self.player:
            self.player.stop()
            print("Playback stopped")  # 未翻译

    # ---------- 音量（micro:bit 遥控 / 语音控制用） ----------

    def get_volume(self) -> int:
        """当前音量 0-100，取不到时按 100 处理（mpv 初始值可能是 None）。"""
        if not self.player:
            return 0
        try:
            value = self.player.volume
            return 100 if value is None else max(0, min(100, int(value)))
        except Exception:
            return 0

    def set_volume(self, value: int) -> int:
        """把音量夹到 0-100 后设置，返回实际生效值。"""
        if not self.player:
            return 0
        clamped = max(0, min(100, int(value)))
        try:
            self.player.volume = clamped
        except Exception:
            return self.get_volume()
        return clamped

    def adjust_volume(self, delta: int) -> int:
        return self.set_volume(self.get_volume() + delta)

    # ---------- 播放进度 ----------

    def get_time_pos(self):
        """当前播放位置（秒），拿不到返回 None。"""
        if not self.player:
            return None
        try:
            pos = self.player.time_pos
            return None if pos is None else float(pos)
        except Exception:
            return None

    def get_duration(self):
        """总时长（秒），直播 / 拿不到时返回 None。"""
        if not self.player:
            return None
        try:
            duration = self.player.duration
            return None if duration is None else float(duration)
        except Exception:
            return None

    def is_playing(self) -> bool:
        """正在播放（未暂停、未在结束态）。"""
        return bool(self.player and not self.player.pause and self.current_file)

    def terminate(self):
        if self.player:
            self.player.terminate()