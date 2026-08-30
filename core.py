import base64
import hashlib
import io
import json
import os
import random
import re
import time
from pathlib import Path
from typing import List, Optional, Literal
from urllib.parse import urlparse

from openai import OpenAI
from openai import APITimeoutError, APIConnectionError, APIStatusError
from pydantic import BaseModel, Field
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

# is_stuck() threshold — the log message used to say 40s while the code
# checked 35s; one constant, one number.
STUCK_THRESHOLD_SECONDS = 35

from answer_cache import AnswerCache
from participant_profile import get_persona
from panel_config import host_matches_hubs as is_survey_router_hub
from sentinel_heuristic import real_input_kind

import logging
logger = logging.getLogger(__name__)

# 30s to match the backend client; both sit under the 45s /decide budget
# the extension enforces. (Was 15s here, 8s in backend.py, 45s in the
# extension — three budgets, the tightest won.)
DECIDE_PROVIDER_TIMEOUT = 30.0

# ── survey-routing / panel login hubs ──────────────────────────────
# Domains come from panel_config.json, which the extension popup edits at
# runtime via POST /config/panel-hub. Read LIVE on every call so updates
# apply without a backend restart.
from panel_config import get_panel_hub_domains

class Action(BaseModel):
    action_type: Literal["click", "type", "select_option", "select_multi", "scroll", "next", "wait", "human_help"]
    element_id: Optional[int] = Field(None, description="data-bot-id from the element map")
    coordinates: Optional[tuple[int, int]] = Field(None, description="Fallback x,y if element_id fails")
    value: Optional[str] = Field(None, description="Text to type, or option text to select")
    reasoning: str = Field(..., description="Short reasoning for this action")

class SurveyDecision(BaseModel):
    page_summary: str
    question_type: Literal["single_choice", "multi_choice", "dropdown", "text", "grid", "mixed", "completion", "unknown"]
    confidence: float = Field(..., ge=0, le=1)
    actions: List[Action]
    memory_note: Optional[str] = None

