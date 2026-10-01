import json
import time
from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.edge.service import Service

def get_bilibili_cookie():
    """
    启动Edge浏览器，等待用户扫码登录B站，成功后提取并保存Cookie。
    """
    # 1. 配置Edge选项
    edge_options = Options()
    edge_options.binary_location = r'./edge/msedge.exe'
    # 添加一些配置，让Selenium看起来更像一个正常的浏览器
    edge_options.add_argument("--disable-blink-features=AutomationControlled")
    edge_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    edge_options.add_experimental_option('useAutomationExtension', False)
    edge_options.add_argument("--no-sandbox")
    edge_options.add_argument("--disable-dev-shm-usage")
    edge_options.add_argument("--disable-gpu")

    service = Service(executable_path=r".\msedgedriver.exe")

    driver = webdriver.Edge(service=service, options=edge_options)

    try:
        # 3. 打开B站登录页面
        driver.get("https://passport.bilibili.com/login")
        print("请在打开的浏览器中扫码登录...")
        max_wait = 120
        waited = 0
        logged_in = False

        while waited < max_wait:
            cookies = driver.get_cookies()
            for cookie in cookies:
                if cookie['name'] == 'SESSDATA':
                    logged_in = True
                    break
            if logged_in:
                break
            time.sleep(2)
            waited += 2
            print(f"\r等待扫码登录中... 已等待 {waited} 秒", end="", flush=True)

        if not logged_in:
            print("\n等待超时，未检测到登录状态，请重试。")
            return

        print("\n登录成功！正在获取Cookie...")

        # 这时候可以随意获取所有 Cookie 并保存
        cookies = driver.get_cookies()
        with open("bilibili_cookies.json", "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print("Cookie已保存至 bilibili_cookies.json")

        # 打印关键Cookie便于确认
        for cookie in cookies:
            if cookie['name'] in ['SESSDATA', 'bili_jct']:
                print(f"{cookie['name']}: {cookie['value'][:20]}...")

    except Exception as e:
        print(f"发生错误: {e}")
    finally:
        driver.quit()


if __name__ == "__main__":
    get_bilibili_cookie()