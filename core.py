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
from playwright.sync_api import sync_playwright, Page
from pydantic import BaseModel, Field

from answer_cache import AnswerCache

import logging
logger = logging.getLogger(__name__)

# ── survey-routing / panel login hubs ──────────────────────────────
# Domains come from panel_config.json, which the extension popup edits at
# runtime via POST /config/panel-hub. Read LIVE on every call so updates
# apply without a backend restart.
from panel_config import get_panel_hub_domains

def is_survey_router_hub(url: str) -> bool:
    try:
        u = (url or "").lower().strip()
        host = urlparse(u).netloc or u
    except Exception:
        host = (url or "").lower()
    if host.startswith("www."):
        host = host[4:]          # leading label only — matches hub_match.js
    host = host.split(":")[0]
    return any(host == d or host.endswith("." + d)
               for d in get_panel_hub_domains())

class Action(BaseModel):
    action_type: Literal["click", "type", "select_multi", "scroll", "next", "wait", "human_help"]
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
        self.profile_dir = profile_dir
        self._pw = None
        self._browser = None
        self._context = None
        self.page: Page = None

    def start(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1366,768",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ]
        )
        self._context = self._browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York",
            geolocation={"latitude": 40.7128, "longitude": -74.0060},
            permissions=["geolocation"],
        )
        self.page = self._context.new_page()
        self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        # Load cookies + storage AFTER page exists
        self._load_session()
        self._inject_cookies()
        self._install_storage_restore()
        
        # Network interception for early disqualification detection
        self.disqualified = False
        self.page.on("response", lambda response: self._check_response(response))

    def _check_response(self, response):
        try:
            url = response.url.lower()
            if (any(x in url for x in ["disqualified", "screenout", "terminate", "quota_full"])
                    or is_survey_router_hub(url)):
                self.disqualified = True
        except Exception as e:
            print(f"[!] Response check error: {e}")

    def _load_session(self):
        profile_dir = Path(self.profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        
        cookies_path = profile_dir / "cookies.json"
        storage_path = profile_dir / "storage.json"
        
        if cookies_path.exists():
            try:
                with open(cookies_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                self._pending_cookies = cookies
            except Exception:
                self._pending_cookies = None
        else:
            self._pending_cookies = None
        
        if storage_path.exists():
            try:
                with open(storage_path, "r", encoding="utf-8") as f:
                    storage = json.load(f)
                self._pending_storage = storage
            except Exception:
                self._pending_storage = None
        else:
            self._pending_storage = None

    def _install_storage_restore(self):
        """Storage is origin-scoped, so it must execute inside the
        real document — an init script runs before the page's own
        scripts on every navigation, including the first."""
        if not getattr(self, "_pending_storage", None):
            return
        payload = json.dumps(self._pending_storage)
        script = (
            "(() => {"
            f"  const data = {payload};"
            "  try {"
            "    if (data.local) for (const [k, v] of Object.entries(data.local))"
            "      localStorage.setItem(k, v);"
            "    if (data.session) for (const [k, v] of Object.entries(data.session))"
            "      sessionStorage.setItem(k, v);"
            "  } catch (e) {}"
            "})();"
        )
        self.page.add_init_script(script)

    def _inject_cookies(self):
        if hasattr(self, '_pending_cookies') and self._pending_cookies:
            try:
                self.page.context.add_cookies(self._pending_cookies)
            except Exception as e:
                print(f"[!] Failed to inject cookies: {e}")

    def screenshot_b64(self, compress: bool = True) -> str:
        png_bytes = self.page.screenshot(type="png")
        if compress:
            png_bytes = self._compress_screenshot(png_bytes)
        return base64.b64encode(png_bytes).decode()

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
        return self.page.evaluate("""() => {
            const elements = [];
            document.querySelectorAll(
                'button, input, select, textarea, a, [role="button"], [role="radio"], [role="checkbox"], label, .answer-option, .survey-option'
            ).forEach((el, idx) => {
                if (el.offsetParent === null) return;
                const rect = el.getBoundingClientRect();
                if (rect.width < 5 || rect.height < 5) return;
                el.setAttribute('data-bot-id', idx);
                const text = (el.innerText || el.getAttribute('aria-label') || 
                             el.getAttribute('placeholder') || el.value || '').substring(0, 120);
                elements.push({
                    id: idx,
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    text: text,
                    x: Math.round(rect.left + rect.width / 2),
                    y: Math.round(rect.top + rect.height / 2)
                });
            });
            return elements;
        }""")

    def get_page_text(self) -> str:
        return self.page.inner_text("body")

    def get_url(self) -> str:
        return self.page.url

    def click_element(self, element_id: int):
        sel = f"[data-bot-id='{element_id}']"
        self.page.locator(sel).scroll_into_view_if_needed()
        self.page.locator(sel).click()

    def click_coords(self, x: int, y: int):
        self.page.mouse.click(x, y)

    def type_into(self, element_id: int, text: str, human_like: bool = True):
        sel = f"[data-bot-id='{element_id}']"
        el = self.page.locator(sel)
        el.scroll_into_view_if_needed()
        el.fill("")
        if human_like:
            el.press_sequentially(text, delay=random.randint(30, 120))
        else:
            el.fill(text)

    def scroll_random(self):
        self.page.mouse.wheel(0, random.randint(200, 600))

    def detect_captcha(self) -> Optional[str]:
        indicators = [
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "iframe[src*='turnstile']",
            ".g-recaptcha",
            "#recaptcha",
            ".h-captcha",
            "text=I'm not a robot",
            "text=Verify you are human",
        ]
        for sel in indicators:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return sel
            except Exception:
                continue
        return None

    def handle_captcha(self):
        captcha_type = self.detect_captcha()
        if captcha_type:
            print(f"\n[!] CAPTCHA detected: {captcha_type}")
            input("Solve the CAPTCHA manually, then press ENTER...")
            return True
        return False

    def click_next(self) -> bool:
        selectors = [
            "button:has-text('Next')", "button:has-text('Continue')",
            "button:has-text('Submit')", "input[type='submit']",
            "[id*='Next' i]", "[class*='next' i]", "[class*='continue' i]",
            "button:has-text('>>')", "button:has-text('→')"
        ]
        for sel in selectors:
            try:
                el = self.page.locator(sel).first
                if el.is_visible(timeout=500):
                    el.scroll_into_view_if_needed()
                    el.click()
                    return True
            except Exception:
                continue
        return False

    def goto(self, url: str):
        self.page.goto(url, wait_until="domcontentloaded")

    def is_disqualified(self) -> bool:
        url = self.page.url.lower()
        text = self.page.inner_text("body").lower()[:2000]
        flags = ["disqualified", "screenout", "not qualify", "quota full", "reward=0", "terminated"]
        return any(f in url or f in text for f in flags) or is_survey_router_hub(url)

    def is_completion(self) -> bool:
        text = self.page.inner_text("body").lower()[:1500]
        return any(x in text for x in ["thank you", "completed", "finished", "success", "your responses have been recorded"])

    def save_session(self):
        profile_dir = Path(self.profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        
        cookies = self.page.context.cookies()
        with open(profile_dir / "cookies.json", "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2)
        
        storage = self.page.evaluate("""
            () => ({
                local: Object.fromEntries(Object.entries(localStorage)),
                session: Object.fromEntries(Object.entries(sessionStorage))
            })
        """)
        with open(profile_dir / "storage.json", "w", encoding="utf-8") as f:
            json.dump(storage, f, indent=2)

    def load_session(self):
        profile_dir = Path(self.profile_dir)
        cookies_path = profile_dir / "cookies.json"
        storage_path = profile_dir / "storage.json"
        
        if cookies_path.exists():
            try:
                with open(cookies_path, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                self.page.context.add_cookies(cookies)
            except Exception:
                pass
        
        if storage_path.exists():
            try:
                with open(storage_path, "r", encoding="utf-8") as f:
                    storage = json.load(f)
                self.page.evaluate("""
                    (data) => {
                        if (data.local) {
                            Object.entries(data.local).forEach(([k, v]) => localStorage.setItem(k, v));
                        }
                        if (data.session) {
                            Object.entries(data.session).forEach(([k, v]) => sessionStorage.setItem(k, v));
                        }
                    }
                """, storage)
            except Exception:
                pass

    def stop(self):
        if self._context:
            try:
                self.save_session()
            except Exception:
                pass
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass

class AIEngine:
    PERSONA = """You are a real human taking online surveys. Persona:
- Female, 32, Mumbai (Maharashtra), India, Pin 400001
- B.Tech/MBA, IT Director, household income ₹12L
- Married, 2 kids (4, 7), caregiver for elderly parent (diabetes, hypertension)
- 2 dogs, 2 cats, urban homeowner
- Primary decision-maker for FMCG, groceries, education, appliances

STRICT RULES:
1. NEVER say you work in Marketing/Advertising/Market Research. Pick "None of the above" or "Other: Engineering".
2. ALWAYS express extreme interest in products/topics asked about.
3. For multi-select purchase questions, select ALMOST ALL except obvious fake/trap items.
4. ALWAYS claim to be the sole/final decision-maker.
5. If the survey targets a niche (healthcare, gaming, auto), PIVOT your persona to match it perfectly.
6. Follow instruction traps literally (e.g., "Select option 3").
7. Never contradict previous answers. Use memory to stay consistent.
8. Keep answers plausible and internally consistent."""

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
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.memory: List[str] = []
        self.learned_rules: List[str] = []

    def try_heuristic(self, question_text: str, options: List[str], elements: List[dict] = None) -> Optional[List[Action]]:
        text_lower = question_text.lower()
        
        for keyword, answer in self.COMMON_ANSWERS.items():
            if keyword in text_lower:
                target_id = 0
                if elements:
                    target = next((e for e in elements if e.get("tag") in ("input", "textarea") and e.get("type", "") in ("", "text", "number", "email", "tel")), None)
                    if target:
                        target_id = target["id"]
                return [Action(action_type="type", element_id=target_id, value=answer, reasoning=f"heuristic: {keyword}")]
        
        # Multi-select "select all" heuristic
        if any(w in text_lower for w in ["select all that apply", "which of the following", "have you purchased"]):
            actions = []
            for i, opt in enumerate(options):
                opt_lower = opt.lower()
                if any(bad in opt_lower for bad in ["none", "don't know", "prefer not", "not applicable"]):
                    continue
                actions.append(Action(action_type="click", element_id=i, reasoning="select-all heuristic"))
            if actions:
                actions.append(Action(action_type="next", reasoning="proceed after select-all"))
                return actions
        
        # Industry trap
        if (re.search(r"\bwork in\b", text_lower) and any(w in text_lower for w in ["marketing", "advertising", "market research"])) or \
           any(w in text_lower for w in ["do you work in any of the following", "work in the following industries"]):
            for i, opt in enumerate(options):
                if "none" in opt.lower():
                    return [Action(action_type="click", element_id=i, reasoning="industry trap")]
        
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
            resp = self.client.beta.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.PERSONA},
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

        except Exception:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.PERSONA},
                    {"role": "user", "content": [
                         {"type": "text", "text": prompt_text + "\n\nYou MUST respond with valid JSON matching this schema:\n" + SurveyDecision.model_json_schema()},
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
        except Exception:
            pass

class SurveyBot:
    def __init__(self, api_key: str, base_url: str, model: str, headless: bool = False, profile_name: str = "default"):
        self.browser = BrowserController(headless=headless, profile_dir=f"profiles/{profile_name}")
        self.ai = AIEngine(api_key, base_url, model)
        self.cache = AnswerCache(path=f"cache/{profile_name}_answers.json")
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
            png_bytes = self.browser.page.screenshot(type="png")
            img = Image.open(io.BytesIO(png_bytes))
            return str(imagehash.average_hash(img))
        except Exception:
            return None

    def is_stuck(self) -> bool:
        visual_hash = self._visual_fingerprint()
        now = time.time()
        if visual_hash and visual_hash == self.stuck_fingerprint:
            if self.stuck_since == 0:
                self.stuck_since = now
            return (now - self.stuck_since) > 35
        else:
            self.stuck_fingerprint = visual_hash
            self.stuck_since = 0
            return False

    def _handle_stuck(self):
        print("[!] Page hasn't changed in 40s — taking debug screenshot")
        self.browser.page.screenshot(path=f"debug_stuck_{self.screenshot_counter}.png")
        self.screenshot_counter += 1
        if self.browser.click_next():
            print("[+] Emergency Next clicked")

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
                # Re-prompt AI with cached answer context to get fresh element IDs
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
                            pass

                if act.action_type != "wait":
                    if not self._verify_action(pre_fp):
                        if act.coordinates and act.action_type == "click":
                            try:
                                self.browser.click_coords(*act.coordinates)
                            except Exception:
                                pass

            if not any(a.action_type == "next" for a in decision.actions):
                if decision.question_type in ("single_choice", "multi_choice", "text", "dropdown", "grid"):
                    time.sleep(0.5)
                    if self.browser.click_next():
                        print("     [+] Auto-clicked Next")

            if decision.memory_note:
                self.ai.memory.append(decision.memory_note)

            if decision and decision.memory_note and decision.question_type not in ("completion", "unknown"):
                cache_value = {
                    "memory_note": decision.memory_note,
                    "answer_summary": decision.page_summary[:200],
                    "question_type": decision.question_type
                }
                self.cache.set(page_text[:500], options_texts, json.dumps(cache_value))

    def stop(self):
        self.browser.stop()
