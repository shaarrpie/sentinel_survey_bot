import base64
import hashlib
import io
import json
import os
import platform
import random
import re
import sys
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

# ── debug logging ─────────────────────────────────────────────────────
from debug_logger import (
    get_survey_logger,
    StageTimer,
    StuckDetector,
    log_startup_diagnostics,
    DEBUG_LOGGING,
)
debug_log = get_survey_logger("SurveyLoop")
_stuck_detector = StuckDetector(debug_log)

# ── AI diagnostics ───────────────────────────────────────────────────
from ai_diagnostics import (
    AI_MAX_RETRIES,
    AI_REQUEST_TIMEOUT,
    AI_FAILURE_COOLDOWN,
    MAX_CONSECUTIVE_AI_FAILURES,
    log_request_failure,
    log_network_environment,
    _retryable,
)

# Module-level failure ledger for the controlled failure policy
_ai_consecutive_failures = 0
_ai_first_failure_at = None
_last_failure_category = None


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


def _fallback_decision(log, reason: str) -> Optional[SurveyDecision]:
    """Return a safe 'human_help' decision when the AI provider is unreachable.
    Prevents the survey loop from crashing — flags the page for human review."""
    log.warning(
        "AI FALLBACK | reason=%s — returning human_help decision",
        reason,
        extra={"stage": "AI"},
    )
    return SurveyDecision(
        page_summary=f"[FALLBACK] AI provider unreachable ({reason}). "
                     "Flagging for human review.",
        question_type="unknown",
        confidence=0.0,
        actions=[Action(
            action_type="human_help",
            reasoning=f"AI provider unreachable ({reason}). "
                      "Manual intervention required.",
        )],
        memory_note=f"AI fallback triggered: {reason}",
    )


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
        try:
            png = self.driver.get_screenshot_as_png()
            if png is None:
                debug_log.warning("get_screenshot_as_png() returned None", extra={"stage": "Capture"})
                return ""
            if compress:
                png = self._compress_screenshot(png)
                if png is None:
                    debug_log.warning("_compress_screenshot() returned None", extra={"stage": "Capture"})
                    return ""
            return base64.b64encode(png).decode()
        except Exception as e:
            debug_log.error(f"screenshot_b64 failed: {e}", extra={"stage": "Capture"}, exc_info=True)
            return ""

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
        except Exception as e:
            debug_log.warning(f"_compress_screenshot failed: {e}, returning original", extra={"stage": "Capture"})
            return png_bytes

    def get_element_map(self) -> List[dict]:
        try:
            result = self.driver.execute_script("""() => {
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
            return result if result is not None else []
        except Exception as e:
            debug_log.warning(f"get_element_map failed: {e}", extra={"stage": "Elements"})
            return []

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
            max_retries=0,  # WE own retries now (labeled, classified)
        )
        self.model = model
        self.memory: List[str] = []
        self.learned_rules: List[str] = []

        # Log the REAL client config at startup
        from urllib.parse import urlparse
        u = urlparse(str(self.client.base_url))
        debug_log.info(
            "AI-CLIENT | base_url=%s | max_retries=%s | timeout=%s",
            self.client.base_url,
            self.client.max_retries,
            self.client.timeout,
        )
        # Preflight: fail fast if endpoint is unreachable
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            import socket
            s = socket.create_connection((u.hostname, port), timeout=3)
            s.close()
            debug_log.info("AI-PREFLIGHT | TCP connect %s:%s OK", u.hostname, port)
        except OSError as e:
            debug_log.error("=" * 60)
            debug_log.error("AI ENDPOINT UNREACHABLE AT STARTUP")
            debug_log.error("=" * 60)
            debug_log.error("Endpoint: %s://%s:%s", u.scheme, u.hostname, port)
            debug_log.error("Error: %s", e)
            debug_log.error("The survey loop will NOT work until this is fixed.")
            debug_log.error("Fix: start the relay, or point base_url at a real provider.")
            debug_log.error("=" * 60)

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
        # Use word boundaries to avoid false matches (e.g. "age" in "garage").
        for keyword, answer in self.COMMON_ANSWERS.items():
            if re.search(rf"\b{re.escape(keyword)}\b", text_lower):
                target = next((e for e in elements
                               if real_input_kind(e) in
                               {"text", "email", "tel", "number", "date", "editable"}
                               and not (e.get("value") or "").strip()), None)
                if target is not None:
                    return [Action(action_type="type", element_id=target["id"],
                                   value=answer, reasoning=f"heuristic: {keyword}")]
        return None

    def decide(self, screenshot_b64: str, elements: List[dict], url: str, page_text: str) -> Optional[SurveyDecision]:
        global _ai_consecutive_failures, _ai_first_failure_at, _last_failure_category

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

        debug_log.debug(
            f"DECIDE call: elements={len(elements)} url={url[:80]}",
            extra={"stage": "AI"},
        )

        # --- Structured parse attempt ---
        started = time.time()
        for attempt in range(1, AI_MAX_RETRIES + 1):
            try:
                with StageTimer(debug_log, "ai_structured_call", threshold=10.0):
                    resp = self.client.chat.completions.parse(
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
                        timeout=AI_REQUEST_TIMEOUT,
                    )
                decision = resp.choices[0].message.parsed
                if decision is not None:
                    debug_log.debug(
                        f"Structured parse OK: type={decision.question_type} "
                        f"actions={len(decision.actions)}",
                        extra={"stage": "AI"},
                    )
                    # SUCCESS: reset failure ledger
                    _ai_consecutive_failures = 0
                    _ai_first_failure_at = None
                    return decision
                raise ValueError("Model returned no parseable decision")

            except Exception as e:
                category = log_request_failure(debug_log, e, attempt, started)
                _last_failure_category = category
                # Short-circuit: connection errors won't fix themselves in 2s
                if category in {"CONNECTION_REFUSED", "DNS_FAILURE", "PROXY_FAILURE"}:
                    debug_log.error(
                        "AI SHORT-CIRCUIT | %s — skipping retries, using fallback",
                        category,
                        extra={"stage": "AI"},
                    )
                    return _fallback_decision(debug_log, "connection_down")
                if attempt < AI_MAX_RETRIES and _retryable(category):
                    delay = min(2 ** attempt, 8)
                    debug_log.warning(
                        f"AI RETRY | sleeping {delay:.1f}s before attempt {attempt + 1}",
                        extra={"stage": "AI"},
                    )
                    time.sleep(delay)
                else:
                    break

        # --- Fallback: raw JSON mode ---
        debug_log.debug("Falling back to raw JSON mode", extra={"stage": "AI"})
        started = time.time()
        for attempt in range(1, AI_MAX_RETRIES + 1):
            try:
                with StageTimer(debug_log, "ai_raw_call", threshold=10.0):
                    resp = self.client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": get_persona()},
                            {"role": "user", "content": [
                                {"type": "text", "text": prompt_text + "\n\nYou MUST respond with valid JSON matching this schema:\n" + json.dumps(SurveyDecision.model_json_schema())},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}}
                            ]}
                        ],
                        max_tokens=2500,
                        temperature=0.2,
                        timeout=AI_REQUEST_TIMEOUT,
                    )
                raw = resp.choices[0].message.content.strip()
                if "```json" in raw:
                    raw = raw.split("```json")[1].split("```")[0]
                decision = SurveyDecision.model_validate_json(raw)
                debug_log.debug(
                    f"Raw JSON parse OK: type={decision.question_type} "
                    f"actions={len(decision.actions)}",
                    extra={"stage": "AI"},
                )
                # SUCCESS: reset failure ledger
                _ai_consecutive_failures = 0
                _ai_first_failure_at = None
                return decision

            except Exception as e:
                category = log_request_failure(debug_log, e, attempt, started)
                _last_failure_category = category
                # Short-circuit: connection errors won't fix themselves in 2s
                if category in {"CONNECTION_REFUSED", "DNS_FAILURE", "PROXY_FAILURE"}:
                    debug_log.error(
                        "AI SHORT-CIRCUIT | %s — skipping retries, using fallback",
                        category,
                        extra={"stage": "AI"},
                    )
                    return _fallback_decision(debug_log, "connection_down")
                if attempt < AI_MAX_RETRIES and _retryable(category):
                    delay = min(2 ** attempt, 8)
                    debug_log.warning(
                        f"AI RETRY | sleeping {delay:.1f}s before attempt {attempt + 1}",
                        extra={"stage": "AI"},
                    )
                    time.sleep(delay)
                else:
                    break

        # --- All paths failed: controlled failure policy ---
        _ai_consecutive_failures += 1
        _ai_first_failure_at = _ai_first_failure_at or time.time()

        debug_log.error(
            f"All AI decision paths failed (failures={_ai_consecutive_failures}/"
            f"{MAX_CONSECUTIVE_AI_FAILURES}, category={_last_failure_category})",
            extra={"stage": "AI"},
        )

        if _ai_consecutive_failures >= MAX_CONSECUTIVE_AI_FAILURES:
            total = time.time() - _ai_first_failure_at
            debug_log.error("=" * 60, extra={"stage": "AI"})
            debug_log.error("AI PROVIDER FAILURE", extra={"stage": "AI"})
            debug_log.error("=" * 60, extra={"stage": "AI"})
            debug_log.error(
                f"Consecutive failures: {_ai_consecutive_failures}",
                extra={"stage": "AI"},
            )
            debug_log.error(
                f"Endpoint: {self.ai.client.base_url if hasattr(self, 'ai') else 'unknown'}",
                extra={"stage": "AI"},
            )
            debug_log.error(
                f"Last error category: {_last_failure_category}",
                extra={"stage": "AI"},
            )
            debug_log.error(
                f"Total elapsed: {total:.1f}s", extra={"stage": "AI"}
            )
            debug_log.error("=" * 60, extra={"stage": "AI"})
            print("\n" + "=" * 60)
            print("AI PROVIDER FAILURE — LOOP STOPPED")
            print(f"  {_ai_consecutive_failures} consecutive failures")
            print(f"  Category: {_last_failure_category}")
            print("  Check that your LLM provider is running.")
            print("=" * 60)
            return None

        # Brake: sleep before allowing another iteration
        debug_log.warning(
            f"AI cooldown: sleeping {AI_FAILURE_COOLDOWN}s before next iteration",
            extra={"stage": "AI"},
        )
        time.sleep(AI_FAILURE_COOLDOWN)
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
        with StageTimer(debug_log, "visual_fingerprint", threshold=2.0):
            visual_hash = self._visual_fingerprint()
        now = time.time()
        if visual_hash and visual_hash == self.stuck_fingerprint:
            if self.stuck_since == 0:
                self.stuck_since = now
            elapsed = now - self.stuck_since
            debug_log.debug(
                f"Stuck check: SAME fingerprint for {elapsed:.1f}s "
                f"(threshold={STUCK_THRESHOLD_SECONDS}s)",
                extra={"stage": "StuckCheck"},
            )
            return elapsed > STUCK_THRESHOLD_SECONDS
        else:
            if visual_hash != self.stuck_fingerprint and self.stuck_since != 0:
                debug_log.debug(
                    "Stuck check: fingerprint CHANGED, resetting timer",
                    extra={"stage": "StuckCheck"},
                )
            self.stuck_fingerprint = visual_hash
            self.stuck_since = 0
            return False

    def _handle_stuck(self):
        debugLog.warning(
            f"Handling stuck state — taking debug screenshot "
            f"(counter={self.screenshot_counter})",
            extra={"stage": "StuckHandler"},
        )
        try:
            self.browser.driver.save_screenshot(
                f"debug_stuck_{self.screenshot_counter}.png"
            )
        except Exception as e:
            debugLog.error(
                f"Debug screenshot failed: {e}",
                extra={"stage": "StuckHandler"},
                exc_info=True,
            )
        if self.browser.click_next():
            debugLog.info(
                "Emergency Next clicked during stuck recovery",
                extra={"stage": "StuckHandler"},
            )
        else:
            debugLog.warning(
                "Could not click Next during stuck recovery",
                extra={"stage": "StuckHandler"},
            )
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
        log_startup_diagnostics(debug_log)
        log_network_environment(debug_log)  # Probe proxy/DNS/TCP before first AI call
        debug_log.info(
            "Browser opened. Navigate to survey, press F12 to start.",
            extra={"stage": "Interactive"},
        )
        print("[+] Browser opened. Navigate to the survey, then press F12 to start.")
        print("    Press Ctrl+C in this terminal to stop.")

        started = False
        poll_count = 0
        while True:
            time.sleep(0.5)
            poll_count += 1
            try:
                driver = self.browser.driver
                if driver and not started:
                    url = driver.current_url
                    if not url or url == "about:blank":
                        if poll_count % 20 == 0:
                            debug_log.debug(
                                f"Waiting for navigation (poll={poll_count})",
                                extra={"stage": "Interactive"},
                            )
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
                        debug_log.info(
                            f"F12 detected on {url} — starting survey loop",
                            extra={"stage": "Interactive"},
                        )
                        print(f"[+] F12 detected on {url} — starting survey loop")
                        self._run_survey_loop()
                        started = False
                        driver.execute_script("window.__sentinelStartKey = null;")
                        debug_log.info(
                            "Survey loop ended. Navigate to another page and press F12.",
                            extra={"stage": "Interactive"},
                        )
                        print("[+] Survey loop ended. Navigate to another page and press F12 again.")
            except KeyboardInterrupt:
                debug_log.info("Interrupted by user (Ctrl+C)", extra={"stage": "Interactive"})
                raise
            except Exception as e:
                debug_log.error(
                    f"run_interactive poll failed: {e}",
                    extra={"stage": "Interactive"},
                    exc_info=True,
                )
                logger.debug("run_interactive poll iteration failed", exc_info=True)

    def _run_survey_loop(self):
        log_startup_diagnostics(debug_log)
        loop_iteration = 0
        consecutive_timeouts = 0

        while True:
            loop_iteration += 1
            iter_start = time.perf_counter()
            debug_log.info(
                f"Iteration={loop_iteration} START",
                extra={"stage": "Loop"},
            )

            # --- Disqualification check ---
            with StageTimer(debug_log, "check_disqualified"):
                is_dq = self.browser.disqualified or self.browser.is_disqualified()
                if is_dq:
                    debug_log.warning(
                        "DISQUALIFIED detected", extra={"stage": "Loop"}
                    )
                    self.ai.learn_from_disqualification(self.ai.memory)
                    break

            # --- Completion check ---
            with StageTimer(debug_log, "check_completion"):
                if self.browser.is_completion():
                    debug_log.info(
                        "SURVEY COMPLETED", extra={"stage": "Loop"}
                    )
                    break

            # --- CAPTCHA check ---
            with StageTimer(debug_log, "check_captcha"):
                if self.browser.handle_captcha():
                    debug_log.info(
                        "CAPTCHA detected, waiting for manual solve",
                        extra={"stage": "Loop"},
                    )
                    continue

            # --- Stuck check ---
            with StageTimer(debug_log, "check_stuck", threshold=2.0):
                if self.is_stuck():
                    debug_log.warning(
                        f"STUCK detected (threshold={STUCK_THRESHOLD_SECONDS}s)",
                        extra={"stage": "Loop"},
                    )
                    self._handle_stuck()
                    continue

            # --- Capture page state ---
            with StageTimer(debug_log, "capture_state", threshold=3.0):
                screenshot = self.browser.screenshot_b64()
                elements = self.browser.get_element_map()
                page_text = self.browser.get_page_text()
                current_url = self.browser.get_url()
                page_title = ""
                try:
                    page_title = self.browser.driver.title or ""
                except Exception:
                    pass

                # Defensive: handle None returns
                screenshot_len = len(screenshot) if screenshot else 0
                elements_len = len(elements) if elements else 0
                page_text_len = len(page_text) if page_text else 0

                debug_log.debug(
                    f"State captured | url={current_url[:100] if current_url else 'None'} | "
                    f"title={page_title[:60]} | "
                    f"elements={elements_len} | "
                    f"page_text_len={page_text_len} | "
                    f"screenshot_b64_len={screenshot_len}",
                    extra={"stage": "Capture"},
                )

                if not screenshot:
                    debug_log.warning(
                        "Screenshot is empty, skipping iteration",
                        extra={"stage": "Capture"},
                    )
                    continue

            # --- Element analysis ---
            with StageTimer(debug_log, "analyze_elements"):
                elements = elements or []
                options_texts = [
                    e.get("text", "") for e in elements if e.get("text")
                ]
                visible_elements = sum(
                    1 for e in elements if e.get("text") or e.get("tag")
                )
                debug_log.debug(
                    f"Elements: total={len(elements)} visible={visible_elements} "
                    f"with_text={len(options_texts)}",
                    extra={"stage": "Elements"},
                )

            # --- Question detection ---
            with StageTimer(debug_log, "detect_question"):
                page_text = page_text or ""
                question_preview = page_text[:150].replace("\n", " ")
                debug_log.debug(
                    f"Question detected: {question_preview}",
                    extra={"stage": "Question"},
                )

            # --- Cache lookup ---
            with StageTimer(debug_log, "cache_lookup"):
                cached = self.cache.get(page_text[:500], options_texts)
                if cached:
                    debug_log.info(
                        f"CACHE HIT: {cached[:120]}",
                        extra={"stage": "Cache"},
                    )
                    page_text += (
                        f"\n\n[CONTEXT: You previously answered this question "
                        f"with: {cached}. Use the same answer.]"
                    )

            # --- Decision (heuristic or AI) ---
            decision = None
            decision_source = "none"
            with StageTimer(debug_log, "decide", threshold=5.0):
                if cached:
                    decision = self.ai.decide(
                        screenshot, elements, current_url, page_text
                    )
                    decision_source = "ai_cached_context"
                else:
                    # Try heuristic first
                    heuristic_actions = self.ai.try_heuristic(
                        page_text, options_texts, elements
                    )
                    if heuristic_actions:
                        debug_log.info(
                            f"HEURISTIC path: {len(heuristic_actions)} actions",
                            extra={"stage": "Decide"},
                        )
                        decision = SurveyDecision(
                            page_summary="heuristic",
                            question_type="unknown",
                            confidence=1.0,
                            actions=heuristic_actions,
                            memory_note="heuristic answer",
                        )
                        decision_source = "heuristic"
                    else:
                        debug_log.debug(
                            "No heuristic match, calling AI",
                            extra={"stage": "Decide"},
                        )
                        decision = self.ai.decide(
                            screenshot, elements, current_url, page_text
                        )
                        decision_source = "ai"

            # --- Decision result ---
            if not decision:
                debug_log.warning(
                    f"DECISION EMPTY (source={decision_source}) — retrying",
                    extra={"stage": "Decide"},
                )
                consecutive_timeouts += 1
                if consecutive_timeouts >= 5:
                    debug_log.error(
                        f"PROVIDER DOWN: {consecutive_timeouts} consecutive "
                        f"empty decisions. Check that your LLM provider is "
                        f"running at the configured BASE_URL.",
                        extra={"stage": "Decide"},
                    )
                    print("\n" + "=" * 60)
                    print("PROVIDER DOWN — LOOP STOPPED")
                    print(f"  {consecutive_timeouts} consecutive empty decisions")
                    print("  Check that your LLM provider is running.")
                    print(f"  BASE_URL: {self.ai.client.base_url}")
                    print("=" * 60)
                    break
                continue

            consecutive_timeouts = 0
            debug_log.info(
                f"DECISION: type={decision.question_type} "
                f"confidence={decision.confidence} "
                f"actions={len(decision.actions)} "
                f"source={decision_source}",
                extra={"stage": "Decide"},
            )

            if decision.question_type == "completion":
                debug_log.info(
                    "Completion detected by AI", extra={"stage": "Loop"}
                )
                break

            # --- Execute actions ---
            pre_fp = self._page_fingerprint()
            action_results = []
            with StageTimer(debug_log, "execute_actions", threshold=3.0):
                for act in decision.actions:
                    debug_log.debug(
                        f"ACTION: type={act.action_type} "
                        f"element_id={act.element_id} "
                        f"value={str(act.value)[:50] if act.value else 'None'} "
                        f"coords={act.coordinates} "
                        f"reasoning={act.reasoning[:80]}",
                        extra={"stage": "Action"},
                    )
                    pre_fp = self._page_fingerprint()
                    action_ok = False
                    try:
                        if act.action_type == "click":
                            if act.element_id is not None:
                                self.browser.click_element(act.element_id)
                                action_ok = True
                            elif act.coordinates:
                                self.browser.click_coords(*act.coordinates)
                                action_ok = True
                        elif act.action_type == "type":
                            if act.element_id is not None and act.value:
                                self.browser.type_into(
                                    act.element_id, act.value
                                )
                                action_ok = True
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
                                action_ok = True
                        elif act.action_type == "scroll":
                            self.browser.scroll_random()
                            action_ok = True
                        elif act.action_type == "next":
                            self.browser.click_next()
                            action_ok = True
                        elif act.action_type == "wait":
                            time.sleep(2)
                            action_ok = True
                        elif act.action_type == "human_help":
                            debug_log.warning(
                                "MANUAL HELP NEEDED — waiting for user",
                                extra={"stage": "Action"},
                            )
                            print("\n" + "=" * 50)
                            print("MANUAL HELP NEEDED")
                            print(f"URL: {current_url}")
                            print("=" * 50)
                            input("Press ENTER after manually fixing the page...")
                            action_ok = True

                        debug_log.debug(
                            f"ACTION RESULT: type={act.action_type} ok={action_ok}",
                            extra={"stage": "Action"},
                        )

                    except Exception as e:
                        debug_log.error(
                            f"ACTION FAILED: type={act.action_type} error={e}",
                            extra={"stage": "Action"},
                            exc_info=True,
                        )
                        action_ok = False
                        if act.coordinates and act.action_type in ("click", "type"):
                            try:
                                self.browser.click_coords(*act.coordinates)
                                debug_log.debug(
                                    "Fallback coordinate click succeeded",
                                    extra={"stage": "Action"},
                                )
                            except Exception as e2:
                                debug_log.error(
                                    f"Fallback click also failed: {e2}",
                                    extra={"stage": "Action"},
                                    exc_info=True,
                                )

                    # Verify action had effect
                    if act.action_type != "wait":
                        with StageTimer(debug_log, "verify_action"):
                            if not self._verify_action(pre_fp):
                                debug_log.warning(
                                    f"Action {act.action_type} had no effect",
                                    extra={"stage": "Verify"},
                                )
                                if act.coordinates and act.action_type == "click":
                                    try:
                                        self.browser.click_coords(*act.coordinates)
                                    except Exception as e:
                                        debug_log.error(
                                            f"Verify fallback failed: {e}",
                                            extra={"stage": "Verify"},
                                            exc_info=True,
                                        )

                    action_results.append(action_ok)

            # --- Auto-click Next if needed ---
            with StageTimer(debug_log, "auto_next"):
                has_next = any(a.action_type == "next" for a in decision.actions)
                if not has_next:
                    qtypes = ("single_choice", "multi_choice", "text", "dropdown", "grid")
                    if decision.question_type in qtypes:
                        time.sleep(0.5)
                        if self.browser.click_next():
                            debug_log.info(
                                "Auto-clicked Next", extra={"stage": "Nav"}
                            )

            # --- Memory & cache ---
            if decision.memory_note:
                self.ai.memory.append(decision.memory_note)
            if decision and decision.memory_note and decision.question_type not in ("completion", "unknown"):
                self.cache.set(page_text[:500], options_texts, {
                    "memory_note": decision.memory_note,
                    "answer_summary": decision.page_summary[:200],
                    "question_type": decision.question_type,
                })

            # --- Stuck detection ---
            page_fp = self._page_fingerprint()
            question_text = page_text[:200]
            diag = _stuck_detector.update(current_url, page_fp, question_text)

            # --- Iteration summary ---
            iter_elapsed = time.perf_counter() - iter_start
            debug_log.info(
                f"Iteration={loop_iteration} completed in {iter_elapsed:.2f}s | "
                f"url_changed={diag['url_changed']} | "
                f"question_changed={diag['question_changed']} | "
                f"fingerprint_changed={diag['fingerprint_changed']} | "
                f"actions_executed={len(action_results)} | "
                f"actions_ok={sum(action_results)} | "
                f"same_state_count={diag['same_state_count']} | "
                f"stuck_for={diag['stuck_elapsed_s']}s",
                extra={"stage": "Summary"},
            )

            # Slow iteration warning
            if iter_elapsed > 30:
                debug_log.warning(
                    f"SLOW iteration: {iter_elapsed:.1f}s — check timing breakdown above",
                    extra={"stage": "Summary"},
                )

    def stop(self):
        self.browser.stop()
