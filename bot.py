import time
import random
import os
import io
import base64
import json
import logging
import msvcrt
import datetime
import socket
import shutil
import re
from urllib.parse import urlparse
from dotenv import load_dotenv
load_dotenv()
# pyrefly: ignore [missing-import]
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from openai import OpenAI
from PIL import Image
import pytesseract

from participant_profile import get_persona, save_answer, get_cached_answer

if not os.path.exists("logs"):
    os.makedirs("logs")

if not os.path.exists("screenshots"):
    os.makedirs("screenshots")


def clean_model_text(value: str | None) -> str:
    text = (value or "").strip()
    if text.startswith("```json"):
        text = text[len("```json"):]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


log_filename = f"logs/survey_run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class PointerInput:
    __slots__ = ("x", "y")
    def __init__(self, x, y):
        self.x = x
        self.y = y

class MouseController:
    def __init__(self, driver):
        self.driver = driver
        self._attached = False
        self._mouse_input = PointerInput(0, 0)

    def cdp(self, method, params=None):
        if params is None:
            params = {}
        try:
            result = self.driver.execute_cdp_cmd(method, params)
            return result
        except Exception as e:
            logger.debug(f"[-] CDP {method} failed: {e}")
            return None

    def _attach(self):
        if not self._attached:
            try:
                self.cdp("Browser.enable", {})
                self._attached = True
            except Exception:
                pass

    def _detach(self):
        if self._attached:
            try:
                self.cdp("Browser.disable", {})
            except Exception:
                pass
            self._attached = False

    def _bezier(self, x0, y0, x1, y1, n=12):
        pts = []
        for i in range(n + 1):
            t = i / n
            x = x0 + (x1 - x0) * t
            y = y0 + (y1 - y0) * t
            pts.append((int(x), int(y)))
        return pts

    def move(self, x, y, duration=0, origin="viewport", button="none"):
        if not self._attached:
            self._attach()
        pts = self._bezier(self._mouse_input.x, self._mouse_input.y, x, y)
        per = duration / max(len(pts) - 1, 1)
        for i, (px, py) in enumerate(pts):
            dt = int(per * random.uniform(0.7, 1.3))
            self.cdp("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": px,
                "y": py,
                "button": button,
                "duration": dt
            })
            self._mouse_input.x = px
            self._mouse_input.y = py

    def click(self, x, y, width=1, height=1):
        """Element-scaled jitter CDP click. Returns True on success so the
        caller can fall back to a synthetic click only when this failed
        (round 30: old jitter used window dims — could miss small elements
        by ~80px — and success was never reported)."""
        try:
            self._attach()
            jitter_x = random.uniform(-0.25, 0.25) * max(1, width)
            jitter_y = random.uniform(-0.25, 0.25) * max(1, height)
            tx = int(x + jitter_x)
            ty = int(y + jitter_y)
            self.move(tx, ty, duration=random.randint(80, 180))
            self.cdp("Input.dispatchMouseEvent", {
                "type": "mousePressed",
                "x": tx,
                "y": ty,
                "button": "left",
                "clickCount": 1
            })
            self.cdp("Input.dispatchMouseEvent", {
                "type": "mouseReleased",
                "x": tx,
                "y": ty,
                "button": "left",
                "clickCount": 1
            })
            return True
        except Exception as e:
            logger.debug("CDP click failed: %s", e)
            return False

    def human_move_sequence(self, start, end, total_ms=350):
        try:
            self._attach()
            sx, sy = int(start[0]), int(start[1])
            ex, ey = int(end[0]), int(end[1])
            self.cdp("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": sx,
                "y": sy,
                "button": "none",
                "duration": 0
            })
            self._mouse_input.x = sx
            self._mouse_input.y = sy
            n = random.randint(8, 14)
            pts = self._bezier(sx, sy, ex, ey, n)
            per = total_ms / n
            for i, (px, py) in enumerate(pts):
                dt = int(per * random.uniform(0.7, 1.3))
                self.cdp("Input.dispatchMouseEvent", {
                    "type": "mouseMoved",
                    "x": px,
                    "y": py,
                    "button": "none",
                    "duration": dt
                })
                self._mouse_input.x = px
                self._mouse_input.y = py
        except Exception as e:
            logger.debug(f"[-] Human move sequence failed: {e}")