class BrowserController:
    def __init__(self, headless: bool = False, slow_mo: int = 50, profile_dir: str = "profiles/default"):
        self.headless = headless
        self.slow_mo = slow_mo
        self.profile_dir = os.path.abspath(profile_dir)
        os.makedirs(self.profile_dir, exist_ok=True)
        self.driver = None
        self.disqualified = False

    def start(self):
        options = uc.ChromeOptions()
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-notifications")
        options.add_argument("--window-size=1280,800")
        options.add_argument("--disable-dev-shm-usage")

        try:
            self.driver = uc.Chrome(options=options, user_data_dir=self.profile_dir)
        except Exception:
            fallback = os.path.join(self.profile_dir, "_chrome_tmp")
            os.makedirs(fallback, exist_ok=True)
            opts = uc.ChromeOptions()
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_argument("--disable-infobars")
            opts.add_argument("--disable-notifications")
            opts.add_argument("--window-size=1280,800")
            opts.add_argument("--disable-dev-shm-usage")
            self.driver = uc.Chrome(options=opts, user_data_dir=fallback)

        self.driver.set_window_position(0, 0)
        self.driver.set_window_size(1280, 800)

        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        })

        self.driver.request_interceptor = self._intercept

    def _intercept(self, request):
        try:
            url = (request.url or "").lower()
            if any(x in url for x in ["disqualified", "screenout", "terminate", "quota_full"]):
                self.disqualified = True
        except Exception:
            logger.debug("swallowed exception in core.py", exc_info=True)

    def screenshot_b64(self, compress: bool = True) -> str:
        png = self.driver.get_screenshot_as_png()
        if compress:
            png = self._compress_screenshot(png)
        return base64.b64encode(png).decode()

    def _compress_screenshot(self, png_bytes: bytes, max_size_kb: int = 500) -> bytes:
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(png_bytes))
            if img.width > 1280:
                ratio = 1280 / img.width
                img = img.resize((1280, int(img.height * ratio)), Image.LANCZOS)
            for quality in [85, 70, 50, 35]:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality)
                if buf.tell() < max_size_kb * 1024:
                    return buf.getvalue()
            return buf.getvalue()
        except Exception:
            return png_bytes

    def get_element_map(self) -> List[dict]:
        return self.driver.execute_script("""() => {
            const elements = [];
            // Clear stale ids from the previous scan — on SPA-style surveys
            // the DOM partially persists and [data-bot-id='7'] would match
            // the first stale node, not the current question's.
            document.querySelectorAll('[data-bot-id]')
                .forEach(el => el.removeAttribute('data-bot-id'));
            document.querySelectorAll(
                'button, input, select, textarea, a, [role="button"], [role="radio"], [role="checkbox"], label, .answer-option, .survey-option'
            ).forEach((el, idx) => {
                if (el.offsetParent === null) return;
                const rect = el.getBoundingClientRect();
                if (rect.width < 5 || rect.height < 5) return;
                // Derive the semantic type from the associated control so a
                // <label><input type=radio></label> reads as a radio — same
                // rule content.js applies (shared element-map schema).
                const control = el.tagName === 'LABEL'
                    ? (el.querySelector('input, select, textarea') || el) : el;
                const role = el.getAttribute('role') || '';
                const semanticType = control.type ||
                    (role === 'radio' || role === 'checkbox' ? role : '');
                el.setAttribute('data-bot-id', idx);
                const text = (el.innerText || el.getAttribute('aria-label') ||
                             el.getAttribute('placeholder') || el.value || '').substring(0, 120);
                const entry = {
                    id: idx,
                    tag: el.tagName.toLowerCase(),
                    type: semanticType,
                    role: role,
                    name: control.getAttribute('name') || '',
                    text: text,
                    x: Math.round(rect.left + rect.width / 2),
                    y: Math.round(rect.top + rect.height / 2)
                };
                if (semanticType === 'radio' || semanticType === 'checkbox') {
                    entry.checked = 'checked' in control
                        ? !!control.checked
                        : control.getAttribute('aria-checked') === 'true';
                } else if (el.tagName === 'SELECT') {
                    entry.value = el.value || '';
                    entry.options = [...el.options].map(option => ({
                        value: option.value,
                        text: option.text.trim(),
                        disabled: option.disabled
                    }));
                } else if ('value' in control) {
                    entry.value = String(control.value || '').slice(0, 200);
                }
                elements.push(entry);
            });
            return elements;
        }""")

    def get_page_text(self) -> str:
        return self.driver.find_element(By.TAG_NAME, "body").text

    def get_url(self) -> str:
        return self.driver.current_url

    def click_element(self, element_id: int):
        sel = f"[data-bot-id='{element_id}']"
        el = self.driver.find_element(By.CSS_SELECTOR, sel)
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        el.click()

    def click_coords(self, x: int, y: int):
        # Absolute viewport click. move_by_offset() is relative to the
        # CURRENT pointer position, so bare offset clicks drifted
        # cumulatively on every fallback click.
        body = self.driver.find_element(By.TAG_NAME, "body")
        ActionChains(self.driver).move_to_element_with_offset(body, x, y).click().perform()

    def type_into(self, element_id: int, text: str, human_like: bool = True):
        sel = f"[data-bot-id='{element_id}']"
        el = self.driver.find_element(By.CSS_SELECTOR, sel)
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        el.clear()
        if human_like:
            for ch in text:
                el.send_keys(ch)
                time.sleep(random.randint(30, 120) / 1000)
        else:
            el.send_keys(text)

    def scroll_random(self):
        self.driver.execute_script(f"window.scrollBy(0, {random.randint(200, 600)});")

    def detect_captcha(self) -> Optional[str]:
        indicators = [
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "iframe[src*='turnstile']",
            ".g-recaptcha",
            "#recaptcha",
            ".h-captcha",
        ]
        for sel in indicators:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if els and els[0].is_displayed():
                    return sel
            except Exception:
                continue
        try:
            body = self.driver.find_element(By.TAG_NAME, "body").text.lower()
            if "i'm not a robot" in body or "verify you are human" in body:
                return "text"
        except Exception:
            logger.debug("swallowed exception in core.py", exc_info=True)
        return None

    def handle_captcha(self):
        captcha_type = self.detect_captcha()
        if captcha_type:
            print(f"\n[!] CAPTCHA detected: {captcha_type}")
            input("Solve the CAPTCHA manually, then press ENTER...")
            return True
        return False

    def click_next(self) -> bool:
        # Pass 1: JS text match — :contains() is jQuery and raises
        # InvalidSelectorException in Selenium, so the old first three
        # selectors never ran.
        try:
            clicked = self.driver.execute_script("""
                const re = /\\b(next|continue|submit|send|finish)\\b/i;
                const cands = Array.from(document.querySelectorAll(
                    'button, input[type=submit], input[type=button], [role=button], a[href]'));
                for (const el of cands) {
                    if (!el.offsetParent || el.disabled) continue;
                    const t = (el.innerText || el.value ||
                               el.getAttribute('aria-label') || '');
                    if (re.test(t)) {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return true;
                    }
                }
                return false;
            """)
            if clicked:
                return True
        except Exception:
            logger.debug("JS next-button pass failed", exc_info=True)
        # Pass 2: the attribute-substring selectors that always worked.
        for sel in ("[id*='next' i]", "[class*='next' i]", "[class*='continue' i]",
                    "input[type='submit']"):
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if el.is_displayed() and el.is_enabled():
                        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        el.click()
                        return True
            except Exception:
                logger.debug("CSS next-selector %s failed", sel, exc_info=True)
        return False

    def goto(self, url: str):
        self.driver.get(url)

    def is_disqualified(self) -> bool:
        url = (self.driver.current_url or "").lower()
        try:
            text = self.driver.find_element(By.TAG_NAME, "body").text.lower()[:2000]
        except Exception:
            text = ""
        flags = ["disqualified", "screenout", "not qualify", "quota full", "reward=0", "terminated"]
        return any(f in url or f in text for f in flags) or is_survey_router_hub(url)

    def is_completion(self) -> bool:
        try:
            text = self.driver.find_element(By.TAG_NAME, "body").text.lower()[:1500]
        except Exception:
            text = ""
        return any(x in text for x in ["thank you", "completed", "finished", "success", "your responses have been recorded"])

    def save_session(self):
        pass

    def load_session(self):
        pass

    def stop(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                logger.debug("swallowed exception in core.py", exc_info=True)

class AIEngine:
    COMMON_ANSWERS = {
        "age": "32",
        "gender": "Female",
        "postal code": "400001",
        "zip": "400001",
        "pincode": "400001",
        "income": "₹12,00,000",
        "education": "Graduate",
        "employment": "Full-time",
        "industry": "Information Technology",
        "decision maker": "Yes",
    }

    def __init__(self, api_key: str, base_url: str, model: str):
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key or "not-needed",
            timeout=DECIDE_PROVIDER_TIMEOUT,
            max_retries=1,
        )
        self.model = model
        self.memory: List[str] = []
        self.learned_rules: List[str] = []

    def try_heuristic(self, question_text: str, options: List[str], elements: List[dict] = None) -> Optional[List[Action]]:
        """Deterministic fast path, element-based.

        Every element_id emitted is a real data-bot-id from ``elements``.
        (The retired version indexed a list of option *texts* — filtered in
        _run_survey_loop, unfiltered in decide() — so its clicks landed on
        whatever element happened to sit at that index.)
        """
        elements = elements or []
        text_lower = question_text.lower()

        # Industry screener: pick "none of the above" when offered.
        if (re.search(r"\bwork in\b", text_lower) and
                any(w in text_lower for w in ["marketing", "advertising", "market research"])) or \
           any(w in text_lower for w in ["do you work in any of the following",
                                         "work in the following industries"]):
            for e in elements:
                kind = real_input_kind(e)
                if kind in ("radio", "checkbox", "label") and \
                        "none" in (e.get("text") or "").lower():
                    return [Action(action_type="click", element_id=e["id"],
                                   reasoning="heuristic: industry screener -> none")]
            return None

        # "Select all that apply" — check every unchecked non-"none" box.
        if any(w in text_lower for w in ["select all that apply",
                                         "which of the following",
                                         "have you purchased"]):
            actions = []
            for e in elements:
                if real_input_kind(e) != "checkbox" or e.get("checked"):
                    continue
                opt = (e.get("text") or "").lower()
                if any(bad in opt for bad in ["none", "don't know",
                                              "prefer not", "not applicable"]):
                    continue
                actions.append(Action(action_type="click", element_id=e["id"],
                                      reasoning="select-all heuristic"))
            if actions:
                actions.append(Action(action_type="next",
                                      reasoning="proceed after select-all"))
                return actions
            return None

        # Keyword-matched text input.
        for keyword, answer in self.COMMON_ANSWERS.items():
            if keyword in text_lower:
                target = next((e for e in elements
                               if real_input_kind(e) in
                               {"text", "email", "tel", "number", "date", "editable"}
                               and not (e.get("value") or "").strip()), None)
                if target is not None:
                    return [Action(action_type="type", element_id=target["id"],
                                   value=answer, reasoning=f"heuristic: {keyword}")]
        return None

    def decide(self, screenshot_b64: str, elements: List[dict], url: str, page_text: str) -> Optional[SurveyDecision]:
        memory_block = "\n".join(self.memory[-12:]) if self.memory else "None yet."
        rules_block = "\n".join(f"- {r}" for r in self.learned_rules) if self.learned_rules else "None yet."

        prompt_text = f"""Analyze the survey screenshot and element map. Decide the next action(s).

URL: {url}
Page text excerpt: {page_text[:2500]}

Interactive elements (id, tag, type, text, center coordinates):
{json.dumps(elements[:40], indent=2)}

Memory of previous Q&A:
{memory_block}

Learned rules from past disqualifications:
{rules_block}

Instructions:
- If this is a text question, use action_type "type" with the element_id and value.
- If single choice, use "click" on the correct option's element_id.
- If multi-select, use multiple "click" actions or one "select_multi".
- If you see a Next/Continue/Submit button and have answered, include a "next" action.
- If the page seems to be a "Thank You" or completion screen, set question_type to "completion".
- If stuck or confused, use "human_help".
- Provide coordinates fallback for critical clicks.
- Keep memory_note to record what question was just answered for consistency."""

        try:
            resp = self.client.chat.completions.parse(   # .parse left .beta in openai >= 1.40
                model=self.model,
                messages=[
                    {"role": "system", "content": get_persona()},
                    {"role": "user", "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}}
                    ]}
                ],
                response_format=SurveyDecision,
                max_tokens=2500,
                temperature=0.2,
            )
            return resp.choices[0].message.parsed

        except APITimeoutError:
            logger.warning("provider timed out after %.0fs -> heuristic fallback",
                           DECIDE_PROVIDER_TIMEOUT)
        except APIConnectionError as e:
            logger.warning("provider unreachable (%s) -> heuristic fallback", e)
        except APIStatusError as e:
            logger.warning("provider error %s: %s -> heuristic fallback",
                           e.status_code, str(e)[:200])
        except Exception:
            # Was `pass` — this is exactly where the get_persona
            # TypeError and the schema concatenation TypeError lived,
            # undetected, since day one.
            logger.warning("structured LLM call failed", exc_info=True)

        # Fallback: try JSON-mode create()
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": get_persona()},
                    {"role": "user", "content": [
                         # model_json_schema() returns a DICT — str + dict
                         # raised TypeError, silently swallowed below
                         {"type": "text", "text": prompt_text + "\n\nYou MUST respond with valid JSON matching this schema:\n" + json.dumps(SurveyDecision.model_json_schema())},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}}
                    ]}
                ],
                max_tokens=2500,
                temperature=0.2,
            )
            raw = resp.choices[0].message.content.strip()
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0]
            return SurveyDecision.model_validate_json(raw)

        except APITimeoutError:
            logger.warning("provider timed out after %.0fs -> heuristic fallback",
                           DECIDE_PROVIDER_TIMEOUT)
        except APIConnectionError as e:
            logger.warning("provider unreachable (%s) -> heuristic fallback", e)
        except APIStatusError as e:
            logger.warning("provider error %s: %s -> heuristic fallback",
                           e.status_code, str(e)[:200])
        except Exception:
            logger.warning("raw-fallback LLM call failed", exc_info=True)

        # Final fallback: heuristic
        heuristic_actions = self.try_heuristic(page_text, [e.get("text", "") for e in elements], elements)
        if heuristic_actions:
            print(f"[heuristic] Fallback for: {page_text[:60]}...")
            return SurveyDecision(
                page_summary="heuristic",
                question_type="unknown",
                confidence=1.0,
                actions=heuristic_actions,
                memory_note="heuristic answer"
            )
        return None

    def learn_from_disqualification(self, memory: List[str]):
        if not memory:
            return
        recent = "\n".join(memory[-6:])
        prompt = f"""I got disqualified after these answers:\n{recent}\n\nWhat 1-sentence rule should I add to avoid this? Start with NEVER or ALWAYS. Reply with ONLY the rule."""
        try:
            resp = self.client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": prompt}],
                max_tokens=100, temperature=0.3
            )
            rule = resp.choices[0].message.content.strip()
            if rule:
                self.learned_rules.append(rule)
        except APITimeoutError:
            logger.warning("provider timed out after %.0fs -> learn_from_disqualification fallback",
                           DECIDE_PROVIDER_TIMEOUT)
        except APIConnectionError as e:
            logger.warning("provider unreachable (%s) -> learn_from_disqualification fallback", e)
        except APIStatusError as e:
            logger.warning("provider error %s: %s -> learn_from_disqualification fallback",
                           e.status_code, str(e)[:200])
        except Exception:
            logger.warning("learn_from_disqualification LLM call failed", exc_info=True)

