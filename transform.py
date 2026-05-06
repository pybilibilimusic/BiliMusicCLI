import subprocess
import sys
from pathlib import Path

from select_file import select

class Transform:

    """Audio/Video to MP3 converter."""

    def __init__(self,ffmpeg=r"./ffmpeg"):
        """

        Initialize transformer with the path to ffmpeg executable.

        Args:
            ffmpeg: Full path to ffmpeg.exe (default: "./ffmpeg")

        """
        self.ffmpeg_path = Path(ffmpeg)

    def convert(self,input_path: str, output_folder: str, output_suffix: str = ".mp3") -> None:
        """
        Convert an audio or video file to MP3 (or other format specified by suffix).

        Supports common input formats such as .mp4, .m4s, .mkv, .avi, .m4a, .wav, .flac.
        ffmpeg output is printed to console in real time.

        Args:
            input_path: Path to the input file (string or Path)
            output_folder: Full path to the output file.
            output_suffix: Suffix (extension) for the output file, e.g. ".mp3", ".wav". Default is ".mp3".
        """

        input_path,output_folder = Path(input_path),Path(output_folder)
        output_file = output_folder / (input_path.stem + output_suffix)

        command = [
            str(self.ffmpeg_path),
            "-i", str(input_path),
            "-q:a", "0",
            "-map", "a",
            str(output_file),
            "-y"
        ]

        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )

        # Read and print each line from stderr as it becomes available
        for line in process.stderr:
            print(line, end='')
            sys.stdout.flush()

        return_code = process.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, command)

        print(f"Conversion successful: {output_file}")

if __name__ == "__main__":
    ffmpeg_path = r"./ffmpeg"
    video_suffix = ['.mp4','.mkv','.avi']
    audio_suffix = ['.mp3','.wav','.flac','.m4a','m4s']
    try:
        transform = Transform(ffmpeg=ffmpeg_path)
        file_path = select(title="请选择需转换的文件:", filetypes=[("Video File", video_suffix),
                                                               ("Audio File", audio_suffix)])
        transform.convert(input_path=file_path, output_folder="./")
    except subprocess.CalledProcessError:
        sys.exit("用户取消了选择，自动退出程序……")
    except Exception as e:
        print(e)