def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def provider_location(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


tesseract_cmd = os.getenv("TESSERACT_CMD") or shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

class ProfileManager:
    """Manages the Multi-Account Cookie Vault."""
    def __init__(self, base_dir="profiles"):
        self.base_dir = os.path.abspath(base_dir)
        if not os.path.exists(self.base_dir):
            os.makedirs(self.base_dir)

    def get_profile_path(self, profile_name):
        profile_path = os.path.join(self.base_dir, profile_name)
        if not os.path.exists(profile_path):
            os.makedirs(profile_path)
        return profile_path

class SentinelSurveyBot:
    def __init__(self, api_key, base_url, model_name, profile_name="default_profile", sweatshop_mode=False):
        host, port = provider_location(base_url)
        if not is_port_in_use(port) and host in {"127.0.0.1", "localhost"}:
            logger.warning(
                "[!] AI provider not detected at %s:%s. Start the provider configured "
                "by BASE_URL before running the bot.",
                host, port,
            )
        self.ai_client = OpenAI(
            base_url=base_url,
            api_key=api_key
        )
        self.model_name = model_name
        self.sweatshop_mode = sweatshop_mode
        self.profile_name = profile_name
        self.memory_log = []
        self.is_paused = False
        self.gui_shutdown = False
        
        logger.info(f"[*] Loading Cookie Vault for Profile: {profile_name}...")
        self.profile_mgr = ProfileManager()
        user_data_dir = self.profile_mgr.get_profile_path(profile_name)

        logger.info("[*] Spooling up stealth browser fingerprint...")
        
        try:
            logger.info("[*] Attempting to attach to existing Chrome instance on port 9222...")
            attach_options = uc.ChromeOptions()
            attach_options.debugger_address = "127.0.0.1:9222"
            self.driver = uc.Chrome(options=attach_options)
            logger.info("[+] Successfully attached to existing Chrome window!")
        except Exception:
            logger.info("[*] No existing Chrome found. Launching a new stealth browser...")
            options = uc.ChromeOptions()
            options.add_argument('--disable-blink-features=AutomationControlled')
            options.add_argument("--disable-infobars")
            options.add_argument("--disable-notifications")
            options.add_argument("--window-size=1280,800")
            # options.add_argument("--remote-debugging-port=9222") # MASSIVE RED FLAG FOR CINT/CLOUDFLARE
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-blink-features=AutomationControlled")
            
            if sweatshop_mode:
                logger.info("[!] Sweatshop Mode Activated. Browser will run invisibly off-screen.")
                options.add_argument("--window-position=-32000,-32000")
            else:
                options.add_argument("--window-position=0,0")

            self.driver = uc.Chrome(options=options, user_data_dir=user_data_dir)
        self.actions = ActionChains(self.driver)
        self.mouse = MouseController(self.driver)

    def save_cookies(self):
        """Explicitly dumps cookies to JSON to ensure session persistence."""
        try:
            cookies = self.driver.get_cookies()
            cookie_file = os.path.join(self.profile_mgr.get_profile_path(self.profile_name), "cookies.json")
            
            existing_cookies = []
            if os.path.exists(cookie_file):
                try:
                    with open(cookie_file, 'r') as f:
                        existing_cookies = json.load(f)
                except Exception:
                    pass
                
            cookie_dict = {f"{c['name']}_{c.get('domain', '')}": c for c in existing_cookies}
            for c in cookies:
                cookie_dict[f"{c['name']}_{c.get('domain', '')}"] = c
                
            with open(cookie_file, 'w') as f:
                json.dump(list(cookie_dict.values()), f)
        except Exception:
            pass

    def load_cookies(self):
        """Injects cookies for the current domain."""
        try:
            cookie_file = os.path.join(self.profile_mgr.get_profile_path(self.profile_name), "cookies.json")
            if os.path.exists(cookie_file):
                with open(cookie_file, 'r') as f:
                    cookies = json.load(f)
                count = 0
                for cookie in cookies:
                    try:
                        self.driver.add_cookie(cookie)
                        count += 1
                    except Exception:
                        pass
                if count > 0:
                    logger.info(f"[+] Injected {count} saved cookies for this session.")
                    self.driver.refresh()
        except Exception:
            pass

    def get_page_fingerprint(self):
        """Generates a unique signature of the page state based on text and input counts."""
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
            text_sig = body.text.strip()[:4000]
            inputs = self.driver.find_elements(By.CSS_SELECTOR, "input, textarea, select")
            visible_inputs = sum(1 for el in inputs if el.is_displayed())
            checked_inputs = sum(1 for el in inputs if el.get_attribute("checked") or el.get_attribute("selected"))
            return f"{text_sig}_{visible_inputs}_{checked_inputs}"
        except Exception:
            return ""

    def get_learned_rules(self):
        rules_file = os.path.join(self.profile_mgr.get_profile_path(self.profile_name), "learned_rules.txt")
        if os.path.exists(rules_file):
            with open(rules_file, "r") as f:
                return f.read().strip()
        return ""

    def add_learned_rule(self, new_rule):
        rules_file = os.path.join(self.profile_mgr.get_profile_path(self.profile_name), "learned_rules.txt")
        existing = self.get_learned_rules()
        if new_rule and f"- {new_rule}" not in existing:
            with open(rules_file, "a") as f:
                f.write(f"- {new_rule}\n")
            logger.info(f"[+] Added new rule to training matrix: {new_rule}")

    def learn_from_disqualification(self):
        if not self.memory_log:
            return
        logger.info("\n[!] DISQUALIFICATION DETECTED. Analyzing failure to train AI...")
        
        recent_memory = "\n".join(self.memory_log[-5:])
        prompt = f"I was taking a survey and got disqualified right after answering these questions.\n\nRecent Q&A:\n{recent_memory}\n\nBased on standard survey traps (e.g., prohibited industry, failing a bot check, contradictory answers, or demographic mismatch), identify which answer likely caused the disqualification. Reply ONLY with a 1-sentence strict rule I should add to my persona to never make this mistake again. Start the rule with 'NEVER' or 'ALWAYS'. Do not explain your reasoning."
        try:
            response = self.ai_client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=200
            )
            rule = clean_model_text(response.choices[0].message.content)
            
            if "</think>" in rule:
                rule = rule.split("</think>")[-1].strip()
            
            self.add_learned_rule(rule)
            logger.info(f"[+] AI updated its own persona matrix. It will not fail this way again: {rule}\n")
            
            # Clear memory so we don't double-trigger if we stay on the page
            self.memory_log = []
        except Exception as e:
            logger.error(f"[-] AI failed to learn from disqualification: {e}")

    def human_mouse_move(self, element):
        try:
            self.actions.move_to_element(element).perform()
            time.sleep(random.uniform(0.1, 0.4))
        except Exception:
            pass

    def human_reading_delay(self, text):
        words = len(text.split())
        reading_time = words / 3.3
        jitter = random.uniform(1.0, 3.5)
        total_delay = min(reading_time + jitter, 10.0)
        logger.info(f"[*] Simulating human reading time: waiting {total_delay:.2f}s...")
        time.sleep(total_delay)

    def human_type(self, element, text):
        for char in text:
            element.send_keys(char)
            delay = random.uniform(0.04, 0.12)
            if random.random() < 0.05:
                delay += random.uniform(0.3, 0.8)
            time.sleep(delay)
            if random.random() < 0.02:
                element.send_keys(random.choice("abcdefghijklmnopqrstuvwxyz"))
                time.sleep(random.uniform(0.1, 0.2))
                element.send_keys(u'\ue003')
                time.sleep(random.uniform(0.1, 0.3))

    def extract_text_from_image(self, img_element):
        """Uses Tesseract OCR to read text from survey image traps, with AI Vision fallback."""
        try:
            img_base64 = self.driver.execute_script("""
                var canvas = document.createElement('canvas');
                var context = canvas.getContext('2d');
                var img = arguments[0];
                canvas.width = img.width;
                canvas.height = img.height;
                context.drawImage(img, 0, 0);
                return canvas.toDataURL('image/png').substring(22);
            """, img_element)
            
            image = Image.open(io.BytesIO(base64.b64decode(img_base64)))
            text = pytesseract.image_to_string(image).strip()
            
            if text:
                return text
                
            # --- Fallback to AI Vision ---
            logger.info("[*] Tesseract returned empty, falling back to AI Vision OCR...")
            response = self.ai_client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Extract all readable text from this image exactly as written. Reply ONLY with the text, no explanations."},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_base64}"}}
                        ]
                    }
                ],
                max_tokens=300
            )
            fallback_text = response.choices[0].message.content.strip()
            return fallback_text
            
        except Exception as e:
            logger.error(f"[-] OCR/Vision Error: {e}")
            return ""

    def ask_deepseek_advanced(self, question_text, options_text_list=None, image_context="", is_sniper=False):
        memory_string = "\n".join(self.memory_log[-15:]) if self.memory_log else "None yet."

        participant_profile_block = get_persona()
        system_persona = f"""
        You are a real human taking a survey.

        {participant_profile_block}

        PREVIOUS ANSWERS IN THIS SURVEY:
        {memory_string}
        """
        
        learned_rules = self.get_learned_rules()
        if learned_rules:
            system_persona += f"\n\nLEARNED RULES FROM PAST DISQUALIFICATIONS (CRITICAL):\n{learned_rules}"

        if image_context:
            question_text += f"\n[The image attached says: '{image_context}']"

        # Safeguard: Prevent massive pages from blowing up the context window
        if len(question_text) > 8000:
            question_text = question_text[:8000] + "\n...[TRUNCATED FOR TOKEN LIMITS]"

        if is_sniper:
            prompt = f"Question: \"{question_text}\"\nThis is a massive dropdown question. Look at your Persona and tell me exactly what 1-2 keywords I should search for in the dropdown to find your answer (e.g., 'Mumbai', 'Maharashtra', or '1994'). Reply ONLY with the keywords, nothing else."
        else:
            options_string = "\n".join([f"- {opt}" for opt in options_text_list]) if options_text_list else "No choice options (open-ended/written question)."
            prompt = f"""
            Question(s) on the page:
            "{question_text}"
            
            Available click options:
            {options_string}
            
            Identify any trap questions first. Choose answers that align with your persona and previous decisions.
            
            Format your response exactly like this:
            
            [ANALYSIS]
            Question: <Short summary of the questions detected>
            Reasoning: <Your step-by-step reasoning matching your persona and memory>
            Answers:
            - <Question/Label>: <Your value or click selection>
            
            [EXECUTION]
            Type: <Keyword/Label of textbox> -> <Value to type>
            Click: X:..., Y:... (Provide the EXACT coordinate tag of the option to click)
            
            For example:
            [ANALYSIS]
            Question: Age and Gender
            Reasoning: My persona age is 32 and gender is Female.
            Answers:
            - Gender: Female
            - Age: 32
            
            [EXECUTION]
            Type: age -> 32
            Click: X:340, Y:512
            """

        try:
            response = self.ai_client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "system", "content": system_persona}, {"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=4096
            )
            
            raw_answer = clean_model_text(response.choices[0].message.content)
            
            if "</think>" in raw_answer:
                raw_answer = raw_answer.split("</think>")[-1].strip()
            
            return raw_answer
        except Exception as e:
            logger.error(f"[-] AI Error: {e}")
            return None

    def execute_type_action(self, label_keyword, value):
        try:
            # First try contenteditable
            contenteditables = self.driver.find_elements(By.CSS_SELECTOR, "[contenteditable='true']")
            visible_ces = [el for el in contenteditables if el.is_displayed()]
            
            best_input = None
            
            # Check contenteditables
            for ce in visible_ces:
                placeholder = ce.get_attribute("data-placeholder") or ""
                parent_text = ""
                try:
                    parent = ce.find_element(By.XPATH, "..")
                    parent_text = parent.text.lower()
                except Exception:
                    pass
                search_pool = f"{placeholder} {parent_text} {ce.get_attribute('id') or ''}".lower()
                if label_keyword.lower() in search_pool:
                    best_input = ce
                    break
            
            # Then try regular inputs
            if not best_input:
                inputs = self.driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input[type='number'], input[type='email'], textarea")
                visible_inputs = [el for el in inputs if el.is_displayed()]
                
                for inp in visible_inputs:
                    inp_id = inp.get_attribute("id") or ""
                    inp_name = inp.get_attribute("name") or ""
                    inp_placeholder = inp.get_attribute("placeholder") or ""
                    
                    label_text = ""
                    if inp_id:
                        try:
                            lbl = self.driver.find_element(By.CSS_SELECTOR, f"label[for='{inp_id}']")
                            label_text = lbl.text.lower()
                        except Exception:
                            pass
                    
                    parent_text = ""
                    try:
                        parent = inp.find_element(By.XPATH, "..")
                        parent_text = parent.text.lower()
                    except Exception:
                        pass
                    
                    search_pool = f"{inp_id} {inp_name} {inp_placeholder} {label_text} {parent_text}".lower()
                    if label_keyword.lower() in search_pool:
                        best_input = inp
                        break
                    
                if not best_input and visible_inputs:
                    best_input = visible_inputs[0]
            
            if best_input:
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", best_input)
                try:
                    rect = best_input.rect
                    cx = int(rect['x'] + rect['width']/2)
                    cy = int(rect['y'] + rect['height']/2)
                    self.mouse.move(cx, cy, duration=random.randint(60, 140))
                except Exception:
                    self.human_mouse_move(best_input)
                
                if best_input.get_attribute("contenteditable") == "true":
                    self.driver.execute_script("arguments[0].textContent = arguments[1];", best_input, value)
                    self.driver.execute_script("""
                        const el = arguments[0];
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    """, best_input)
                else:
                    best_input.clear()
                    self.human_type(best_input, value)
                logger.info(f"    [+] Typed '{value}' into element matching '{label_keyword}'")
        except Exception as e:
            logger.error(f"    [-] Failed to type into element '{label_keyword}': {e}")

    def execute_click_action(self, label_keyword, options_elements):
        try:
            matched = False
            label_lower = label_keyword.lower().strip()
            for opt_el in options_elements:
                opt_text = opt_el.text.strip()
                if not opt_text:
                    opt_text = opt_el.get_attribute("aria-label") or ""
                if not opt_text:
                    opt_text = opt_el.get_attribute("title") or ""
                if not opt_text and opt_el.tag_name == "input":
                    try:
                        row = opt_el.find_element(By.XPATH, "./ancestor::tr")
                        opt_text = row.text.strip() + " - " + (opt_el.get_attribute("value") or "")
                    except Exception:
                        pass
                
                try:
                    rect = opt_el.rect
                    cx = int(rect['x'] + rect['width']/2)
                    cy = int(rect['y'] + rect['height']/2)
                    coord_marker = f"X:{cx}, Y:{cy}"
                except Exception:
                    coord_marker = ""
                
                opt_text = f"{opt_text} {coord_marker}".lower().strip()
                
                if opt_text and (label_lower in opt_text or (coord_marker and coord_marker.lower() in label_lower)):
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", opt_el)
                    try:
                        rect = opt_el.rect
                        cx = int(rect['x'] + rect['width']/2)
                        cy = int(rect['y'] + rect['height']/2)
                        clicked = self.mouse.click(cx, cy,
                                                   int(rect['width']),
                                                   int(rect['height']))
                    except Exception:
                        clicked = False
                    if not clicked:
                        self.human_mouse_move(opt_el)
                        time.sleep(random.uniform(0.3, 0.8))
                        try:
                            self.actions.move_to_element(opt_el).click().perform()
                        except Exception:
                            try:
                                self.driver.execute_script("arguments[0].click();", opt_el)
                            except Exception:
                                self.driver.execute_script(f"document.elementFromPoint({cx} - window.pageXOffset, {cy} - window.pageYOffset).click();")
                    logger.info(f"    [+] Clicked option at {coord_marker}")
                    matched = True
                    break
            if not matched:
                cx, cy = None, None
                
                explicit_match = re.search(r'x:\s*(\d+)\s*,\s*y:\s*(\d+)', label_lower, re.IGNORECASE)
                if explicit_match:
                    cx = int(explicit_match.group(1))
                    cy = int(explicit_match.group(2))
                else:
                    bracket_match = re.search(r'\[\s*(\d+)\s*,\s*(\d+)\s*\]', label_lower)
                    if bracket_match:
                        cx = int(bracket_match.group(1))
                        cy = int(bracket_match.group(2))
                
                if cx is not None and cy is not None:
                    try:
                        self.mouse.click(cx, cy)
                        logger.info(f"    [+] Fallback: CDP clicked raw coordinates X:{cx}, Y:{cy}")
                        matched = True
                    except Exception:
                        try:
                            success = self.driver.execute_script(f"""
                                var el = document.elementFromPoint({cx}, {cy});
                                if(el) {{
                                    el.click();
                                    return true;
                                }}
                                return false;
                            """)
                            if success:
                                logger.info(f"    [+] Fallback: Clicked raw coordinates X:{cx}, Y:{cy}")
                                matched = True
                            else:
                                logger.error(f"    [-] Fallback failed: No element found at X:{cx}, Y:{cy}")
                        except Exception as e:
                            logger.error(f"    [-] Fallback JS error at X:{cx}, Y:{cy}: {e}")

            if not matched:
                logger.warning(f"    [!] Click target not found in options: {label_keyword}")
        except Exception as e:
            logger.error(f"    [-] Failed to click '{label_keyword}': {e}")

    def execute_autonomous_action(self, ans, options_elements):
        if not ans: return
        logger.info("[*] Attempting autonomous DOM injection...")
        
        execution_lines = []
        if "[EXECUTION]" in ans:
            exec_part = ans.split("[EXECUTION]")[-1].strip()
            execution_lines = [line.strip() for line in exec_part.split('\n') if line.strip()]
        else:
            execution_lines = [line.strip() for line in ans.split('\n') if line.strip()]
            
        try:
            for line in execution_lines:
                line_lower = line.lower()
                if line_lower.startswith("type:") and "->" in line:
                    parts = line.split(":", 1)[1].split("->", 1)
                    label_keyword = parts[0].strip()
                    val = parts[1].strip()
                    self.execute_type_action(label_keyword, val)
                elif line_lower.startswith("click:"):
                    label_keyword = line.split(":", 1)[1].strip()
                    self.execute_click_action(label_keyword, options_elements)
                else:
                    if "[EXECUTION]" in ans and line.strip() and len(line) < 150 and not line.startswith("[") and not line.startswith("*"):
                        self.execute_click_action(line.strip(), options_elements)
                            
            time.sleep(random.uniform(1.2, 2.5))
            # Attempt to click Next with i18n support
            try:
                next_btn = None
                i18n_keywords = ['next', 'continue', 'submit', '>>', '→', 'done',
                                 'siguiente', 'weiter', 'suivant', 'avanti',
                                 'volgende', 'następne', 'proceed', 'prosseguir',
                                 'próximo', 'continuar', 'finalizar']
                btns = self.driver.find_elements(By.CSS_SELECTOR, "input[type='submit'], button[type='submit'], input[type='button'], button, [id*='Next' i], [name*='Next' i], [class*='next' i], [class*='btn' i], [class*='button' i]")
                for btn in btns:
                    btn_text = btn.text.lower() if btn.text else btn.get_attribute("value")
                    if btn_text:
                        btn_text = btn_text.lower()
                        if any(k in btn_text for k in i18n_keywords):
                            next_btn = btn
                            break
                if not next_btn and btns:
                    for btn in btns:
                        if btn.get_attribute("id") == "NextButton":
                            next_btn = btn
                            break
                    if not next_btn:
                        next_btn = btns[-1] # Fallback to last button
                
                if next_btn:
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", next_btn)
                    rect = next_btn.rect
                    cx = int(rect['x'] + rect['width'] / 2)
                    cy = int(rect['y'] + rect['height'] / 2)
                    clicked = self.mouse.click(
                        cx, cy, int(rect['width']), int(rect['height'])
                    )
                    if not clicked:
                        self.human_mouse_move(next_btn)
                        self.actions.move_to_element(next_btn).click().perform()
                    logger.info("    [+] Autonomously clicked Next page.")
            except Exception as next_err:
                logger.debug(f"    [!] Could not click Next button: {next_err}")
        except Exception as e:
            logger.error(f"    [-] Autonomous DOM injection failed: {e}")

    def run_manual_hud(self, target_url=None):
        if target_url:
            logger.info(f"[*] Navigating to {target_url}...")
            self.driver.get(target_url)
            self.load_cookies()
        logger.info("[+] SentinelCore Manual HUD Active. Monitoring screen for questions...")
        logger.info("[+] Hotkey Active: Press 'P' in this console at any time to PAUSE/RESUME the scanner.")
        
        last_fingerprint = ""
        fingerprint_since = time.time()
        screenshot_taken_for_current = False
        
        while not getattr(self, "gui_shutdown", False):
            time.sleep(1.5)
            
            # --- GUI / Hardware Pause Toggle ---
            if msvcrt.kbhit():
                char = msvcrt.getch()
                if char.lower() == b'p':
                    self.is_paused = not self.is_paused
                    if self.is_paused:
                        logger.info("\n[!] ⏸️ SCAN PAUSED. Press 'P' or use GUI to resume.")
                    else:
                        logger.info("[+] ▶️ SCAN RESUMED.\n")
                        last_fingerprint = ""  # Force immediate rescan
                        fingerprint_since = time.time()
                        screenshot_taken_for_current = False
                        
            if getattr(self, "is_paused", False):
                continue
            
            try:
                # 1. Recover browser window handle if closed or lost
                try:
                    _ = self.driver.current_window_handle
                except Exception:
                    handles = self.driver.window_handles
                    if handles:
                        self.driver.switch_to.window(handles[0])
                        logger.info("[+] Recovered closed browser tab/window connection.")
                    else:
                        logger.warning("[!] No active browser windows detected.")
                        time.sleep(2)
                        continue

                # 2. Automatically follow new tabs/popups (CPX Research loves opening new tabs)
                try:
                    handles = self.driver.window_handles
                    if len(handles) > 1:
                        current_handle = self.driver.current_window_handle
                        if current_handle != handles[-1]:
                            self.driver.switch_to.window(handles[-1])
                            logger.info("[+] Switched to new popup tab.")
                except Exception:
                    pass

                current_url = self.driver.current_url.lower()
                if any(k in current_url for k in ["disqualified", "screenout", "reward=0", "&term="]):
                    if self.memory_log:
                        self.learn_from_disqualification()
                    time.sleep(5)
                    continue

                self.driver.switch_to.default_content()
                
                # Check for Iframes — score by area + same-origin bonus
                current_url = self.driver.current_url
                current_host = urlparse(current_url).netloc
                iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
                scored = []
                for f in iframes:
                    if not f.is_displayed():
                        continue
                    r = f.rect
                    area = (r.get("width") or 0) * (r.get("height") or 0)
                    src = f.get_attribute("src") or ""
                    src_host = urlparse(src).netloc
                    host_bonus = 2 if (src and (src.startswith("/") or src_host.endswith(current_host))) else 0
                    scored.append((area + host_bonus * 100000, f))
                scored.sort(key=lambda t: t[0], reverse=True)
                if scored:
                    self.driver.switch_to.frame(scored[0][1])
                else:
                    body = self.driver.find_element(By.TAG_NAME, "body")
                current_text = body.text.strip()
                current_fingerprint = self.get_page_fingerprint()
                
                # Stuck check: If we have been on the same fingerprint for more than 45 seconds
                if current_fingerprint == last_fingerprint and last_fingerprint != "" and not self.is_paused:
                    elapsed = time.time() - fingerprint_since
                    if elapsed > 45.0 and not screenshot_taken_for_current:
                        logger.warning(f"\n[⚠️ STUCK DETECTED] Bot has been on the same question for {int(elapsed)} seconds!")
                        screenshot_taken_for_current = True
                        try:
                            screenshot_name = f"screenshots/stuck_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                            self.driver.save_screenshot(screenshot_name)
                            logger.warning(f"[📷 SCREENSHOT SAVED] Captured state to: {screenshot_name}")
                            
                            # --- AI VISION STUCK SOLVER ---
                            logger.info("[*] Sending screenshot to AI Vision for autonomous stuck resolution...")
                            try:
                                with open(screenshot_name, "rb") as image_file:
                                    img_b64 = base64.b64encode(image_file.read()).decode('utf-8')
                                
                                vision_prompt = "I am stuck on this survey page. What do I need to click or type to proceed?\n\nCRITICAL: You MUST respond ONLY with an [EXECUTION] block. Do not provide analysis or reasoning. For coordinates, explicitly write X: <val>, Y: <val>.\n\nFormat:\n[EXECUTION]\nClick: X:..., Y:...\nType: <field> -> <value>"
                                vision_resp = self.ai_client.chat.completions.create(
                                    model=self.model_name,
                                    messages=[
                                        {
                                            "role": "user",
                                            "content": [
                                                {"type": "text", "text": vision_prompt},
                                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
                                            ]
                                        }
                                    ],
                                    max_tokens=300
                                )
                                vision_ans = vision_resp.choices[0].message.content.strip()
                                logger.info(f"\n[🤖 VISION AI SUGGESTS]:\n{vision_ans}\n")
                                
                                options_elements = self.driver.find_elements(By.CSS_SELECTOR, "label, .survey-qualification-answer-multi, .survey-qualification-answer-single, .answer-option, button, input[type='submit']")
                                self.execute_autonomous_action(vision_ans, options_elements)
                                last_fingerprint = "" # Force rescan
                                fingerprint_since = time.time()
                            except Exception as vision_err:
                                logger.error(f"[-] Vision AI failed: {vision_err}")
                                
                        except Exception as screenshot_err:
                            logger.error(f"[-] Failed to take screenshot: {screenshot_err}")
                        
                        # Print manual action guidance
                        logger.warning("\n--- 🛠️ MANUAL INTERVENTION REQUIRED ---")
                        logger.warning(f"CURRENT QUESTION TEXT:\n{current_text[:800]}\n")
                        
                        inputs_found = self.driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input[type='number'], input[type='email'], textarea")
                        visible_inputs = [el for el in inputs_found if el.is_displayed()]
                        if visible_inputs:
                            logger.warning(f"[ℹ️ Info] Found {len(visible_inputs)} visible text inputs on screen.")
                        
                        options_found = self.driver.find_elements(By.CSS_SELECTOR, "label, .survey-qualification-answer-multi, .answer-option, button")
                        visible_options = [opt.text.strip() for opt in options_found if opt.is_displayed() and opt.text.strip()]
                        if visible_options:
                            logger.warning("AVAILABLE DETECTED OPTIONS (Manual Clicks):")
                            for idx, opt in enumerate(visible_options[:20], 1):
                                logger.warning(f"  {idx}. {opt}")
                        logger.warning("----------------------------------------\n")
                
                if current_text and current_fingerprint != last_fingerprint and len(current_text) > 10:
                    last_fingerprint = current_fingerprint
                    fingerprint_since = time.time()
                    screenshot_taken_for_current = False
                    
                    image_context = ""
                    try:
                        imgs = [i for i in self.driver.find_elements(By.CSS_SELECTOR, "img") if i.is_displayed()]
                        for img in imgs[:5]:
                            r = img.rect
                            if 50 < r.get("width", 0) < 600:
                                image_context = self.extract_text_from_image(img)
                                if image_context:
                                    break
                    except Exception:
                        pass
                    
                    options_elements = []
                    options_text_list = []
                    
                    all_possible_options = self.driver.find_elements(By.CSS_SELECTOR, "label, .survey-qualification-answer-multi, .survey-qualification-answer-single, .answer-option, button, input[type='radio'], input[type='checkbox']")
                    for el in all_possible_options:
                        if not el.is_displayed():
                            continue
                        txt = el.text.strip()
                        if not txt:
                            txt = el.get_attribute("aria-label") or ""
                        if not txt:
                            txt = el.get_attribute("title") or ""
                        if not txt and el.tag_name == "input":
                            try:
                                row = el.find_element(By.XPATH, "./ancestor::tr")
                                txt = row.text.strip() + " - " + (el.get_attribute("value") or "")
                            except Exception:
                                pass
                        
                        txt = txt.strip()
                        if txt:
                            raw_txt = txt
                            try:
                                rect = el.rect
                                cx = int(rect['x'] + rect['width']/2)
                                cy = int(rect['y'] + rect['height']/2)
                                coord_marker = f"X:{cx}, Y:{cy}"
                                txt = f"{txt} {coord_marker}"
                            except Exception:
                                pass
                            
                            options_elements.append(el)
                            if raw_txt not in options_text_list:
                                options_text_list.append(raw_txt)
                    
                    logger.info("\n" + "="*60)
                    if len(options_text_list) > 50:
                        logger.info(f"[*] Massive Dropdown Detected! ({len(options_text_list)} options). Engaging Sniper Method...")
                        self.human_reading_delay(current_text[:400])
                        keyword = self.ask_deepseek_advanced(current_text, image_context=image_context, is_sniper=True)
                        if keyword:
                            keyword = keyword.strip().strip("*'\"` ")
                        logger.info(f"[*] AI target keyword: '{keyword}'. Scanning options locally...")
                        
                        match_found = None
                        if keyword:
                            for opt in options_text_list:
                                if keyword.lower() in opt.lower():
                                    match_found = opt
                                    break
                        
                        if match_found:
                            ans = match_found
                        else:
                            logger.info("[!] Sniper missed. Falling back to full AI scan...")
                            ans = self.ask_deepseek_advanced(current_text, options_text_list=options_text_list, image_context=image_context)
                        
                        logger.info(f"\n[🤖 AI SUGGESTS CLICKING]:\n{ans}\n")
                        if ans:
                            self.memory_log.append(f"Q: {current_text[:150].replace(chr(10), ' ')}... -> A: {ans}")
                            save_answer(current_text.strip(), ans.strip())
                    else:
                        logger.info(f"[*] New Question Detected! ({len(options_text_list)} options) Asking AI...")
                        self.human_reading_delay(current_text[:400])
                        ans = self.ask_deepseek_advanced(current_text, options_text_list=options_text_list, image_context=image_context)
                        
                        if ans:
                            # Parse and print analysis block
                            analysis_block = "No analysis block provided."
                            if "[ANALYSIS]" in ans:
                                analysis_block = ans.split("[ANALYSIS]")[-1].split("[EXECUTION]")[0].strip()
                            else:
                                analysis_block = ans.strip()
                            logger.info(f"\n[🤖 AI ANALYSIS & STRATEGY]:\n{analysis_block}\n")
                            
                            self.memory_log.append(f"Q: {current_text[:150].replace(chr(10), ' ')}... -> A: {analysis_block[:150]}")
                            save_answer(current_text.strip(), ans.strip())
                            self.execute_autonomous_action(ans, options_elements)
                    logger.info("="*60 + "\n")
                    self.save_cookies()
                    
            except Exception as e:
                err_type = type(e).__name__
                if err_type not in ["StaleElementReferenceException", "NoSuchElementException", "NoSuchWindowException", "WebDriverException"]:
                    logger.debug(f"[-] HUD Loop error: {e}")
                pass

    def run(self, target_url):
        self.run_manual_hud(target_url)

