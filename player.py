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

    def terminate(self):
        if self.player:
            self.player.terminate()