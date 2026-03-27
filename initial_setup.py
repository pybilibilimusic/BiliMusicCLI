import configparser
from pathlib import Path

import select_file


class Config:
    def __init__(self):
        self.config_path = Path("config.ini")
        self.config = configparser.ConfigParser()

    def default_config(self):
        self.config['first_run'] = {'first_run': '0'}
        #self.config['language'] = {'language': 'English'}
        self.config['paths'] = {
            'temporary_save_location': './temp',
            'logs': './log',
            'video_temp': './video_temp',
            'mp3': './mp3'
        }
        self.config['timeout'] = {'timeout': '5'}
        self.config['threads'] = {'threads': '4'}

    def setup_directories(self):
        dirs = [
            self.config['paths']['temporary_save_location'],
            self.config['paths']['logs'],
            self.config['paths']['video_temp'],
            self.config['paths']['mp3']
        ]

        for dir_path in dirs:
            Path(dir_path).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def inquiry(context, valid_value, default=None, target_type: type = str):
        while True:
            user_choice = input(context + " >? ")
            if user_choice == '' and default is not None:
                return default
            try:
                user_choice = target_type(user_choice)
            except ValueError:
                print("Please enter a valid type.")
                continue
            if user_choice not in valid_value:
                print(f"Invalid selection: {user_choice}, please try again")
            else:
                return user_choice

    @staticmethod
    def ask_folder(context, default):
        print(f"Please select the default folder to save downloaded {context} files.")
        print(f"If not selected, the default folder {default} will be used.")
        print("You can directly close the pop-up window to select the default value…")
        folder_path = select_file.select(title=f'Please select the default folder to save downloaded {context} files:',
                                         mode='folder')
        if folder_path is None:
            print(f"No folder selected, use the default folder {default} instead.")
            folder_path = default
        return folder_path

    def initial_setup(self):
        try:
            if self.config_path.exists():
                self.config.read(self.config_path, encoding='utf-8')
            else:
                self.default_config()
            first_run = self.config.get('first_run', 'first_run', fallback='0')
            if first_run == '1':
                return True
            else:
                print("First-time use detected, executing initialization program...")

                print("Would you like to use all the default values? The default values are as follows:")
                #print("Language:English,")
                print("Temporary file storage location:./temp,")
                print("Video file storage location:./video_temp,")
                print("MP3 file storage location:./mp3,")
                print("Timeout:5")
                print("Downloading Threads:4")

                use_default = self.inquiry(context="Please choose(Y or N):", valid_value=["Y", "y", "N", "n"],
                                           default="Y", target_type=str)
                if use_default.lower() == "y":
                    print("Initialization complete.")
                    self.default_config()
                    self.config['first_run'] = {'first_run': '1'}
                    return True
                else:
                    print("The user refused to use the default value.")

                # language = self.inquiry(context='Please select a language:',
                #              default = "English",
                #              valid_value=['Chinese','English'],
                #              target_type=str)
                # self.config['language'] = {'language': language}

                temporary_save_location = self.ask_folder(context="temporary", default="./temp")
                self.config['temporary_save_location'] = {'temporary_save_location': temporary_save_location}

                video_temp = self.ask_folder(context="video", default="./video_temp")
                self.config['paths']['video_temp'] = video_temp

                mp3 = self.ask_folder(context="mp3", default="./mp3")
                self.config['paths']['mp3'] = mp3

                timeout = self.inquiry(
                    context='Please enter the wait time when selecting this program (default:5 seconds),'
                            '\nThe available range is 1 to 10 seconds.',
                    valid_value=range(1, 11),
                    target_type=int)
                self.config['timeout'] = {'timeout': str(timeout)}

                threads = self.inquiry(context='Please enter the number of threads when downloading (default:4),'
                             '\nThe available range is 1 to 10 seconds.'
                             'Note: It is not recommended to have more than 5 threads, otherwise it may put a burden on the hard drive.',
                    valid_value=range(1, 10),
                    target_type=int)
                self.config['threads'] = {'threads': str(threads)}

                print("Initialization complete.")
                self.config['first_run'] = {'first_run': '1'}

                return True

        except KeyboardInterrupt:
            print("Initialization interrupted. Using default configuration.")
            self.default_config()

        except Exception as error:
            print(f"An error occurred during initialization: {error}")
            print("Falling back to default configuration.")
            self.default_config()

        finally:
            self.setup_directories()
            with open(self.config_path, 'w', encoding='utf-8') as f:
                self.config.write(f)


if __name__ == '__main__':
    config = Config()
    config.initial_setup()
