import tkinter as tk
from tkinter import filedialog
# noinspection PyUnresolvedReferences
import ctypes

def select(title="Please select a file:", filetypes=None,mode='file'):
    """
        Open a file/folder selection dialog using tkinter.

        Args:
            title: Dialog title.
            filetypes: List of file type filters (e.g., [("Video", "*.mp4")]).
            mode: 'file' for file selection, 'folder' for directory selection.

        Returns:
            Selected path as string, or None if cancelled/error.
    """
    try:
        try:
            # Windows 8.1及以上
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except:
            try:
                # Windows 8及以下
                ctypes.windll.user32.SetProcessDPIAware()
            except:
                pass

        root = tk.Tk()
        root.withdraw()

        default_font = ("Microsoft YaHei UI", 9)  # Windows清晰字体
        root.option_add("*Font", default_font)

        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()

            dc = user32.GetDC(0)
            DPI_SCALEX = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)
            DPI_SCALEY = ctypes.windll.gdi32.GetDeviceCaps(dc, 90)
            user32.ReleaseDC(0, dc)

            scale_x = DPI_SCALEX / 96.0

            root.tk.call('tk', 'scaling', scale_x)
        except:
            pass

        root.resizable(False, False)

        root.option_add('*Dialog.msg.font', default_font)

        if mode == 'folder':
            path = filedialog.askdirectory(title=title)
        else:
            if filetypes is None:
                filetypes = [
                    ("Video file", "*.mp4 *.avi *.mkv *.mov *.wmv"),
                    ("Only MP4 file", "*.mp4"),
                    ("All file", "*.*")]
            path = filedialog.askopenfilename(
                title=title,
                filetypes=filetypes
            )

        root.destroy()

        return path

    except Exception as e:
        return None

if __name__ == "__main__":
    print(select())