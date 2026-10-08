import configparser
from pathlib import Path
import threading
from modelscope import snapshot_download
import logging

import select_file


class InitialSetup:
    """Configuration management class for reading, writing, and initializing app settings"""

    def __init__(self):
        """Initialize config object, set config file path and ConfigParser instance"""
        self.config_path = Path("config.ini")          # Path to the configuration file
        self.config = configparser.ConfigParser()      # Config parser instance
        self._reset_config_file = False
        # 初始化向导里是否选择了「现在扫码登录」，由主程序（cmd_login）决定是否执行
        self.pending_login = False

    def _default_config(self):
        """Set default configuration items including first-run flag, paths, timeout and threads"""
        self.config['first_run'] = {'first_run': '0'}
        self.config['language'] = {'language': 'zh'}        # 语言选项，取值需与 lang.py 的键一致（zh / en）
        self.config['paths'] = {
            'temporary_save_location': './temp',   # Directory for temporary files
            'logs': './log',                       # Directory for logs
            'm4s_temp': './m4s_temp',          # Directory for m4s temporary files
            'mp3': './mp3'                         # Directory for MP3 output
        }
        self.config['timeout'] = {'timeout': '5'}   # Request timeout in seconds
        self.config['threads'] = {'threads': '4'}   # Number of download threads
        self.config['audio'] = {'input_device_index': '-1'}  #Default index of microphone
        self.config['microbit'] = {'port': ''}
        # 搜索反馈埋点：默认开着，随时能在 config.ini 里关掉。
        # 只记录本地的搜索选择（见 feedback.py），不记账号、不上传。
        self.config['feedback'] = {'enabled': '1'}

    def _setup_directories(self):
        """Create all required directories from config (create parents if needed)"""
        dirs = [
            self.config['paths']['temporary_save_location'],
            self.config['paths']['logs'],
            self.config['paths']['m4s_temp'],
            self.config['paths']['mp3']
        ]

        for dir_path in dirs:
            Path(dir_path).mkdir(parents=True, exist_ok=True)   # Create directory, ignore if exists

    @staticmethod
    def _inquiry(context, valid_value, default=None, target_type: type = str):
        """
        Prompt user for input and validate that the input is within allowed values

        :param context: Prompt text to display to the user
        :param valid_value: Collection of valid values (e.g., list, range)
        :param default: Default value if user presses Enter without input
        :param target_type: Type to convert user input to (e.g., int, str)
        :return: Validated user input
        """
        while True:
            user_choice = input(context + " >? ")
            # Return default if user pressed Enter and a default is provided
            if user_choice == '' and default is not None:
                return default
            try:
                user_choice = target_type(user_choice)   # Attempt type conversion
            except ValueError:
                print("Please enter a valid type.")
                continue
            if user_choice not in valid_value:
                print(f"Invalid selection: {user_choice}, please try again")
            else:
                return user_choice

    @staticmethod
    def _ask_folder(context, default):
        """
        Open a folder selection dialog for the user to choose a save directory

        :param context: Context for the prompt (e.g., "temporary", "mp3")
        :param default: Default path if user cancels the selection
        :return: Selected folder path or the default path
        """
        print(f"Please select the default folder to save downloaded {context} files.")
        print(f"If not selected, the default folder {default} will be used.")
        print("You can directly close the pop-up window to select the default value…")
        # Call select_file module to choose a folder
        folder_path = select_file.select(title=f'Please select the default folder to save downloaded {context} files:',
                                         mode='folder')
        if folder_path is None:
            print(f"No folder selected, use the default folder {default} instead.")
            folder_path = default
        return folder_path

    @staticmethod
    def _preload_models():
        try:
            logging.getLogger("modelscope").setLevel(logging.WARNING)
            print("Preloading voice models in background (first time takes 1-2 minutes)...")
            snapshot_download('iic/SenseVoiceSmall')
            snapshot_download('iic/speech_fsmn_vad_zh-cn-16k-common-pytorch')
            print("Preloading voice models complete.")
        except Exception as e:
            print(f"Preloading voice models fail: {e}")

    def _detect_microbit_port(self):
        """Check the micro:bit serial port and save it to config.ini"""
        try:
            import serial.tools.list_ports
            ports = serial.tools.list_ports.comports()
            for port in ports:
                desc = port.description.lower()
                if "mbed" in desc or "microbit" in desc:
                    self.config['microbit'] = {'port': port.device}
                    print(f"✅ micro:bit detected on {port.device}")
                    return port.device

            # Not detected micro:bit
            print("⚠️ micro:bit not detected, remote control disabled.")
            self.config['microbit'] = {'port': ''}
            return None
        except ImportError:
            print("⚠️ pyserial not installed, micro:bit detection skipped.")
            self.config['microbit'] = {'port': ''}
            return None

    def _record_microbit_port(self):
        print("\n#micro:bit config start.")
        microbit_choice = self._inquiry(
            context='Do you have a micro:bit?',
            valid_value=["Y", "y", "N", "n"],
            default="Y",
            target_type=str
        )
        if microbit_choice.lower() == 'y':
            print("Please connect micro:bit to the computer and press Enter to continue...")
            input()
            print("Detecting micro:bit...")
            port_number = self._detect_microbit_port()
            if port_number:
                self.config['microbit'] = {'port': port_number}
            else:
                self.config['microbit'] = {'port': ''}
        else:
            self.config['microbit'] = {'port': ''}
        return None

    def _record_login_choice(self, default='N'):
        """
        询问是否现在扫码登录 B 站。

        只负责收集意愿（写进 self.pending_login），真正的扫码流程由
        main_CUI.BiliLogin 执行，避免初始化模块反过来依赖主控模块。

        :param default: 直接回车时的默认值，Y 表示默认登录，N 表示默认跳过
        :return: 是否现在登录
        """
        print("\n#登录配置 Login config")
        print("扫码登录后才能下载 320kbps / Hi-Res 音质；不登录也能用，音质上限约 192kbps。")
        print("Log in to unlock 320kbps / Hi-Res audio; without login the bitrate caps at ~192kbps.")
        print("也可以稍后在命令行里输入 login 再登录 / You can also type 'login' later.")

        choice = self._inquiry(
            context="是否现在扫码登录？Log in now?(Y or N):",
            valid_value=["Y", "y", "N", "n"],
            default=default,
            target_type=str
        )
        self.pending_login = choice.lower() == 'y'
        return self.pending_login

    def configure_microphone(self):
        """List input devices, let the user select an input microphone, and save it to config.ini"""
        import pyaudio
        print("\nDetecting microphone device...")
        audio = pyaudio.PyAudio()

        # 先收集所有输入设备信息
        input_devices = []
        for i in range(audio.get_device_count()):
            try:
                info = audio.get_device_info_by_index(i)
                if info['maxInputChannels'] > 0:
                    input_devices.append((i, info['name'], info['maxInputChannels'], int(info['defaultSampleRate'])))
            except Exception:
                # 跳过无法查询的设备
                continue

        audio.terminate()

        if not input_devices:
            print("No input device (microphone) was found, the system default device will be used.")
            if 'audio' not in self.config:
                self.config['audio'] = {}
            self.config['audio']['input_device_index'] = '-1'
            return -1

        print("\nAvailable microphone devices:")
        for idx, (i, name, max_channels, default_rate) in enumerate(input_devices, 1):
            print(f"{idx}. {name} (Number of channels: {max_channels}, Default sample rate: {default_rate} Hz)")
        print("")

        # 让用户选择
        device_index = self._inquiry(
            context="Please select a microphone (enter the number):",
            valid_value=range(1, len(input_devices) + 1),  # 注意：用户看到的是 1-based
            default=1,
            target_type=int
        )

        # 用户选择的是 1-based，需要转回 0-based 索引
        selected_idx = input_devices[device_index - 1][0]

        if 'audio' not in self.config:
            self.config['audio'] = {}
        self.config['audio']['input_device_index'] = str(selected_idx)
        print(f"Microphone selected: {selected_idx if selected_idx != -1 else 'System default'}")
        return selected_idx

    def initial_setup(self):
        """
        Perform first-run initialization:
        - If config file exists and first_run == 1, skip interactive setup
        - If config file exists and first_run != 1, enter setup
        - If config file does not exist, enter setup
        - If user wants to reset config, enter setup
        :return: True after initialization is complete
        """
        try:
            # 检测配置文件是否存在
            config_exists = self.config_path.exists()

            if config_exists:
                self.config.read(self.config_path, encoding='utf-8')
                first_run = self.config.get('first_run', 'first_run', fallback='0')
            else:
                first_run = '0'
                self._reset_config_file = True

            if first_run == '1' and not self._reset_config_file:
                if __name__ == '__main__':
                    print("Initialization has been completed.")
                    print("Want to reset config file?")
                    reset_choice = self._inquiry(
                        context="Please choose(Y or N):",
                        valid_value=["Y", "y", "N", "n"],
                        default="Y",
                        target_type=str
                    )
                    if reset_choice.lower() == 'y':
                        self._reset_config_file = True
                        return self.initial_setup()
                    else:
                        return True
                return True

            print("First-time use detected, executing initialization program...")

            print("Would you like to use all the default values? The default values are as follows:")
            print("Language: English,")
            print("Temporary file storage location: ./temp,")
            print("M4s file storage location: ./m4s_temp,")
            print("MP3 file storage location: ./mp3,")
            print("Timeout: 5")
            print("Downloading Threads: 4")

            use_default = self._inquiry(
                context="Please choose(Y or N):",
                valid_value=["Y", "y", "N", "n"],
                default="Y",
                target_type=str
            )

            if use_default.lower() == "y":
                print("Initialization complete.")
                self._default_config()
                self._reset_config_file = False
                self._record_microbit_port()
                self._record_login_choice(default='N')
                self.config['first_run'] = {'first_run': '1'}
                self._setup_directories()
                with open(self.config_path, 'w', encoding='utf-8') as f:
                    self.config.write(f)
                return True

            print("The user refused to use the default value.")

            self._default_config()

            # Language setting
            lang_choice = self._inquiry(
                context="Please choose language / 请选择语言 (1: English, 2: 中文)",
                valid_value=[1, 2],
                default=2,
                target_type=int
            )
            self.config['language'] = {'language': 'en' if lang_choice == 1 else 'zh'}

            # Select temporary file save location
            temporary_save_location = self._ask_folder(context="temporary", default="./temp")
            self.config['paths']['temporary_save_location'] = temporary_save_location

            # Select m4s temp directory
            m4s_temp = self._ask_folder(context="m4s", default="./m4s_temp")
            self.config['paths']['m4s_temp'] = m4s_temp

            # Select MP3 output directory
            mp3 = self._ask_folder(context="mp3", default="./mp3")
            self.config['paths']['mp3'] = mp3

            # Set timeout (1-10 seconds)
            timeout = self._inquiry(
                context='Please enter the wait time when selecting this program (default:5 seconds),\n'
                        'The available range is 1 to 10 seconds.',
                valid_value=range(1, 11),
                target_type=int,
                default=5
            )
            self.config['timeout'] = {'timeout': str(timeout)}

            # Set number of download threads (1-9)
            threads = self._inquiry(
                context='Please enter the number of threads when downloading (default:4),\n'
                        'The available range is 1 to 9.\n'
                        'Note: It is not recommended to have more than 5 threads, otherwise it may put a burden on the hard drive.',
                valid_value=range(1, 10),
                target_type=int,
                default=4
            )
            self.config['threads'] = {'threads': str(threads)}

            # Preload voice models (background)
            threading.Thread(target=self._preload_models, daemon=True).start()

            # Configure microphone
            device_index = self.configure_microphone()
            self.config['audio'] = {'input_device_index': str(device_index)}

            self._record_microbit_port()

            # 登录：要不要现在扫码（自定义向导里默认登录）
            self._record_login_choice(default='Y')

            print("Initialization complete.")
            self.config['first_run'] = {'first_run': '1'}
            self._reset_config_file = False

            self._setup_directories()
            with open(self.config_path, 'w', encoding='utf-8') as f:
                self.config.write(f)

            return True

        except KeyboardInterrupt:
            print("\nInitialization interrupted by user. Using default configuration.")
            self._default_config()
            self.config['first_run'] = {'first_run': '1'}
            self._setup_directories()
            with open(self.config_path, 'w', encoding='utf-8') as f:
                self.config.write(f)
            return False

        except Exception as e:
            print(f"An error occurred during initialization: {e}")
            print("Falling back to default configuration.")
            self._default_config()
            self._setup_directories()
            with open(self.config_path, 'w', encoding='utf-8') as f:
                self.config.write(f)
            return False


if __name__ == '__main__':
    # Run initialization when this script is executed directly
    config = InitialSetup()
    config.initial_setup()