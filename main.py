import sys
import logging
import os
from dotenv import load_dotenv
load_dotenv()
from core import SurveyBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def main():
    api_key = os.getenv("API_KEY") or os.getenv("FREELLMAPI_KEY", "")
    if not api_key:
        print("[-] Set API_KEY or FREELLMAPI_KEY in .env file")
        return

    bot = SurveyBot(
        api_key=api_key,
        base_url=os.getenv("BASE_URL", "http://127.0.0.1:3001/v1"),
        model=os.getenv("MODEL_NAME", "gpt-4o"),
        headless=os.getenv("HEADLESS", "0") == "1",
        profile_name="default"
    )
    try:
        url = input("[?] Survey URL: ").strip()
        if not url.startswith("http"):
            print("[-] Invalid URL")
            return
        bot.run(url)
    except KeyboardInterrupt:
        print("\n[!] Stopped by user")
    finally:
        bot.stop()

if __name__ == "__main__":
    main()
