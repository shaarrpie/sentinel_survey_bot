import logging
import os

from dotenv import load_dotenv

from core import SurveyBot


load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


def main() -> None:
    api_key = os.getenv("API_KEY", "").strip()
    base_url = os.getenv("BASE_URL", "").strip()
    model = os.getenv("MODEL_NAME", "").strip()

    missing = [
        name for name, value in {
            "API_KEY": api_key,
            "BASE_URL": base_url,
            "MODEL_NAME": model,
        }.items() if not value
    ]
    if missing:
        print(f"[-] Missing configuration: {', '.join(missing)}")
        return

    bot = SurveyBot(
        api_key=api_key,
        base_url=base_url,
        model=model,
        headless=os.getenv("HEADLESS", "0") == "1",
        profile_name="default",
    )
    try:
        bot.run_interactive()
    except KeyboardInterrupt:
        print("\n[!] Stopped by user")
    finally:
        bot.stop()


if __name__ == "__main__":
    main()