class SurveyBot:
    def __init__(self, api_key: str, base_url: str, model: str, headless: bool = False, profile_name: str = "default"):
        self.browser = BrowserController(headless=headless, profile_dir=f"profiles/{profile_name}")
        self.ai = AIEngine(api_key, base_url, model)
        self.cache = AnswerCache(
            path=str(Path(__file__).resolve().parent / "cache" / f"{profile_name}_answers.json")
        )
        self.stuck_fingerprint: Optional[str] = None
        self.stuck_since: float = 0
        self.screenshot_counter = 0
        self.last_action_fingerprint: Optional[str] = None
        self.profile_name = profile_name

    def _page_fingerprint(self) -> str:
        text = self.browser.get_page_text()[:3000]
        url = self.browser.get_url()
        return hashlib.md5(f"{url}::{text}".encode()).hexdigest()

    def _visual_fingerprint(self) -> Optional[str]:
        try:
            import imagehash
            from PIL import Image
            png_bytes = self.browser.driver.get_screenshot_as_png()
            img = Image.open(io.BytesIO(png_bytes))
            # phash @ 16 (256 bits). average_hash @ 8 (64 bits) is coarse
            # enough that toggling a radio hashes identically — stuck
            # detection fired on pages that were visibly changing.
            return str(imagehash.phash(img, hash_size=16))
        except Exception:
            logger.debug("visual fingerprint failed", exc_info=True)
            return None

    def is_stuck(self) -> bool:
        visual_hash = self._visual_fingerprint()
        now = time.time()
        if visual_hash and visual_hash == self.stuck_fingerprint:
            if self.stuck_since == 0:
                self.stuck_since = now
            return (now - self.stuck_since) > STUCK_THRESHOLD_SECONDS
        else:
            self.stuck_fingerprint = visual_hash
            self.stuck_since = 0
            return False

    def _handle_stuck(self):
        print(f"[!] Page hasn't changed in {STUCK_THRESHOLD_SECONDS}s — taking debug screenshot")
        try:
            self.browser.driver.save_screenshot(f"debug_stuck_{self.screenshot_counter}.png")
        except Exception:
            logger.debug("debug screenshot failed", exc_info=True)
        self.screenshot_counter += 1
        if self.browser.click_next():
            print("[+] Emergency Next clicked")
        # Re-arm. The threshold was already exceeded when we got here, so
        # without this reset the handler re-fired on EVERY loop iteration —
        # that is why the repo carried 11 byte-identical debug_stuck_*.png.
        self.stuck_fingerprint = None
        self.stuck_since = 0

    def _verify_action(self, pre_fingerprint: str) -> bool:
        time.sleep(0.8)
        post_fingerprint = self._page_fingerprint()
        if post_fingerprint == pre_fingerprint:
            print("[!] Action had no effect — retrying with coordinates")
            return False
        return True

    def run(self, url: str):
        self.browser.start()
        self.browser.goto(url)
        print(f"[+] Loaded: {url}")
        self._run_survey_loop()

    def run_interactive(self):
        self.browser.start()
        print("[+] Browser opened. Navigate to the survey, then press F12 to start.")
        print("    Press Ctrl+C in this terminal to stop.")

        started = False
        while True:
            time.sleep(0.5)
            try:
                driver = self.browser.driver
                if driver and not started:
                    url = driver.current_url
                    if not url or url == "about:blank":
                        continue

                    driver.execute_script("""
                        if (!window.__sentinelKeyListenerInstalled) {
                            window.__sentinelKeyListenerInstalled = true;
                            window.__sentinelStartKey = null;
                            document.addEventListener('keydown', function(e) {
                                window.__sentinelStartKey = e.key;
                            }, true);
                        }
                    """)
                    last_key = driver.execute_script("return window.__sentinelStartKey;")
                    if last_key == "F12":
                        started = True
                        print(f"[+] F12 detected on {url} — starting survey loop")
                        self._run_survey_loop()
                        started = False
                        driver.execute_script("window.__sentinelStartKey = null;")
                        print("[+] Survey loop ended. Navigate to another page and press F12 again.")
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.debug("run_interactive poll iteration failed", exc_info=True)

    def _run_survey_loop(self):
        while True:
            time.sleep(1.5)

            if self.browser.disqualified or self.browser.is_disqualified():
                print("[!] DISQUALIFIED")
                self.ai.learn_from_disqualification(self.ai.memory)
                break

            if self.browser.is_completion():
                print("[+] SURVEY COMPLETED")
                break

            if self.browser.handle_captcha():
                continue

            if self.is_stuck():
                self._handle_stuck()
                continue

            screenshot = self.browser.screenshot_b64()
            elements = self.browser.get_element_map()
            page_text = self.browser.get_page_text()
            current_url = self.browser.get_url()

            options_texts = [e.get("text", "") for e in elements if e.get("text")]

            cached = self.cache.get(page_text[:500], options_texts)
            if cached:
                print(f"[cache] Cached answer found: {cached[:120]}")
                page_text += f"\n\n[CONTEXT: You previously answered this question with: {cached}. Use the same answer.]"
                decision = self.ai.decide(screenshot, elements, current_url, page_text)
            else:
                heuristic_actions = self.ai.try_heuristic(page_text, options_texts, elements)
                if heuristic_actions:
                    print(f"[heuristic] Fast path for: {page_text[:60]}...")
                    decision = SurveyDecision(
                        page_summary="heuristic",
                        question_type="unknown",
                        confidence=1.0,
                        actions=heuristic_actions,
                        memory_note="heuristic answer"
                    )
                else:
                    decision = self.ai.decide(screenshot, elements, current_url, page_text)

            if not decision:
                print("[-] AI returned nothing, retrying...")
                continue

            print(f"\n[AI] {decision.question_type} | confidence: {decision.confidence}")
            print(f"     Summary: {decision.page_summary[:120]}")

            if decision.question_type == "completion":
                print("[+] Completion detected by AI")
                break

            pre_fp = self._page_fingerprint()
            for act in decision.actions:
                print(f"     -> {act.action_type}: {act.reasoning[:80]}")
                pre_fp = self._page_fingerprint()  # FRESH before each action
                try:
                    if act.action_type == "click":
                        if act.element_id is not None:
                            self.browser.click_element(act.element_id)
                        elif act.coordinates:
                            self.browser.click_coords(*act.coordinates)
                    elif act.action_type == "type":
                        if act.element_id is not None and act.value:
                            self.browser.type_into(act.element_id, act.value)
                    elif act.action_type == "select_multi":
                        if act.value:
                            for part in act.value.split(","):
                                part = part.strip()
                                if part.isdigit():
                                    self.browser.click_element(int(part))
                                else:
                                    for el in elements:
                                        if part.lower() in el["text"].lower():
                                            self.browser.click_element(el["id"])
                                            break
                    elif act.action_type == "scroll":
                        self.browser.scroll_random()
                    elif act.action_type == "next":
                        self.browser.click_next()
                    elif act.action_type == "wait":
                        time.sleep(2)
                    elif act.action_type == "human_help":
                        print("\n" + "="*50)
                        print("MANUAL HELP NEEDED")
                        print(f"URL: {current_url}")
                        print("="*50)
                        input("Press ENTER after manually fixing the page...")
                except Exception as e:
                    print(f"[-] Action failed: {e}")
                    if act.coordinates and act.action_type in ("click", "type"):
                        try:
                            self.browser.click_coords(*act.coordinates)
                        except Exception:
                            logger.debug("swallowed exception in core.py", exc_info=True)

                if act.action_type != "wait":
                    if not self._verify_action(pre_fp):
                        if act.coordinates and act.action_type == "click":
                            try:
                                self.browser.click_coords(*act.coordinates)
                            except Exception:
                                logger.debug("swallowed exception in core.py", exc_info=True)

            if not any(a.action_type == "next" for a in decision.actions):
                if decision.question_type in ("single_choice", "multi_choice", "text", "dropdown", "grid"):
                    time.sleep(0.5)
                    if self.browser.click_next():
                        print("     [+] Auto-clicked Next")

            if decision.memory_note:
                self.ai.memory.append(decision.memory_note)

            if decision and decision.memory_note and decision.question_type not in ("completion", "unknown"):
                self.cache.set(page_text[:500], options_texts, {
                    "memory_note": decision.memory_note,
                    "answer_summary": decision.page_summary[:200],
                    "question_type": decision.question_type,
                })

    def stop(self):
        self.browser.stop()