def run_cli_mode():
    logger.info("==========================================")
    logger.info("   SentinelCore Survey Bot (CLI Mode)     ")
    logger.info("==========================================")
    
    API_KEY = os.getenv("API_KEY", "")
    BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:20128/v1")
    MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.5-flash")
    if not API_KEY:
        logger.error("[!] Set API_KEY in .env")
        return
    
    logger.info("[*] Spooling up stealth browser instance...")
    bot = SentinelSurveyBot(
        api_key=API_KEY,
        base_url=BASE_URL,
        model_name=MODEL_NAME,
        profile_name="mumbai_hr_executive_01", 
        sweatshop_mode=False
    )
    try:
        while True:
            url = input("\n[?] Paste survey URL here (or press ENTER to scan current page, or type 'exit' to quit): ")
            if url.lower() == 'exit':
                logger.info("[+] Shutting down farm. Good work today!")
                try:
                    bot.driver.quit()
                except Exception:
                    pass
                break
                
            if url.strip() == '':
                bot.run_manual_hud(None)
            else:
                bot.run(url.strip())
    except KeyboardInterrupt:
        logger.info("\n[+] SentinelCore Operation Halted by User.")

if __name__ == "__main__":
    import sys
    if "--cli" in sys.argv:
        run_cli_mode()
    else:
        try:
            logger.info("[*] Launching HUD GUI...")
            from gui import SentinelGUI
            app = SentinelGUI()
            app.mainloop()
        except Exception as e:
            logger.warning(f"[!] Could not launch GUI: {e}. Falling back to CLI mode...")
            run_cli_mode()
