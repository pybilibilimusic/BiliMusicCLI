import json
import urllib3
import qrcode
import requests
import time
from qrcode.main import QRCode

import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class GetCookies:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False

        self.get_qrcode = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
        self.poll_url = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
        self.headers = config.headers.copy()
        self.headers['Accept-Encoding'] = 'gzip, deflate'
        self.headers['Referer'] = 'https://passport.bilibili.com/login'
        self.headers['Origin'] = 'https://passport.bilibili.com'

        self.qrcode_path = 'qrcode.png'
        self.cookie_path = 'bilibili_cookies.json'

    def _save_qrcode_image(self, qrcode_url):
        """生成并保存二维码图片."""
        qrcode_image = qrcode.make(qrcode_url)
        qrcode_image.save(self.qrcode_path)

    def _get_qrcode_key(self):
        """申请二维码，保存图片，返回 qrcode_key."""
        qrcode_response = self.session.get(
            self.get_qrcode, headers=self.headers, timeout=10
        )
        qrcode_data = qrcode_response.json()["data"]
        qrcode_url = qrcode_data["url"]
        qrcode_key = qrcode_data["qrcode_key"]

        self._save_qrcode_image(qrcode_url)

        print()
        print("=" * 50)
        print(f"📷 二维码已保存至：{self.qrcode_path}")
        print("   请打开该图片，用手机B站App扫描")
        print("   扫描后请在手机端确认登录")
        print("=" * 50)
        print()

        return qrcode_key

    def _save_cookies(self):
        """保存 Session 内的 Cookie 为列表格式(兼容其他模块)."""
        cookies_list = [
            {
                'name': c.name,
                'value': c.value,
                'domain': c.domain,
                'path': c.path,
            }
            for c in self.session.cookies
        ]
        with open(self.cookie_path, 'w', encoding='utf-8') as f:
            json.dump(cookies_list, f, ensure_ascii=False, indent=2)
        print(f"✅ Cookie 已保存至 {self.cookie_path}")

    def _poll_until_done(self, qrcode_key):
        """轮询登录状态，返回 'success' / 'expired' / 'timeout'."""
        max_wait = 180
        waited = 0
        while waited < max_wait:
            poll_resp = self.session.get(
                self.poll_url,
                params={"qrcode_key": qrcode_key},
                headers=self.headers,
                timeout=10
            )
            poll_data = poll_resp.json()["data"]
            status_code = poll_data["code"]

            if status_code == 0:
                return 'success'
            elif status_code == 86038:
                return 'expired'
            elif status_code == 86090:
                print("⏳ 已扫描，请在手机上确认登录...")
            elif status_code == 86101:
                print("⏳ 等待扫描...")
            else:
                print(f"未知状态: {status_code}")

            time.sleep(2)
            waited += 2

        return 'timeout'

    def login(self):
        """主登录流程：循环申请二维码直到登录成功或超时."""
        while True:
            qrcode_key = self._get_qrcode_key()
            result = self._poll_until_done(qrcode_key)

            if result == 'success':
                print("✅ 登录成功！")
                self._save_cookies()
                return True
            elif result == 'expired':
                print("⚠️ 二维码已失效，正在重新生成...")
                continue
            else:
                print("❌ 扫码超时，请重试")
                return False

if __name__ == '__main__':
    cookies = GetCookies()
    cookies.login()