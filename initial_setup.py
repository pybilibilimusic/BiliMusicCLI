import configparser
from pathlib import Path
import threading
from modelscope import snapshot_download

import select_file


class InitialSetup:
    """Configuration management class for reading, writing, and initializing app settings"""

    def __init__(self):
        """Initialize config object, set config file path and ConfigParser instance"""
        self.config_path = Path("config.ini")          # Path to the configuration file
        self.config = configparser.ConfigParser()      # Config parser instance

    def _default_config(self):
        """Set default configuration items including first-run flag, paths, timeout and threads"""
        self.config['first_run'] = {'first_run': '0'}
        # self.config['language'] = {'language': 'English'}   # Language option (not yet enabled)
        self.config['paths'] = {
            'temporary_save_location': './temp',   # Directory for temporary files
            'logs': './log',                       # Directory for logs
            'm4s_temp': './m4s_temp',          # Directory for m4s temporary files
            'mp3': './mp3'                         # Directory for MP3 output
        }
        self.config['timeout'] = {'timeout': '5'}   # Request timeout in seconds
        self.config['threads'] = {'threads': '4'}   # Number of download threads

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
            print("Preloading voice models in background (first time takes 1-2 minutes)...")
            snapshot_download('iic/SenseVoiceSmall')
            snapshot_download('iic/speech_fsmn_vad_zh-cn-16k-common-pytorch')
            print("Preloading voice models complete.")
        except Exception as e:
            print(f"Preloading voice models fail: {e}")

    def initial_setup(self):
        """
        Perform first-run initialization:
        - Read existing config and check first_run flag
        - If not initialized (first_run != '1'), guide user through interactive setup
        - Allow user to accept all defaults or customize each option
        - Create required directories and save config file
        :return: True after initialization is complete
        """
        try:
            # Check if config file exists; read it if so
            if self.config_path.exists():
                self.config.read(self.config_path, encoding='utf-8')
            else:
                self._default_config()  # Generate default config if not present

            # Get first-run flag; if '1', initialization already done
            first_run = self.config.get('first_run', 'first_run', fallback='0')
            if first_run == '1':
                return True
            else:
                print("First-time use detected, executing initialization program...")

                # Show default values and ask whether to use all of them
                print("Would you like to use all the default values? The default values are as follows:")
                print("Language:English,")
                print("Temporary file storage location:./temp,")
                print("M4s file storage location:./m4s_temp,")
                print("MP3 file storage location:./mp3,")
                print("Timeout:5")
                print("Downloading Threads:4")

                use_default = self._inquiry(context="Please choose(Y or N):", valid_value=["Y", "y", "N", "n"],
                                            default="Y", target_type=str)
                if use_default.lower() == "y":
                    print("Initialization complete.")
                    self._default_config()  # Use all default config
                    self.config['first_run'] = {'first_run': '1'}  # Mark as initialized
                    return True
                else:
                    print("The user refused to use the default value.")

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

                # Select m4s temp directory (m4s_temp)
                m4s_temp = self._ask_folder(context="m4s", default="./m4s_temp")
                self.config['paths']['m4s_temp'] = m4s_temp

                # Select MP3 output directory
                mp3 = self._ask_folder(context="mp3", default="./mp3")
                self.config['paths']['mp3'] = mp3

                # Set timeout (1-10 seconds)
                timeout = self._inquiry(
                    context='Please enter the wait time when selecting this program (default:5 seconds),'
                            '\nThe available range is 1 to 10 seconds.', valid_value=range(1, 11), target_type=int)
                self.config['timeout'] = {'timeout': str(timeout)}

                # Set number of download threads (1-9, recommended ≤5)
                threads = self._inquiry(context='Please enter the number of threads when downloading (default:4),'
                                                '\nThe available range is 1 to 10 seconds.'
                                                'Note: It is not recommended to have more than 5 threads, otherwise it may put a burden on the hard drive.',
                                        valid_value=range(1, 10), target_type=int)
                self.config['threads'] = {'threads': str(threads)}

                threading.Thread(target=self._preload_models, daemon=True).start()

                print("Initialization complete.")
                self.config['first_run'] = {'first_run': '1'}   # Mark as initialized

                return True

        except KeyboardInterrupt:
            # User pressed Ctrl+C, fall back to default config
            print("Initialization interrupted. Using default configuration.")
            self._default_config()

        except Exception as error:
            # Any other exception also falls back to default config
            print(f"An error occurred during initialization: {error}")
            print("Falling back to default configuration.")
            self._default_config()

        finally:
            # Ensure directories exist and save config file regardless of exceptions
            self._setup_directories()
            with open(self.config_path, 'w', encoding='utf-8') as f:
                self.config.write(f)


if __name__ == '__main__':
    # Run initialization when this script is executed directly
    config = InitialSetup()
    config.initial_setup()