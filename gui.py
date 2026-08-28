import customtkinter as ctk
import threading
import logging
import os
from dotenv import load_dotenv
load_dotenv()
from bot import SentinelSurveyBot

# Configure sleek UI
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class TextboxHandler(logging.Handler):
    """Custom logging handler to route terminal logs to the GUI."""
    def __init__(self, textbox):
        super().__init__()
        self.textbox = textbox

    def emit(self, record):
        msg = self.format(record)
        # Schedule the update in the main thread
        self.textbox.after(0, self.append_text, msg)

    def append_text(self, msg):
        self.textbox.insert("end", msg + "\n")
        self.textbox.see("end")

class SentinelGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.title("SentinelCore Survey Bot - HUD")
        self.geometry("900x700")
        
        self.bot = None
        self.bot_thread = None
        
        self.setup_ui()
        self.setup_logging()

    def setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        
        # --- Top Frame (Controls) ---
        self.top_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.top_frame.grid(row=0, column=0, padx=20, pady=20, sticky="ew")
        self.top_frame.grid_columnconfigure(0, weight=1)
        
        self.url_entry = ctk.CTkEntry(self.top_frame, placeholder_text="Paste Survey URL here (leave blank to attach to existing browser)", height=40)
        self.url_entry.grid(row=0, column=0, padx=(0, 10), sticky="ew")
        
        self.launch_btn = ctk.CTkButton(self.top_frame, text="LAUNCH BOT", command=self.start_bot, height=40, font=("Inter", 14, "bold"))
        self.launch_btn.grid(row=0, column=1, padx=(0, 10))
        
        self.pause_btn = ctk.CTkButton(self.top_frame, text="⏸ PAUSE", command=self.toggle_pause, height=40, fg_color="#8B0000", hover_color="#600000", font=("Inter", 14, "bold"), state="disabled")
        self.pause_btn.grid(row=0, column=2)

        # --- Main HUD Display ---
        self.console_textbox = ctk.CTkTextbox(self, font=("Consolas", 13), fg_color="#1e1e1e", text_color="#00ff00", wrap="word")
        self.console_textbox.grid(row=1, column=0, padx=20, pady=(0, 20), sticky="nsew")
        self.console_textbox.insert("0.0", "=== SentinelCore HUD GUI Initialized ===\nWaiting for deployment...\n")

    def setup_logging(self):
        self.handler = TextboxHandler(self.console_textbox)
        self.handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%H:%M:%S'))
        # Get the global root logger to capture all bot output
        root_logger = logging.getLogger()
        root_logger.addHandler(self.handler)
        root_logger.setLevel(logging.INFO)

    def start_bot(self):
        self.launch_btn.configure(state="disabled", text="RUNNING")
        self.pause_btn.configure(state="normal")
        
        url = self.url_entry.get().strip()
        target_url = url if url else None
        
        self.bot_thread = threading.Thread(target=self.run_bot_loop, args=(target_url,), daemon=True)
        self.bot_thread.start()
        self.after(2000, self._poll_bot_thread)

    def _poll_bot_thread(self):
        if getattr(self, "bot_thread", None) and not self.bot_thread.is_alive():
            self.launch_btn.configure(state="normal", text="LAUNCH BOT")
            self.pause_btn.configure(state="disabled")
        else:
            self.after(2000, self._poll_bot_thread)

    def run_bot_loop(self, target_url):
        API_KEY = os.getenv("API_KEY", "")
        BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:3001/v1")
        MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.5-flash")
        if not API_KEY:
            logging.getLogger().error("[!] Set API_KEY in .env")
            return
        
        try:
            self.bot = SentinelSurveyBot(
                api_key=API_KEY,
                base_url=BASE_URL,
                model_name=MODEL_NAME,
                profile_name="mumbai_hr_executive_01"
            )
            self.bot.run_manual_hud(target_url)
        except Exception as e:
            logging.getLogger().error(f"Bot execution error: {e}")

    def toggle_pause(self):
        if self.bot:
            self.bot.is_paused = not self.bot.is_paused
            if self.bot.is_paused:
                self.pause_btn.configure(text="▶ RESUME", fg_color="#006400", hover_color="#004d00")
                logging.getLogger().info("\n[!] ⏸️ SCAN PAUSED VIA GUI.")
            else:
                self.pause_btn.configure(text="⏸ PAUSE", fg_color="#8B0000", hover_color="#600000")
                logging.getLogger().info("\n[+] ▶️ SCAN RESUMED VIA GUI.")

if __name__ == "__main__":
    app = SentinelGUI()
    app.mainloop()
