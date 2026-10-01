import subprocess
from pathlib import Path

class Transform:
    def __init__(self, ffmpeg=r"./ffmpeg"):
        self.ffmpeg_path = Path(ffmpeg)

    def convert(self, input_path: str, output_folder: str, output_suffix: str = ".mp3") -> None:
        input_path, output_folder = Path(input_path), Path(output_folder)
        output_file = output_folder / (input_path.stem + output_suffix)

        command = [
            str(self.ffmpeg_path), "-i", str(input_path),
            "-q:a", "0", "-map", "a",
            str(output_file), "-y"
        ]

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='ignore'
            )
            stdout, stderr = process.communicate(timeout=60)
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, command, output=stderr)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise Exception(f"ffmpeg conversion timeout for {input_path}")