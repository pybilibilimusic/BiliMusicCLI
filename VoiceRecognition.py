import numpy as np
import pyaudio
import torch
import silero_vad
import threading
import re
from typing import Callable, Optional, List
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess


class VoiceRecognition:
    """
    Voice recognition with wake word detection and ASR using FunASR.
    """

    def __init__(self,
                 wake_words: List[str],
                 sample_rate: int = 16000,
                 vad_chunk: int = 512,
                 silence_duration: float = 2,
                 device: str = "cpu",
                 vad_threshold: float = 0.5,
                 callback: Optional[Callable[[str], None]] = None,
                 on_ready: Optional[Callable[[], None]] = None,
                 callback_stop: Optional[Callable[[], None]] = None,
                 callback_resume: Optional[Callable[[], None]] = None,
                 callback_next: Optional[Callable[[], None]] = None,
                 callback_pause: Optional[Callable[[], None]] = None):
        """
        Initialize voice recognition.

        Args:
            wake_words: List of wake words (e.g., ["小爱同学", "播放"]).
            sample_rate: Audio sample rate (default 16000).
            vad_chunk: Number of samples per VAD chunk (default 512).
            silence_duration: Seconds of silence to end speech (default 2).
            device: 'cpu' or 'cuda:0'.
            vad_threshold: VAD speech probability threshold (0-1, default 0.5).
            callback: Called when a song name is recognized (after wake word removal).
            on_ready: Called when the model is loaded and audio stream is ready.
            callback_stop: Called when stop keywords are detected.
            callback_resume: Called when resume keywords are detected.
            callback_next: Called when next-track keywords are detected.
            callback_pause: Called when pause keywords are detected.
        """
        self.wake_words = wake_words
        self.sample_rate = sample_rate
        self.vad_chunk = vad_chunk
        self.silence_duration = silence_duration
        self.device = device
        self.vad_threshold = vad_threshold
        self.callback = callback
        self.on_ready = on_ready
        self.callback_stop = callback_stop
        self.callback_resume = callback_resume
        self.callback_pause = callback_pause
        self.callback_next = callback_next

        self._cleaned_up = False
        self._stop_flag = False
        self._audio_stream = None
        self._pyaudio_instance = None
        self._vad_model = None
        self._asr_model = None
        self._lock = threading.Lock()

    def _load_models(self):
        """Load FunASR and Silero VAD models."""
        print("Loading FunASR model...")
        self._asr_model = AutoModel(
            model="iic/SenseVoiceSmall",
            device=self.device,
            disable_pbar=True,
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 30000},
            disable_update=True
        )
        print("Loading Silero VAD model...")
        self._vad_model = silero_vad.load_silero_vad()
        print("Loading model complete.")

    def _record_until_silence(self):
        """
        Record audio until silence timeout.

        Returns:
            np.ndarray: Float32 audio array normalized to [-1, 1].
        """
        frames = []
        triggered = False
        silent_chunks = 0
        silence_threshold = int(self.silence_duration * self.sample_rate / self.vad_chunk)

        while not self._stop_flag:
            data = self._audio_stream.read(self.vad_chunk, exception_on_overflow=False)
            audio_float = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            audio_tensor = torch.from_numpy(audio_float)

            with torch.no_grad():
                speech_prob = self._vad_model(audio_tensor, self.sample_rate).item()

            if speech_prob > self.vad_threshold:
                if not triggered:
                    triggered = True
                silent_chunks = 0
            else:
                if triggered:
                    silent_chunks += 1

            if triggered:
                frames.append(data)

            if triggered and silent_chunks > silence_threshold:
                break

        audio_bytes = b''.join(frames)
        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        return audio_int16.astype(np.float32) / 32768.0

    def _recognize_and_extract(self, audio):
        """
        Run ASR, extract song name after wake word removal,
        and call appropriate callbacks based on keywords.
        """
        try:
            res = self._asr_model.generate(
                input=audio,
                language="auto",
                use_itn=True,
                batch_size_s=0,
            )
            if res and len(res) > 0 and "text" in res[0]:
                raw_text = res[0]["text"]
                text = rich_transcription_postprocess(raw_text)

                # Check for stop/pause/resume/next commands (priority)
                stop_keywords = ["停止", "暂停", "别放了", "结束"]
                if any(kw in text for kw in stop_keywords):
                    if self.callback_stop:
                        self.callback_stop()
                    return

                resume_keywords = ["继续", "接着放", "恢复播放"]
                if any(kw in text for kw in resume_keywords):
                    if self.callback_resume:
                        self.callback_resume()
                    return

                next_keywords = ["下一首", "切歌", "换一首"]
                if any(kw in text for kw in next_keywords):
                    if self.callback_next:
                        self.callback_next()
                    return

                # Check for wake words
                has_wake_word = any(wake in text for wake in self.wake_words)
                if not has_wake_word:
                    return

                # Extract song name: remove wake words and common command words
                remaining = text
                for wake in self.wake_words:
                    remaining = remaining.replace(wake, "")
                command_words = ["播放", "唱", "来一首", "我想听", "点一首"]
                for cmd in command_words:
                    remaining = remaining.replace(cmd, "")
                song_name = re.sub(r'[^\w\u4e00-\u9fff]', '', remaining).strip()
                if song_name and self.callback:
                    self.callback(song_name)

        except Exception as e:
            print(f"Recognition error: {e}")

    def _worker_loop(self):
        """Background worker: continuously record and recognize."""
        try:
            while not self._stop_flag:
                audio = self._record_until_silence()
                if len(audio) > 0:
                    threading.Thread(target=self._recognize_and_extract, args=(audio,), daemon=True).start()
        except Exception as e:
            print(f"Worker thread error: {e}")
        finally:
            self._cleanup()

    def _cleanup(self):
        """Release audio resources."""
        if self._cleaned_up:
            return
        self._cleaned_up = True

        if self._audio_stream:
            self._audio_stream.stop_stream()
            self._audio_stream.close()
        if self._pyaudio_instance:
            self._pyaudio_instance.terminate()
        print("The program has finished running, and the resources it occupied have been released.")

    def start_monitor(self):
        """
        Start voice monitoring (blocking).
        Loads models, initializes PyAudio, and enters worker loop.
        """
        self._load_models()

        # Initialize PyAudio
        self._pyaudio_instance = pyaudio.PyAudio()
        self._audio_stream = self._pyaudio_instance.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.sample_rate,
            input=True,
            frames_per_buffer=self.vad_chunk,
        )

        if self.on_ready:
            self.on_ready()

        try:
            self._worker_loop()
        except KeyboardInterrupt:
            self.stop_monitor()
        finally:
            self._cleanup()

    def stop_monitor(self):
        """Signal the worker loop to stop."""
        self._stop_flag = True