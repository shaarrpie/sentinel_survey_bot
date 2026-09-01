import time
import random
import os
import io
import base64
import json
import logging
import datetime
import socket
import shutil
import re
import sys
from urllib.parse import urlparse
from dotenv import load_dotenv
load_dotenv()

# Cross-platform keyboard polling
if sys.platform == "win32":
    import msvcrt
else:
    import select
    import termios
    import tty

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
# Use rotating file handler: 5MB max per file, keep 3 backups
from logging.handlers import RotatingFileHandler
file_handler = RotatingFileHandler(
    log_filename, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8'
)
console_handler = logging.StreamHandler()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[file_handler, console_handler]
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
                logger.debug("swallowed exception in bot.py", exc_info=True)

    def _detach(self):
        if self._attached:
            try:
                self.cdp("Browser.disable", {})
            except Exception:
                logger.debug("swallowed exception in bot.py", exc_info=True)
            self._attached = False

    def _bezier(self, x0, y0, x1, y1, n=12):
        """Cubic Bezier with randomized control points for human-like curve."""
        dx = x1 - x0
        dy = y1 - y0
        # Random control points that create a natural curve
        # Place them perpendicular to the direct path for realistic arc
        offset = random.uniform(0.2, 0.5)
        perp_x = -dy * offset * random.choice([-1, 1])
        perp_y = dx * offset * random.choice([-1, 1])
        
        # Control points at 1/3 and 2/3 along path with perpendicular offset
        cx1 = x0 + dx / 3 + perp_x * random.uniform(0.5, 1.5)
        cy1 = y0 + dy / 3 + perp_y * random.uniform(0.5, 1.5)
        cx2 = x0 + 2 * dx / 3 + perp_x * random.uniform(0.5, 1.5)
        cy2 = y0 + 2 * dy / 3 + perp_y * random.uniform(0.5, 1.5)
        
        pts = []
        for i in range(n + 1):
            t = i / n
            # Cubic Bezier formula
            mt = 1 - t
            x = mt**3 * x0 + 3 * mt**2 * t * cx1 + 3 * mt * t**2 * cx2 + t**3 * x1
            y = mt**3 * y0 + 3 * mt**2 * t * cy1 + 3 * mt * t**2 * cy2 + t**3 * y1
            pts.append((int(x), int(y)))
        return pts

    def move(self, x, y, duration=0, origin="viewport", button="none"):
        if not self._attached:
            self._attach()
        sx, sy = self._mouse_input.x, self._mouse_input.y
        pts = self._bezier(sx, sy, x, y)
        per = duration / max(len(pts) - 1, 1)
        logger.info(f"    [🖱️ MOUSE MOVE] ({sx},{sy}) -> ({x},{y}) duration={duration}ms")
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
            logger.info(f"    [🖱️ CLICK] at ({tx},{ty}) [jitter from ({x},{y}), element {width}x{height}]")
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


def check_keypress():
    """Cross-platform non-blocking keypress check. Returns key char or None."""
    if sys.platform == "win32":
        if msvcrt.kbhit():
            return msvcrt.getch()
        return None
    else:
        # Unix/macOS: check stdin without blocking
        try:
            if select.select([sys.stdin], [], [], 0)[0]:
                return sys.stdin.read(1)
        except Exception:
            logger.debug("swallowed exception in bot.py", exc_info=True)
        return None


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
            options.add_argument("--disable-blink-features=AutomationControlled")
            
            if sweatshop_mode:
                logger.info("[!] Sweatshop Mode Activated. Browser will run invisibly off-screen.")
                options.add_argument("--window-position=-32000,-32000")
            else:
                options.add_argument("--window-position=0,0")

            self.driver = uc.Chrome(options=options, user_data_dir=user_data_dir)
        self.actions = ActionChains(self.driver)
        self.mouse = MouseController(self.driver)

        # ── Comprehensive anti-detection script injected on every new document ──
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": self._stealth_script()
        })

    @staticmethod
    def _stealth_script() -> str:
        """Comprehensive anti-detection script injected on every new document.

        Patches navigator.plugins, navigator.languages, window.chrome,
        Permissions.prototype.query, and WebGL vendor/renderer to appear
        as a real Chrome browser. Without these, advanced bot detection
        can identify automation even when navigator.webdriver is hidden.
        """
        return """
        // ── navigator.webdriver ──────────────────────────────────────────
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

        // ── navigator.plugins ────────────────────────────────────────────
        const plugins = [
            { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Plugin', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
        ];
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const arr = [...plugins];
                arr.__proto__ = PluginArray.prototype;
                return arr;
            }
        });

        // ── navigator.languages ──────────────────────────────────────────
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en']
        });
        Object.defineProperty(navigator, 'language', {
            get: () => 'en-US'
        });

        // ── window.chrome ────────────────────────────────────────────────
        window.chrome = window.chrome || {};
        if (!window.chrome.runtime) {
            window.chrome.runtime = { PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' }, OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' }, OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' }, Arch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' } };
        }

        // ── Permissions.prototype.query ──────────────────────────────────
        const originalQuery = window.Permissions?.prototype?.query;
        if (originalQuery) {
            window.Permissions.prototype.query = function(params) {
                if (params.name === 'notifications') {
                    return Promise.resolve({ state: Notification.permission });
                }
                return originalQuery.call(this, params);
            };
        }

        // ── WebGL vendor/renderer ────────────────────────────────────────
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {
            if (param === 0x9245) return 'Google Inc. (NVIDIA)';
            if (param === 0x9246) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
            return getParameter.call(this, param);
        };
        if (window.WebGL2RenderingContext) {
            const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(param) {
                if (param === 0x9245) return 'Google Inc. (NVIDIA)';
                if (param === 0x9246) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return getParameter2.call(this, param);
            };
        }
        """

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
                except Exception as e:
                    logger.debug(f"[-] Failed to load existing cookies: {e}")
            
            cookie_dict = {f"{c['name']}_{c.get('domain', '')}": c for c in existing_cookies}
            for c in cookies:
                cookie_dict[f"{c['name']}_{c.get('domain', '')}": c]
                
            with open(cookie_file, 'w') as f:
                json.dump(list(cookie_dict.values()), f)
        except Exception as e:
            logger.debug(f"[-] Failed to save cookies: {e}")

    def load_cookies(self):
        """Injects cookies for the current domain."""
        try:
            cookie_file = os.path.join(self.profile_mgr.get_profile_path(self.profile_name), "cookies.json")
            if os.path.exists(cookie_file):
                with open(cookie_file, 'r') as f:
                    cookies = json.load(f)
                
                # Get current page domain for validation
                current_url = self.driver.current_url
                current_domain = urlparse(current_url).netloc
                # Strip port for comparison
                current_domain = current_domain.split(":")[0]
                
                count = 0
                skipped = 0
                for cookie in cookies:
                    try:
                        cookie_domain = cookie.get("domain", "").lstrip(".")
                        # Validate cookie domain matches current page
                        if cookie_domain and current_domain and \
                           not (current_domain == cookie_domain or 
                                current_domain.endswith("." + cookie_domain)):
                            skipped += 1
                            continue
                        self.driver.add_cookie(cookie)
                        count += 1
                    except Exception:
                        logger.debug("swallowed exception in bot.py", exc_info=True)
                if count > 0:
                    logger.info(f"[🍪 COOKIES] Injected {count} saved cookies (skipped {skipped} domain mismatches). Refreshing page...")
                    self.driver.refresh()
                    logger.info(f"    [🔄 REFRESH] Page reloaded at {self.driver.current_url}")
                elif skipped > 0:
                    logger.info(f"[🍪 COOKIES] Skipped {skipped} cookies - domain mismatch with current page ({current_domain})")
        except Exception as e:
            logger.debug(f"[-] Cookie load error: {e}")

    def get_page_fingerprint(self):
        """Generates a structural signature of the page state.

        Uses DOM structure hash (tag names, name/for attributes, option values)
        while excluding text content, timers, and ads. This catches actual
        question changes while ignoring dynamic fluff like timer updates.
        Falls back to text-based fingerprint if structural hash fails.
        """
        try:
            structural = self.driver.execute_script("""
                const container = document.querySelector(
                    '[role="main"], .question-container, .survey-question, ' +
                    '.quiz-question, [data-question], #question, ' +
                    '.form-group, fieldset'
                ) || document.body;

                const parts = [];
                const walker = document.createTreeWalker(
                    container,
                    NodeFilter.SHOW_ELEMENT,
                    {
                        acceptNode: (node) => {
                            const tag = node.tagName.toLowerCase();
                            const cls = (node.className || '').toString();
                            const id = node.id || '';

                            if (['nav', 'footer', 'script', 'style', 'noscript'].includes(tag)) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            if (cls.match(/\\b(timer|countdown|clock|ad|advert|banner|social|share)\\b/i)) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            if (id.match(/\\b(timer|countdown|clock|ad|advert|banner)\\b/i)) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            if (node.offsetParent === null) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            return NodeFilter.FILTER_ACCEPT;
                        }
                    }
                );

                let node;
                while ((node = walker.nextNode()) !== null) {
                    const tag = node.tagName.toLowerCase();
                    const name = node.getAttribute('name') || '';
                    const forAttr = node.getAttribute('for') || '';
                    const role = node.getAttribute('role') || '';
                    const type = node.getAttribute('type') || '';
                    const inputType = node.type || '';

                    let token = tag;
                    if (name) token += '[name=' + name + ']';
                    if (forAttr) token += '[for=' + forAttr + ']';
                    if (role) token += '[role=' + role + ']';
                    if (type) token += '[type=' + type + ']';
                    if (inputType && inputType !== tag) token += '[input=' + inputType + ']';

                    if (tag === 'select') {
                        const opts = Array.from(node.options).map(o => o.value).join(',');
                        token += '{opts=' + opts + '}';
                    }

                    if (inputType === 'radio' || inputType === 'checkbox') {
                        token += '{checked=' + (node.checked ? 1 : 0) + '}';
                    }

                    parts.push(token);
                }

                return parts.join('|');
            """)
            if structural:
                import hashlib
                return hashlib.md5(structural.encode()).hexdigest()
        except Exception:
            logger.debug("structural fingerprint failed, falling back to text", exc_info=True)

        # Fallback to text-based fingerprint
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
        
        # Retry with exponential backoff
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.ai_client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.4,
                    max_tokens=200,
                    timeout=30
                )
                rule = clean_model_text(response.choices[0].message.content)
                
                if "</think>" in rule:
                    rule = rule.split("</think>")[-1].strip()
                
                self.add_learned_rule(rule)
                logger.info(f"[+] AI updated its own persona matrix. It will not fail this way again: {rule}\n")
                
                # Clear memory so we don't double-trigger if we stay on the page
                self.memory_log = []
                return
            except Exception as e:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"[-] Learn from DQ failed (attempt {attempt+1}/{max_retries}): {e}. Retrying in {wait:.1f}s...")
                if attempt < max_retries - 1:
                    time.sleep(wait)
                else:
                    logger.error(f"[-] Learn from DQ failed after {max_retries} attempts: {e}")

    def human_mouse_move(self, element):
        try:
            rect = element.rect
            cx = int(rect['x'] + rect['width']/2)
            cy = int(rect['y'] + rect['height']/2)
            logger.info(f"    [🖱️ HUMAN MOVE] to element at ({cx},{cy}) [{rect['width']:.0f}x{rect['height']:.0f}]")
            self.actions.move_to_element(element).perform()
            time.sleep(random.uniform(0.1, 0.4))
        except Exception:
            logger.debug("swallowed exception in bot.py", exc_info=True)

    def human_reading_delay(self, text):
        words = len(text.split())
        reading_time = words / 3.3
        jitter = random.uniform(1.0, 3.5)
        total_delay = min(reading_time + jitter, 10.0)
        logger.info(f"[*] Simulating human reading time: waiting {total_delay:.2f}s...")
        time.sleep(total_delay)

    def human_type(self, element, text):
        logger.info(f"    [⌨️ TYPE] '{text}' into element")
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

        # Retry with exponential backoff
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.ai_client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "system", "content": system_persona}, {"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=4096,
                    timeout=60
                )
                
                raw_answer = clean_model_text(response.choices[0].message.content)
                
                if "</think>" in raw_answer:
                    raw_answer = raw_answer.split("</think>")[-1].strip()

                return raw_answer
            except Exception as e:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"[-] AI call failed (attempt {attempt+1}/{max_retries}): {e}. Retrying in {wait:.1f}s...")
                if attempt < max_retries - 1:
                    time.sleep(wait)
                else:
                    logger.error(f"[-] AI call failed after {max_retries} attempts: {e}")
                    return None

    # ── Helper: strict label match (Fix 2) ───────────────────────────
    def _opt_label_matches(self, label_lower: str, opt_text_lower: str) -> bool:
        """Word-boundary / exact match for option text.

        The old ``label_lower in opt_text_lower`` substring match fired
        on any option that happened to contain the keyword as a
        fragment (e.g. label "No" matched "Nothing"). Require a
        word-boundary match or equality to avoid that.
        """
        if not label_lower or not opt_text_lower:
            return False
        if label_lower == opt_text_lower:
            return True
        if re.search(r"\b" + re.escape(label_lower) + r"\b", opt_text_lower):
            return True
        return False

    # ── Helper: re-harvest options at execution time (Fix 3) ─────────
    def _reharvest_options(self) -> list:
        """Re-scan for visible option-like elements right before click.

        The scan-phase snapshot can go stale between the AI scan and
        the click (SPAs, ad re-renders, late-loading widgets).
        """
        sels = ("label, .survey-qualification-answer-multi, "
                ".survey-qualification-answer-single, .answer-option, "
                "button, input[type='radio'], input[type='checkbox']")
        try:
            elements = self.driver.find_elements(By.CSS_SELECTOR, sels)
        except Exception:
            return []
        return [el for el in elements if el.is_displayed()]

    # ── Helper: detect and fill a "Other (please specify)" field (Fix 9) ──
    def _maybe_fill_other_specify(self, last_clicked_rect: dict) -> None:
        """After clicking an option, check if a previously-hidden text
        input appeared within 200px of the click. If so, fill it with
        a generic plausible answer.
        """
        if not last_clicked_rect:
            return
        try:
            lx = last_clicked_rect.get("x", 0) + last_clicked_rect.get("width", 0) / 2
            ly = last_clicked_rect.get("y", 0) + last_clicked_rect.get("height", 0) / 2
        except Exception:
            return
        try:
            inputs = self.driver.find_elements(
                By.CSS_SELECTOR,
                "input[type='text'], input:not([type]), textarea",
            )
        except Exception:
            return
        generic = "Prefer not to say"
        for inp in inputs:
            try:
                if not inp.is_displayed():
                    continue
                # Skip already-filled
                if (inp.get_attribute("value") or "").strip():
                    continue
                r = inp.rect
                ix = r.get("x", 0) + r.get("width", 0) / 2
                iy = r.get("y", 0) + r.get("height", 0) / 2
                # Only consider inputs near the click (200px)
                if ((ix - lx) ** 2 + (iy - ly) ** 2) ** 0.5 > 200:
                    continue
                # Heuristic: name/id/placeholder mentions "other" or "specify"
                ctx = " ".join(filter(None, [
                    inp.get_attribute("name") or "",
                    inp.get_attribute("id") or "",
                    inp.get_attribute("placeholder") or "",
                ])).lower()
                if "other" in ctx or "specify" in ctx or "please" in ctx:
                    inp.clear()
                    self.human_type(inp, generic)
                    logger.info("    [+] Filled 'Other (please specify)' with generic answer")
            except Exception:
                logger.debug("swallowed exception in bot.py", exc_info=True)

    # ── Helper: verify click state (Fix 4) ──────────────────────────
    def _verify_click(self, element, expected_kind: str) -> bool:
        """After clicking a radio/checkbox, confirm ``checked`` is set.
        For buttons, just return True (URL/text change happens later).
        """
        if expected_kind not in ("radio", "checkbox"):
            return True
        try:
            if element.tag_name.lower() == "input":
                return element.get_attribute("checked") == "true" or element.is_selected()
            # For <label>-wrapped inputs, also check the inner control
            inner = element.find_element(By.CSS_SELECTOR, "input") if False else None
            return element.is_selected() if hasattr(element, "is_selected") else True
        except Exception:
            return False

    # ── Helper: detect question type from elements ─────────────────────
    @staticmethod
    def detect_question_type(elements: list) -> str:
        """Detect question type from Selenium WebElement list.

        Analyzes the elements to determine if this is single-choice,
        multi-select, dropdown, text, or grid. Used to enforce correct
        execution behavior (e.g., only one click for single-choice).
        """
        radios = [e for e in elements if (e.get_attribute("type") or "").lower() == "radio"]
        checkboxes = [e for e in elements if (e.get_attribute("type") or "").lower() == "checkbox"]
        selects = [e for e in elements if e.tag_name.lower() == "select"]
        text_inputs = [e for e in elements if (e.get_attribute("type") or "").lower() in ("text", "number", "email", "tel", "date")]

        # Grid detection: many radios with row-like structure
        if len(radios) > 10:
            name_groups = {}
            for r in radios:
                name = r.get_attribute("name") or ""
                name_groups[name] = name_groups.get(name, 0) + 1
            multi_groups = [n for n, count in name_groups.items() if count > 1]
            if len(multi_groups) >= 2:
                return "grid"

        # Single choice: radios without checkboxes
        if len(radios) > 0 and len(checkboxes) == 0:
            return "single_choice"

        # Multi-select: checkboxes present
        if len(checkboxes) > 0:
            return "multi_select"

        # Dropdown: select elements
        if len(selects) > 0:
            return "dropdown"

        # Open-ended: text inputs without radios/checkboxes
        if len(text_inputs) > 0 and len(radios) == 0 and len(checkboxes) == 0:
            return "text"

        # Check for contenteditable elements
        editables = [e for e in elements if e.get_attribute("contenteditable") == "true"]
        if editables:
            return "text"

        return "unknown"

    # ── Helper: enforce question type constraints ──────────────────────
    @staticmethod
    def enforce_question_type(click_targets: list, question_type: str) -> list:
        """Enforce question type constraints on click targets.

        For single_choice: only execute the first click target.
        For dropdown: prioritize select_option over click.
        For grid: ensure all rows are answered.

        This prevents the AI from hallucinating multiple clicks for
        single-choice questions.
        """
        if question_type == "single_choice":
            # Only allow one click for single-choice
            if len(click_targets) > 1:
                return [click_targets[0]]
        return click_targets
        """For React/Vue/etc. that watch the value via the native input
        event: set ``.value`` and dispatch ``input`` and ``change``.
        Used for non-contenteditable inputs that need framework sync."""
        self.driver.execute_script("""
            const el = arguments[0];
            const value = arguments[1];
            const proto = Object.getPrototypeOf(el);
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')
                && Object.getOwnPropertyDescriptor(proto, 'value').set;
            if (setter) setter.call(el, value); else el.value = value;
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        """, element, value)

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
                    logger.debug("swallowed exception in bot.py", exc_info=True)
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
                            logger.debug("swallowed exception in bot.py", exc_info=True)
                    
                    parent_text = ""
                    try:
                        parent = inp.find_element(By.XPATH, "..")
                        parent_text = parent.text.lower()
                    except Exception:
                        logger.debug("swallowed exception in bot.py", exc_info=True)
                    
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
                    logger.info(f"    [📜 SCROLL] Element into view, center at ({cx},{cy})")
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
                    # Use JS value assignment + event dispatch for React/Vue/Angular
                    # compatibility. send_keys()/.clear() don't trigger onChange.
                    self._set_input_value_with_events(best_input, value)
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
                        logger.debug("swallowed exception in bot.py", exc_info=True)
                
                try:
                    rect = opt_el.rect
                    cx = int(rect['x'] + rect['width']/2)
                    cy = int(rect['y'] + rect['height']/2)
                    coord_marker = f"X:{cx}, Y:{cy}"
                except Exception:
                    coord_marker = ""
                
                opt_text = f"{opt_text} {coord_marker}".lower().strip()

                # Fix 2: word-boundary / exact match instead of naive
                # ``label_lower in opt_text_lower`` substring.
                if opt_text and (self._opt_label_matches(label_lower, opt_text) or
                                 (coord_marker and coord_marker.lower() in label_lower)):
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", opt_el)
                    logger.info(f"    [📜 SCROLL] Option into view: {opt_el.text.strip()[:40]}")
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
                    # Fix 4: verify the click actually toggled the
                    # radio/checkbox state. If not, retry once via JS
                    # .click() before giving up.
                    kind = "radio" if (opt_el.tag_name.lower() == "input"
                                       and (opt_el.get_attribute("type") or "").lower() == "radio") else (
                          "checkbox" if (opt_el.tag_name.lower() == "input"
                                         and (opt_el.get_attribute("type") or "").lower() == "checkbox") else "other")
                    if kind in ("radio", "checkbox") and not self._verify_click(opt_el, kind):
                        logger.warning("    [!] Click did not toggle state — retrying via JS .click()")
                        try:
                            self.driver.execute_script("arguments[0].click();", opt_el)
                        except Exception:
                            pass
                    # Fix 9: if a new text field appeared near the click
                    # (e.g. "Other (please specify)"), fill it.
                    try:
                        self._maybe_fill_other_specify(opt_el.rect)
                    except Exception:
                        pass
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
                
                # Validate coordinates are within viewport bounds
                if cx is not None and cy is not None:
                    try:
                        viewport = self.driver.execute_script("""
                            return {w: window.innerWidth, h: window.innerHeight};
                        """)
                        vp_w = viewport.get('w', 1280)
                        vp_h = viewport.get('h', 800)
                        if cx < 0 or cx > vp_w or cy < 0 or cy > vp_h:
                            logger.error(f"    [-] LLM coordinates X:{cx}, Y:{cy} out of viewport ({vp_w}x{vp_h}) - rejecting")
                            cx, cy = None
                    except Exception:
                        logger.debug("swallowed exception in bot.py", exc_info=True)
                
                if cx is not None and cy is not None:
                    # Fix 6: scroll-compensated click. The AI returns
                    # page coordinates (X,Y) which may be outside the
                    # current viewport. Scroll the point into view,
                    # then re-derive a viewport-local position before
                    # dispatching the click.
                    try:
                        self.driver.execute_script(
                            "window.scrollTo(0, arguments[1] - window.innerHeight/2);",
                            cx, cy,
                        )
                        time.sleep(0.15)
                        vp_x, vp_y = self.driver.execute_script(
                            "return [arguments[0] - window.pageXOffset, "
                            "arguments[1] - window.pageYOffset];",
                            cx, cy,
                        )
                    except Exception:
                        vp_x, vp_y = cx, cy
                    try:
                        self.mouse.click(int(vp_x), int(vp_y))
                        logger.info(f"    [+] Fallback: CDP clicked coords X:{int(vp_x)}, Y:{int(vp_y)} (page {cx},{cy})")
                        matched = True
                    except Exception:
                        try:
                            success = self.driver.execute_script(f"""
                                var el = document.elementFromPoint({int(vp_x)}, {int(vp_y)});
                                if(el) {{
                                    el.click();
                                    return true;
                                }}
                                return false;
                            """)
                            if success:
                                logger.info(f"    [+] Fallback: Clicked raw coordinates X:{int(vp_x)}, Y:{int(vp_y)}")
                                matched = True
                            else:
                                logger.error(f"    [-] Fallback failed: No element found at X:{int(vp_x)}, Y:{int(vp_y)}")
                        except Exception as e:
                            logger.error(f"    [-] Fallback JS error at X:{int(vp_x)}, Y:{int(vp_y)}: {e}")

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
            # Fix 3: the caller's options_elements may be stale (SPAs,
            # ad re-renders). Re-harvest fresh, visible elements right
            # before dispatch so the click hits a still-attached node.
            if not options_elements:
                options_elements = self._reharvest_options()
            for line in execution_lines:
                line_lower = line.lower()
                if line_lower.startswith("type:") and "->" in line:
                    parts = line.split(":", 1)[1].split("->", 1)
                    label_keyword = parts[0].strip()
                    val = parts[1].strip()
                    self.execute_type_action(label_keyword, val)
                elif line_lower.startswith("click:"):
                    label_keyword = line.split(":", 1)[1].strip()
                    # Fix 3: fresh re-harvest at click time
                    fresh = self._reharvest_options()
                    self.execute_click_action(label_keyword, fresh)
                else:
                    if "[EXECUTION]" in ans and line.strip() and len(line) < 150 and not line.startswith("[") and not line.startswith("*"):
                        fresh = self._reharvest_options()
                        self.execute_click_action(line.strip(), fresh)
                            
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
                    logger.info(f"    [➡️ NEXT BUTTON] Clicking 'Next' at ({cx},{cy}) - text: '{next_btn.text or next_btn.get_attribute('value')}'")
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
            logger.info(f"    [🌐 NAVIGATED] Now at: {self.driver.current_url}")
            logger.info(f"    [📄 PAGE TITLE] {self.driver.title}")
            self.load_cookies()
        logger.info("[+] SentinelCore Manual HUD Active. Monitoring screen for questions...")
        logger.info("[+] Hotkey Active: Press 'P' in this console at any time to PAUSE/RESUME the scanner.")
        
        last_fingerprint = ""
        fingerprint_since = time.time()
        screenshot_taken_for_current = False
        
        try:
            while not getattr(self, "gui_shutdown", False):
                time.sleep(1.5)
                
                # --- GUI / Hardware Pause Toggle ---
                key = check_keypress()
                if key is not None:
                    if isinstance(key, bytes):
                        key = key.decode('utf-8', errors='ignore')
                    if key.lower() == 'p':
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
                                logger.info(f"    [🗔 TAB SWITCH] Switched to new popup tab. Now at: {self.driver.current_url}")
                    except Exception:
                        logger.debug("swallowed exception in bot.py", exc_info=True)

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
                        frame_src = scored[0][1].get_attribute("src") or "unknown"
                        logger.info(f"    [🖼️ FRAME SWITCH] Entering iframe: {frame_src[:80]}")
                        self.driver.switch_to.frame(scored[0][1])
                    body = self.driver.find_element(By.TAG_NAME, "body")

                    current_url = self.driver.current_url
                    current_text = body.text.strip()
                    current_fingerprint = self.get_page_fingerprint()
                    
                    if current_url != getattr(self, '_last_logged_url', None):
                        self._last_logged_url = current_url
                        logger.info(f"    [🌐 URL CHANGE] Now at: {current_url}")
                    
                    # Stuck check: If we have been on the same fingerprint for more than 25 seconds
                    if current_fingerprint == last_fingerprint and last_fingerprint != "" and not self.is_paused:
                        elapsed = time.time() - fingerprint_since
                        if elapsed > 25.0 and not screenshot_taken_for_current:
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
                                
                                # Fix 8: better vision prompt with
                                # persona, recent memory, and the
                                # current question text so the model
                                # has context for the choice.
                                try:
                                    persona_snippet = get_persona()[:500]
                                except Exception:
                                    persona_snippet = "(persona unavailable)"
                                recent = "\n".join(self.memory_log[-3:]) if self.memory_log else "(no prior answers)"
                                vision_prompt = (
                                    "I am stuck on this survey page. "
                                    "Based on my persona and recent answers, "
                                    "decide what to click or type to proceed.\n\n"
                                    f"PERSONA:\n{persona_snippet}\n\n"
                                    f"RECENT ANSWERS:\n{recent}\n\n"
                                    f"CURRENT QUESTION:\n{current_text[:300]}\n\n"
                                    "Respond ONLY with an [EXECUTION] block. "
                                    "For coordinates, explicitly write X: <val>, Y: <val>.\n\n"
                                    "Format:\n"
                                    "[EXECUTION]\n"
                                    "Click: X:..., Y:...\n"
                                    "Type: <field> -> <value>"
                                )
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
                        logger.debug("swallowed exception in bot.py", exc_info=True)
                    
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
                                logger.debug("swallowed exception in bot.py", exc_info=True)
                        
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
                                logger.debug("swallowed exception in bot.py", exc_info=True)
                            
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
                            kw = keyword.lower()
                            for opt in options_text_list:
                                if self._opt_label_matches(kw, opt.lower()):
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
        except KeyboardInterrupt:
            logger.info("\n[!] Interrupted by user (Ctrl+C)")
        finally:
            # Graceful shutdown: save cookies and cleanup
            logger.info("[*] Shutting down gracefully...")
            try:
                self.save_cookies()
                logger.info("[+] Cookies saved.")
            except Exception as e:
                logger.debug(f"[-] Failed to save cookies during shutdown: {e}")
            try:
                if self.driver:
                    self.driver.quit()
                    logger.info("[+] Browser closed.")
            except Exception:
                logger.debug("swallowed exception in bot.py", exc_info=True)

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
                    logger.debug("swallowed exception in bot.py", exc_info=True)
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
