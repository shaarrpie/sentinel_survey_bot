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
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    WebDriverException,
    MoveTargetOutOfBoundsException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
    NoSuchElementException,
)
from webdriver_guard import (
    SessionGuard, SessionDeadError, HealthStatus, BotState,
    is_session_death,
    WD_MAX_RECOVERY_ATTEMPTS, WD_RECOVERY_BACKOFF,
    WD_SESSION_FAILURE_THRESHOLD,
)

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


def _levenshtein_ratio(s1: str, s2: str) -> float:
    """Calculate similarity ratio between two strings (0.0 to 1.0).

    Uses Levenshtein distance normalized by the longer string length.
    Returns 1.0 for identical strings, 0.0 for completely different.
    """
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    s1, s2 = s1.lower().strip(), s2.lower().strip()
    if s1 == s2:
        return 1.0

    # Quick substring check (handles "Mumbai" matching "01 - Mumbai / Maharashtra")
    if s1 in s2 or s2 in s1:
        shorter = min(len(s1), len(s2))
        longer = max(len(s1), len(s2))
        return shorter / longer

    # Levenshtein distance
    len1, len2 = len(s1), len(s2)
    if len1 < len2:
        s1, s2 = s2, s1
        len1, len2 = len2, len1

    previous_row = range(len2 + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    distance = previous_row[-1]
    return 1.0 - (distance / len1)


def fuzzy_match_option(target: str, options: List[dict], threshold: float = 0.8) -> Optional[dict]:
    """Find the best matching option using fuzzy string matching.

    Args:
        target: The expected value/keyword to match (e.g., "Mumbai")
        options: List of option dicts with 'text' and 'value' keys
        threshold: Minimum similarity ratio (0.0-1.0) to accept a match

    Returns:
        The best matching option dict, or None if no match above threshold
    """
    if not target or not options:
        return None

    best_match = None
    best_score = 0.0

    for opt in options:
        # Match against both text and value
        text = opt.get("text", "")
        value = opt.get("value", "")

        # Check text similarity
        text_score = _levenshtein_ratio(target, text)
        # Check value similarity
        value_score = _levenshtein_ratio(target, value)
        # Use the higher score
        score = max(text_score, value_score)

        if score > best_score:
            best_score = score
            best_match = opt

    if best_match and best_score >= threshold:
        return best_match
    return None


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


def _classify_click_failure(exc: Exception) -> str:
    """Classify a Selenium exception during a click action.

    Returns a short, stable classification string so that recovery logic
    can decide whether a retry with fresh coordinates is worthwhile or
    whether the defect is permanent (e.g. geometry error).
    """
    if isinstance(exc, MoveTargetOutOfBoundsException):
        return "TARGET_OUTSIDE_VIEWPORT"
    if isinstance(exc, StaleElementReferenceException):
        return "TARGET_STALE"
    if isinstance(exc, ElementClickInterceptedException):
        return "TARGET_OBSCURED"
    if isinstance(exc, NoSuchElementException):
        return "TARGET_NOT_FOUND"
    # Fallback: inspect the message text for known patterns
    msg = str(exc).lower()
    if "out of bounds" in msg:
        return "TARGET_OUTSIDE_VIEWPORT"
    if "stale" in msg:
        return "TARGET_STALE"
    if "intercepted" in msg:
        return "TARGET_OBSCURED"
    if "no such element" in msg:
        return "TARGET_NOT_FOUND"
    return "CLICK_FAILED"


class BrowserController:
    def __init__(self, headless: bool = False, slow_mo: int = 50, profile_dir: str = "profiles/default"):
        self.headless = headless
        self.slow_mo = slow_mo
        self.profile_dir = os.path.abspath(profile_dir)
        os.makedirs(self.profile_dir, exist_ok=True)
        self.driver = None
        self.disqualified = False
        # element_id → iframe index (None = top document), filled by
        # get_element_map(); used by _enter_active_frame() so element-backed
        # clicks/typing land in the right browsing context.
        self._element_frames: dict = {}
        # iframe index → (offset_x, offset_y) of the iframe's top-left in
        # top-document viewport coordinates (from the last element-map scan).
        self._frame_offsets: dict = {}
        # Metadata for the iframes seen during the last get_element_map()
        # scan — surfaced to the AI prompt as context (src, size, visibility).
        self.last_frame_info: list = []

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
            "source": self._stealth_script()
        })

        self.driver.request_interceptor = self._intercept

    def _intercept(self, request):
        """uc's request_interceptor callback. Receives the full CDP
        ``Network.requestWillBeSent`` event. We only treat a request as a
        disqualification when it's a top-level DOCUMENT navigation; the
        old loose substring-on-URL match fired on tracking pixels and ad
        sync beacons whose URLs routinely contain words like
        ``terminate`` or ``disqualified`` (e.g. ad-pixels with
        "disqualifier=0" query params)."""
        try:
            event = request if isinstance(request, dict) else {}
            req_type = (event.get("type") or "").lower()
            if req_type and req_type != "document":
                return
            req = event.get("request") or {}
            url = (req.get("url") or "").lower()
            if not url:
                return
            if any(x in url for x in ["disqualified", "screenout",
                                      "quota_full", "terminated"]):
                self.disqualified = True
        except Exception:
            logger.debug("swallowed exception in core.py", exc_info=True)

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
        // Real browsers have plugins (Chrome PDF Viewer, Native Client, etc.)
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
        // Real Chrome has window.chrome object with runtime property
        window.chrome = window.chrome || {};
        if (!window.chrome.runtime) {
            window.chrome.runtime = { PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' }, OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' }, OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' }, Arch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' } };
        }

        // ── Permissions.prototype.query ──────────────────────────────────
        // Patch to not reveal automation via permission queries
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
        // Spoof GPU info to avoid "SwiftShader" or "Mesa" detection
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {
            // UNMASKED_VENDOR_WEBGL = 0x9245
            if (param === 0x9245) return 'Google Inc. (NVIDIA)';
            // UNMASKED_RENDERER_WEBGL = 0x9246
            if (param === 0x9246) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
            return getParameter.call(this, param);
        };
        // Also patch WebGL2 if available
        if (window.WebGL2RenderingContext) {
            const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(param) {
                if (param === 0x9245) return 'Google Inc. (NVIDIA)';
                if (param === 0x9246) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return getParameter2.call(this, param);
            };
        }

        // ── iframe contentWindow patch ───────────────────────────────────
        // Ensure iframes inherit the same stealth properties
        const originalAttachShadow = Element.prototype.attachShadow;
        if (originalAttachShadow) {
            Element.prototype.attachShadow = function() {
                const shadow = originalAttachShadow.apply(this, arguments);
                return shadow;
            };
        }
        """

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
        """Frame-aware element map: the top document plus every VISIBLE
        depth-1 iframe. There is NO arbitrary 15-frame cap — ProProfs-style
        pages place the quiz iframe far down the DOM behind ad/tracking
        frames, so a low cap silently skips the content frame (observed:
        38 iframes on the target page, top-doc census useless, quiz inside
        a frame). Entries found inside an iframe carry a ``frame`` index,
        and their coordinates are already shifted by the iframe's offset so
        the whole map is expressed in top-document viewport-absolute pixels
        — the same space click_coords() and the AI's coordinate
        instructions use.

        WebDriver CAN switch into cross-origin iframes (same-origin policy
        only restricts a page's own JS, not execute_script running inside
        the frame itself) — so ad/tracking frames that load are scannable
        too. Only genuinely unswitchable frames are excluded per-frame
        below (pre-load about:blank, sandboxed without allow-scripts,
        teardown races).
        """
        elements: List[dict] = []
        self._element_frames = {}
        self._frame_offsets = {}
        self.last_frame_info = []
        try:
            # ── Top document ──────────────────────────────────────────────
            self._scan_current_frame(elements, None, (0, 0))
            # ── All depth-1 iframes (no low cap; skip invisible) ──────────
            try:
                # execute_script per frame.
                frames_meta = self.driver.execute_script("""
                    return Array.from(document.querySelectorAll('iframe')).map(
                        function (f, i) {
                            var r = f.getBoundingClientRect();
                            return {index: i, src: f.src || '',
                                    w: Math.round(r.width),
                                    h: Math.round(r.height),
                                    offx: Math.round(r.left),
                                    offy: Math.round(r.top),
                                    vis: r.width > 50 && r.height > 50};
                        });
                """)
            except Exception:
                frames_meta = []
            scanned_frames = 0
            frames_with_elements = 0
            for meta in frames_meta or []:
                i = int(meta.get("index", -1))
                if i < 0:
                    continue
                self.last_frame_info.append({
                    "index": i,
                    "src": str(meta.get("src") or "")[:200],
                    "w": meta.get("w", 0),
                    "h": meta.get("h", 0),
                    "visible": bool(meta.get("vis")),
                })
                if not meta.get("vis"):
                    continue
                off = (int(meta.get("offx", 0)), int(meta.get("offy", 0)))
                self._frame_offsets[i] = off
                before = len(elements)
                try:
                    self.driver.switch_to.frame(i)
                    try:
                        self._scan_current_frame(elements, i, off)
                    finally:
                        self.driver.switch_to.default_content()
                    scanned_frames += 1
                    if len(elements) > before:
                        frames_with_elements += 1
                except Exception as e:
                    # Unswitchable frame — NOT a cross-origin failure (see
                    # docstring). Pre-load / sandboxed / teardown races.
                    debug_log.debug(
                        f"iframe[{i}] not scannable ({e.__class__.__name__})",
                        extra={"stage": "Elements"},
                    )
                    self._reset_frame()
            if scanned_frames:
                debug_log.debug(
                    f"Frames scanned={scanned_frames} "
                    f"with-elements={frames_with_elements}",
                    extra={"stage": "Elements"},
                )
            # Store for locator-descriptor re-acquisition at click time
            self._last_elements = elements
            return elements
        except Exception as e:
            debug_log.warning(f"get_element_map failed: {e}", extra={"stage": "Elements"})
            self._reset_frame()
            return elements

    def _scan_current_frame(
        self, elements: List[dict], frame_index: Optional[int], offset: tuple
    ):
        """Run the element-scanner JS in the CURRENT browsing context and
        append the entries to ``elements`` with globally unique ids,
        per-element frame bookkeeping, and coordinates shifted by ``offset``
        into top-document viewport space."""
        # Per-context id base keeps data-bot-id globally unique: the top
        # document gets 0..999, iframe i gets (i+1)*1000... (bounded scans
        # never approach 1000 entries per context).
        start_id = 0 if frame_index is None else (frame_index + 1) * 1000
        try:
            result = self.driver.execute_script("""(startId) => {
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
                    const botId = startId + idx;
                    el.setAttribute('data-bot-id', botId);
                    const text = (el.innerText || el.getAttribute('aria-label') ||
                                 el.getAttribute('placeholder') || el.value || '').substring(0, 120);
                    const entry = {
                        id: botId,
                        tag: el.tagName.toLowerCase(),
                        type: semanticType,
                        role: role,
                        name: control.getAttribute('name') || '',
                        text: text,
                        x: Math.round(rect.left + rect.width / 2),
                        y: Math.round(rect.top + rect.height / 2),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height)
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
            }""", start_id)
        except Exception as e:
            debug_log.warning(
                f"element scan failed (frame={frame_index}): {e}",
                extra={"stage": "Elements"},
            )
            return
        # Fold this context's entries into the shared map: shift local
        # coordinates by the iframe's offset so EVERY entry is expressed in
        # top-document viewport pixels (the space click_coords uses), tag
        # iframe entries with their frame index, and record the element →
        # frame mapping for _enter_active_frame().
        for entry in result or []:
            entry["x"] = int(entry.get("x", 0)) + offset[0]
            entry["y"] = int(entry.get("y", 0)) + offset[1]
            if frame_index is not None:
                entry["frame"] = frame_index
                self._element_frames[entry["id"]] = frame_index
            elements.append(entry)

        # Census diagnostic — ONLY when the top document's scan returned
        # nothing. Distinguishes H1 "page never rendered interactive UI"
        # (matches=0), H2 "elements exist but all filtered out"
        # (matches>0 with hidden_null/small), and H3 "document not ready"
        # (readyState/visibilityState). Purely additive — never alters
        # the scan result.
        if frame_index is None and not (result or []):
            try:
                census = self.driver.execute_script("""
                    var selector = 'button, input, select, textarea, a, label, [onclick], [role="button"], [role="link"]';
                    var all = document.querySelectorAll(selector);
                    var matches = all.length;
                    var hidden_null = 0, small = 0;
                    all.forEach(function(el) {
                        if (el.offsetParent === null) hidden_null++;
                        var r = el.getBoundingClientRect();
                        if (r.width < 5 || r.height < 5) small++;
                    });
                    return {
                        matches: matches,
                        hidden_null: hidden_null,
                        small: small,
                        readyState: document.readyState,
                        visibilityState: document.visibilityState
                    };
                """)
                debug_log.warning(
                    f"ELEMENT MAP EMPTY | frame=top | "
                    f"matches={census['matches']} "
                    f"hidden_null={census['hidden_null']} "
                    f"small={census['small']} | "
                    f"readyState={census['readyState']} | "
                    f"visibilityState={census['visibilityState']}",
                    extra={"stage": "QuestionDetection"},
                )
            except Exception as e:
                debug_log.warning(
                    f"ELEMENT MAP EMPTY census_failed: {e}",
                    extra={"stage": "QuestionDetection"},
                )

    def get_page_text(self) -> str:
        return self.driver.find_element(By.TAG_NAME, "body").text

    def get_url(self) -> str:
        return self.driver.current_url

    def get_viewport(self) -> tuple[int, int]:
        """Live CSS-pixel viewport size (innerWidth x innerHeight)."""
        try:
            wh = self.driver.execute_script(
                "return [window.innerWidth, window.innerHeight];"
            )
            return (int(wh[0]), int(wh[1]))
        except Exception:
            logger.debug("get_viewport failed", exc_info=True)
            return (0, 0)

    def _enter_active_frame(self, element_id: Optional[int] = None):
        """Switch the driver into the browsing context that owns
        ``element_id`` according to the last get_element_map() scan.
        Always returns to the top document first, so calls compose safely
        and a stale/unknown id silently acts on the top document."""
        self._reset_frame()
        if element_id is None:
            return
        frame_index = self._element_frames.get(element_id)
        if frame_index is None:
            return  # element lives in the top document
        try:
            self.driver.switch_to.frame(frame_index)
        except Exception as e:
            debug_log.warning(
                f"frame enter failed for element_id={element_id} "
                f"(frame={frame_index}): {e.__class__.__name__}",
                extra={"stage": "Action"},
            )

    def _reset_frame(self):
        """Return the driver to the top document (safe to call anytime)."""
        try:
            self.driver.switch_to.default_content()
        except Exception:
            pass

    def click_element(self, element_id: int):
        """Click an element by ID, re-acquiring it fresh at execution time.

        Uses a 3-strategy fallback:
          1. CSS selector by data-bot-id
          2. XPath by associated label text
          3. elementFromPoint at the stored coordinates
        """
        # Look up the descriptor from the last element map
        descriptor = None
        if hasattr(self, '_last_elements') and self._last_elements:
            descriptor = next(
                (e for e in self._last_elements if e.get("id") == element_id), None
            )

        self._enter_active_frame(element_id)
        try:
            # Strategy 1: CSS selector by data-bot-id
            sel = f"[data-bot-id='{element_id}']"
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
            except Exception:
                el = None

            # Strategy 2: XPath by text if we have a descriptor
            if el is None and descriptor and descriptor.get("text"):
                text_safe = descriptor["text"][:80].replace("'", "\\'")
                try:
                    el = self.driver.find_element(
                        By.XPATH,
                        f"//*[@data-bot-id='{element_id}' or contains(text(), '{text_safe}')]"
                    )
                except Exception:
                    el = None

            # Strategy 3: elementFromPoint at stored coordinates
            if el is None and descriptor and descriptor.get("x") and descriptor.get("y"):
                x, y = descriptor["x"], descriptor["y"]
                el = self.driver.execute_script(
                    "return document.elementFromPoint(arguments[0], arguments[1]);",
                    x, y
                )

            if el is None:
                debug_log.warning(
                    f"CLICK_ELEMENT | element_id={element_id} not found by any strategy",
                    extra={"stage": "Action"},
                )
                return False

            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", el
            )

            # Click the label if the element is an input (bigger target)
            tag = el.tag_name.lower() if hasattr(el, 'tag_name') else ''
            if tag in ("input", "select"):
                parent_label = self.driver.execute_script(
                    "return arguments[0].closest('label');", el
                )
                if parent_label:
                    el = parent_label

            self.driver.execute_script("arguments[0].click();", el)
            return True
        finally:
            self._reset_frame()

    def click_coords(self, x: int, y: int, element_w: int = None, element_h: int = None):
        """
        Click at absolute VIEWPORT coordinates (x, y) with human-like mouse.

        If element_w/element_h are provided, jitter is relative to the
        element size (not the viewport) — critical for small targets like
        12×12px radio buttons where viewport-relative jitter of ±320px
        would miss entirely.

        Also adds realism: overshoot-and-correct (5% chance), Gaussian
        tremor on the path, and 10% chance of a mid-movement pause.
        """
        # ── Geometry diagnostics (before any action) ──────────────────────
        geo = self.driver.execute_script(
            "return {vw: window.innerWidth, vh: window.innerHeight, "
            "sx: window.scrollX, sy: window.scrollY, "
            "dpr: window.devicePixelRatio, "
            "doc_h: document.documentElement.scrollHeight, "
            "frames: document.querySelectorAll('iframe').length};"
        )
        vw, vh = int(geo["vw"]), int(geo["vh"])
        debug_log.debug(
            f"ACTION START | type=click | interaction=coords | target=({x},{y}) | "
            f"viewport={vw}x{vh} | scroll=({geo['sx']},{geo['sy']}) | "
            f"dpr={geo['dpr']} | doc_h={geo['doc_h']} | iframes={geo['frames']}",
            extra={"stage": "Action"},
        )

        # ── Bounds check (refuse invalid coords BEFORE W3C Perform Actions) ─
        if not (0 <= x < vw and 0 <= y < vh):
            debug_log.warning(
                f"TARGET_OUTSIDE_VIEWPORT | coords=({x},{y}) viewport=({vw}x{vh}) — "
                f"refusing invalid click",
                extra={"stage": "Action"},
            )
            return False

        # ── Jitter: element-relative if we know the size, else minimal ──
        if element_w and element_h:
            jitter_x = random.uniform(-0.25, 0.25) * element_w
            jitter_y = random.uniform(-0.25, 0.25) * element_h
        else:
            # Minimal jitter for coordinate-only clicks
            jitter_x = random.uniform(-2, 2)
            jitter_y = random.uniform(-2, 2)

        target_x = max(0, min(vw - 1, x + int(jitter_x)))
        target_y = max(0, min(vh - 1, y + int(jitter_y)))

        # ── Human-like mouse path (overshoot + tremor + pause) ──────────────
        self._human_mouse_to(target_x, target_y)

        # ── Perform click ───────────────────────────────────────────────────
        builder = ActionBuilder(self.driver)
        builder.pointer_action.move_to_location(target_x, target_y).click()
        builder.perform()
        return True

    def _human_mouse_to(self, target_x: int, target_y: int):
        """Move mouse to (target_x, target_y) with human-like behavior.

        Implements: cubic bezier path, overshoot-and-correct (5% chance),
        Gaussian tremor on every point, and 10% chance of a mid-path pause.
        """
        try:
            # Get current mouse position via JS
            current = self.driver.execute_script(
                "return {x: window.mouseX || 0, y: window.mouseY || 0};"
            )
            # Fallback: start from viewport center
            start_x = current.get("x", 0) or 0
            start_y = current.get("y", 0) or 0
            if not start_x and not start_y:
                start_x, start_y = 100, 100
        except Exception:
            start_x, start_y = 100, 100

        # 5% chance of overshooting the target and correcting
        if random.random() < 0.05:
            overshoot_x = target_x + random.randint(-30, 30)
            overshoot_y = target_y + random.randint(-30, 30)
        else:
            overshoot_x, overshoot_y = target_x, target_y

        # Build a cubic bezier path with tremor
        mid_x = (start_x + overshoot_x) // 2 + random.randint(-10, 10)
        mid_y = (start_y + overshoot_y) // 2 + random.randint(-10, 10)
        control1_x = mid_x + random.randint(-15, 15)
        control1_y = mid_y + random.randint(-15, 15)
        control2_x = mid_x + random.randint(-15, 15)
        control2_y = mid_y + random.randint(-15, 15)

        steps = max(5, int(((target_x - start_x) ** 2 + (target_y - start_y) ** 2) ** 0.5) // 10)
        steps = min(steps, 30)

        builder = ActionBuilder(self.driver)
        action = builder.pointer_action
        for i in range(steps + 1):
            t = i / steps
            # Cubic bezier interpolation
            cx = ((1 - t) ** 3 * start_x +
                  3 * (1 - t) ** 2 * t * control1_x +
                  3 * (1 - t) * t ** 2 * control2_x +
                  t ** 3 * overshoot_x)
            cy = ((1 - t) ** 3 * start_y +
                  3 * (1 - t) ** 2 * t * control1_y +
                  3 * (1 - t) * t ** 2 * control2_y +
                  t ** 3 * overshoot_y)

            # Gaussian tremor (sigma=1.5px)
            cx += random.gauss(0, 1.5)
            cy += random.gauss(0, 1.5)

            action.move_to_location(int(cx), int(cy))

            # 10% chance of a pause at a random intermediate point
            if i > 0 and i < steps and random.random() < 0.10:
                builder.perform()
                time.sleep(random.uniform(0.2, 0.8))

        # Final move to exact target
        action.move_to_location(target_x, target_y)
        builder.perform()

    def get_element_coords(self, element_id: int) -> Optional[tuple[int, int]]:
        """
        Re-acquire a single element's CURRENT viewport-absolute center
        via getBoundingClientRect (after scrollIntoView).

        Used by the fallback/verify paths to avoid reusing stale
        coordinates captured before scroll/click.
        """
        sel = f"[data-bot-id='{element_id}']"
        try:
            el = self.driver.find_element(By.CSS_SELECTOR, sel)
        except NoSuchElementException:
            debug_log.warning(
                f"TARGET_NOT_FOUND | element_id={element_id} — element no longer in DOM",
                extra={"stage": "Action"},
            )
            return None
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", el
        )
        rect = self.driver.execute_script(
            "const r = arguments[0].getBoundingClientRect();"
            "return [Math.round(r.left + r.width/2), Math.round(r.top + r.height/2)];",
            el,
        )
        return (int(rect[0]), int(rect[1]))

    def type_into(self, element_id: int, text: str, human_like: bool = True):
        """Type into an element bypassing React/Vue/Angular state updates.

        Uses JS value assignment + explicit event dispatch instead of
        send_keys(), which doesn't trigger onChange handlers on
        framework-controlled inputs.
        """
        sel = f"[data-bot-id='{element_id}']"
        el = self.driver.find_element(By.CSS_SELECTOR, sel)
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)

        if human_like:
            # Type character-by-character with JS dispatch for realism + framework compat
            for ch in text:
                self.driver.execute_script(
                    "arguments[0].value = arguments[0].value + arguments[1];"
                    "['input','change'].forEach(function(evtName) {"
                    "var evt = new Event(evtName, { bubbles: true });"
                    "arguments[0].dispatchEvent(evt);"
                    "});",
                    el, ch,
                )
                time.sleep(random.randint(30, 120) / 1000)
        else:
            # Fast path: set value + dispatch events in one script
            self.driver.execute_script(
                "arguments[0].value = arguments[1];"
                "['input','change'].forEach(function(evtName) {"
                "var evt = new Event(evtName, { bubbles: true });"
                "arguments[0].dispatchEvent(evt);"
                "});",
                el, text,
            )

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
        """Save cookies to disk for session persistence.

        Saves BEFORE navigation so if the bot crashes mid-survey,
        the session state is preserved. This is non-destructive —
        it doesn't refresh the page.
        """
        if not self.driver:
            return
        try:
            cookies = self.driver.get_cookies()
            cookie_path = os.path.join(self.profile_dir, "session_cookies.json")
            with open(cookie_path, "w") as f:
                json.dump(cookies, f, indent=2)
            logger.debug(f"Saved {len(cookies)} cookies to {cookie_path}")
        except Exception as e:
            logger.debug(f"save_session failed: {e}", exc_info=True)

    def load_session(self, refresh: bool = False) -> bool:
        """Load cookies from disk to restore a previous session.

        Args:
            refresh: If True, refresh the page after loading cookies.
                     Only should be True on initial navigation, NOT
                     mid-survey (which would reset everything).

        Returns:
            True if cookies were loaded successfully
        """
        cookie_path = os.path.join(self.profile_dir, "session_cookies.json")
        if not os.path.exists(cookie_path):
            return False
        try:
            with open(cookie_path, "r") as f:
                cookies = json.load(f)

            # Navigate to the cookie domain first (required for setting cookies)
            if cookies:
                first_cookie = cookies[0]
                domain = first_cookie.get("domain", "")
                if domain:
                    # Ensure we're on the right domain
                    if not self.driver.current_url.startswith(f"https://{domain}"):
                        self.driver.get(f"https://{domain}")

            # Add cookies
            for cookie in cookies:
                try:
                    # Remove problematic fields
                    cookie.pop("sameSite", None)
                    cookie.pop("storeId", None)
                    self.driver.add_cookie(cookie)
                except Exception:
                    pass

            # Only refresh on initial navigation, not mid-survey
            if refresh:
                self.driver.refresh()

            logger.debug(f"Loaded {len(cookies)} cookies from {cookie_path}")
            return True
        except Exception as e:
            logger.debug(f"load_session failed: {e}", exc_info=True)
            return False

    def stop(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                logger.debug("driver.quit() failed; falling back to service.stop()",
                             exc_info=True)
                try:
                    self.driver.service.stop()
                except Exception:
                    logger.debug("service.stop() also failed", exc_info=True)
            self.driver = None  # no stale handle for poll loops to grab

    def check_new_tabs(self) -> Optional[str]:
        """Check for newly opened tabs/windows.

        Survey routers (CPX Research, Dynata) often open new tabs mid-survey.
        This method detects new window handles and returns the URL of the
        most recently opened tab, or None if no new tabs.

        CRITICAL: Always restores the required (primary) browsing context
        before returning, so subsequent commands target the survey tab.

        Returns:
            URL of new tab if detected, None otherwise
        """
        if not self.driver:
            return None
        try:
            current_handles = set(self.driver.window_handles)
            # Initialize known handles on first call
            if not hasattr(self, '_known_window_handles') or not self._known_window_handles:
                self._known_window_handles = current_handles
                # The tab that was current at survey start is the REQUIRED
                # target; every cleanup must preserve it.
                try:
                    self._primary_window = self.driver.current_window_handle
                except Exception:
                    self._primary_window = None
                return None

            new_handles = current_handles - self._known_window_handles
            if new_handles:
                # New tab detected — peek at its URL WITHOUT abandoning the
                # required context. Set order is arbitrary; sort for
                # determinism (it is NOT recency).
                new_handle = sorted(new_handles)[0]
                original = getattr(self, "_primary_window", None)
                try:
                    self.driver.switch_to.window(new_handle)
                    new_url = self.driver.current_url
                    debug_log.info(
                        f"NEW TAB DETECTED | url={new_url[:100]} | "
                        f"total_tabs={len(current_handles)}",
                        extra={"stage": "TabMonitor"},
                    )
                    # Update known handles
                    self._known_window_handles = current_handles
                    return new_url
                finally:
                    # ALWAYS re-establish the required browsing context
                    # before any capture/command continues.
                    try:
                        if original in self.driver.window_handles:
                            self.driver.switch_to.window(original)
                    except Exception:
                        debug_log.error(
                            "TAB RESTORE FAILED | required window lost",
                            extra={"stage": "TabMonitor"},
                        )
                        raise

            # Check for closed tabs
            closed = self._known_window_handles - current_handles
            if closed:
                self._known_window_handles = current_handles
                # If our primary tab was closed, switch to remaining tab
                primary = getattr(self, "_primary_window", None)
                if primary not in current_handles and len(current_handles) > 0:
                    # Primary is gone — log loudly, don't guess
                    debug_log.error(
                        f"PRIMARY WINDOW LOST | primary={primary} "
                        f"not in remaining={current_handles}",
                        extra={"stage": "TabMonitor"},
                    )
                    self._primary_window = list(current_handles)[0]

            return None
        except Exception as e:
            logger.debug(f"check_new_tabs failed: {e}", exc_info=True)
            return None

    def close_extra_tabs(self):
        """Close all tabs except the REQUIRED (survey) window, then
        explicitly re-establish the required context.

        Never keys off 'current' — the current window may be the intruder
        itself (e.g., DevTools opened by F12). The required window is the
        one that was current when check_new_tabs() was first called.
        """
        if not self.driver:
            return
        try:
            handles = self.driver.window_handles
            if len(handles) <= 1:
                return
            primary = getattr(self, "_primary_window", None)
            if primary not in handles:
                debug_log.error(
                    f"TAB CLEANUP ABORTED | required window {primary} "
                    f"not in handles={handles} — not guessing",
                    extra={"stage": "TabMonitor"},
                )
                return
            for h in handles:
                if h == primary:
                    continue
                try:
                    self.driver.switch_to.window(h)
                    self.driver.close()
                    debug_log.info(
                        f"TAB CLOSED | handle={h[:12]}... (non-primary)",
                        extra={"stage": "TabMonitor"},
                    )
                except Exception as e:
                    debug_log.warning(
                        f"TAB CLOSE FAILED | handle={h[:12]}... | {e}",
                        extra={"stage": "TabMonitor"},
                    )
            # Re-establish the required target before returning control.
            self.driver.switch_to.window(primary)
            self._known_window_handles = {primary}
            debug_log.info(
                f"CLOSED EXTRA TABS | remaining=1 (primary={primary[:12]}...)",
                extra={"stage": "TabMonitor"},
            )
        except Exception as e:
            logger.debug(f"close_extra_tabs failed: {e}", exc_info=True)
            raise

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

    def sniper_match_dropdown(self, target_value: str, elements: List[dict], threshold: float = 0.8) -> Optional[Action]:
        """Fuzzy-match a dropdown option for sniper mode.

        Handles cases where the persona says "Mumbai" but the dropdown
        has "01 - Mumbai / Maharashtra". Uses Levenshtein distance
        instead of substring search.

        Args:
            target_value: The expected value from persona (e.g., "Mumbai")
            elements: Element map to search for select elements
            threshold: Minimum similarity ratio to accept

        Returns:
            Action to select the matched option, or None if no good match
        """
        # Find all select elements in the map
        selects = [e for e in elements if e.get("tag") == "select"]
        if not selects:
            return None

        for select_elem in selects:
            options = select_elem.get("options", [])
            if not options:
                continue

            # Try fuzzy match against all options
            match = fuzzy_match_option(target_value, options, threshold)
            if match:
                debug_log.info(
                    f"SNIPER MATCH | target='{target_value}' -> "
                    f"'{match.get('text', match.get('value', ''))}' "
                    f"(element_id={select_elem['id']})",
                    extra={"stage": "Sniper"},
                )
                return Action(
                    action_type="select_option",
                    element_id=select_elem["id"],
                    value=match.get("value", match.get("text", "")),
                    reasoning=f"sniper: fuzzy match '{target_value}'",
                )

        # No match above threshold — log for debugging
        debug_log.debug(
            f"SNIPER NO MATCH | target='{target_value}' — no option above {threshold} similarity",
            extra={"stage": "Sniper"},
        )
        return None

    def decide(
        self,
        screenshot_b64: str,
        elements: List[dict],
        url: str,
        page_text: str,
        viewport: Optional[tuple] = None,
        page_title: str = "",
        frame_info: Optional[List[dict]] = None,
        detected_type: Optional[str] = None,
    ) -> Optional[SurveyDecision]:
        global _ai_consecutive_failures, _ai_first_failure_at, _last_failure_category

        memory_block = "\n".join(self.memory[-12:]) if self.memory else "None yet."
        rules_block = "\n".join(f"- {r}" for r in self.learned_rules) if self.learned_rules else "None yet."

        # ── Change 3: truncation-aware budgets ────────────────────────────
        # OUTPUT_TRUNCATED (finish_reason="length") means the output budget
        # was exhausted mid-JSON. Retrying the IDENTICAL request truncates
        # identically, so these budgets are mutable and the retry ALTERS
        # the constraints instead (max_tokens x2, then halved elements).
        current_max_tokens = 2500
        element_budget = 40
        truncation_adjustments = 0

        # Viewport size for the coordinate-system instruction (0,0 when
        # unknown — callers normally always pass the live viewport).
        vw, vh = viewport if viewport else (0, 0)

        # Iframe summary — the AI must know the survey may live inside a
        # frame so it doesn't treat an empty top-document map as "no page".
        if frame_info:
            visible_frames = [f for f in frame_info if f.get("visible")]
            frames_block = (
                f"{len(frame_info)} iframe(s) on page, "
                f"{len(visible_frames)} visible"
                + "".join(
                    f"\n  - frame[{f['index']}] {f.get('src', '')[:100]}"
                    for f in visible_frames[:5]
                )
            )
        else:
            frames_block = "No iframes detected (all elements are in the top document)."

        # Detected question type — helps the AI understand the question format
        type_hint = f"Detected question type: {detected_type}" if detected_type and detected_type != "unknown" else ""

        def _build_prompt(budget: int) -> str:
            return f"""Analyze the survey screenshot and element map. Decide the next action(s).

URL: {url}
Title: {page_title[:150] or "(unknown)"}
Viewport: {vw}x{vh} CSS pixels (click coordinates are in this space)
Frames: {frames_block}
{type_hint}
Page text excerpt: {page_text[:2500]}

Interactive elements (id, tag, type, text, center coordinates):
{json.dumps(elements[:budget], indent=2)}

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
- Coordinates are CSS pixels relative to the viewport origin (top-left is
  (0,0), bottom-right is ({vw},{vh})). The screenshot may be scaled by
  devicePixelRatio — estimate positions relative to the viewport size, not
  the image's pixel dimensions.
- Only use element_id values that appear in the element map above; if the
  map is empty, click by coordinates instead of inventing an id.
- Keep memory_note to record what question was just answered for consistency."""

        prompt_text = _build_prompt(element_budget)

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
                        max_tokens=current_max_tokens,
                        temperature=0.2,
                        timeout=AI_REQUEST_TIMEOUT,
                    )
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    debug_log.info(
                        f"AI USAGE | completion={getattr(usage, 'completion_tokens', '?')} "
                        f"prompt={getattr(usage, 'prompt_tokens', '?')} "
                        f"total={getattr(usage, 'total_tokens', '?')} "
                        f"(unaccounted = reasoning/thinking tokens)",
                        extra={"stage": "AI"},
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
                if category == "OUTPUT_TRUNCATED":
                    # Identical requests truncate identically — ALTER the
                    # request instead of repeating it (report §24 Change 3).
                    if truncation_adjustments == 0:
                        truncation_adjustments += 1
                        current_max_tokens *= 2
                        prompt_text = _build_prompt(element_budget)
                        debug_log.warning(
                            f"OUTPUT TRUNCATED | adjusted retry 1/2: "
                            f"max_tokens={current_max_tokens}",
                            extra={"stage": "AI"},
                        )
                        continue
                    if truncation_adjustments == 1:
                        truncation_adjustments += 1
                        current_max_tokens *= 2
                        element_budget = 20
                        prompt_text = _build_prompt(element_budget)
                        debug_log.warning(
                            f"OUTPUT TRUNCATED | adjusted retry 2/2: "
                            f"max_tokens={current_max_tokens} "
                            f"element_budget={element_budget}",
                            extra={"stage": "AI"},
                        )
                        continue
                    debug_log.error(
                        "OUTPUT TRUNCATED after 2 adjusted retries — using fallback",
                        extra={"stage": "AI"},
                    )
                    return _fallback_decision(debug_log, "output_truncated")
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
                        max_tokens=current_max_tokens,
                        temperature=0.2,
                        timeout=AI_REQUEST_TIMEOUT,
                    )
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    debug_log.info(
                        f"AI USAGE | completion={getattr(usage, 'completion_tokens', '?')} "
                        f"prompt={getattr(usage, 'prompt_tokens', '?')} "
                        f"total={getattr(usage, 'total_tokens', '?')} "
                        f"(unaccounted = reasoning/thinking tokens)",
                        extra={"stage": "AI"},
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
                if category == "OUTPUT_TRUNCATED":
                    # Identical requests truncate identically — ALTER the
                    # request instead of repeating it (report §24 Change 3).
                    # Budgets/adjustments persist from the structured loop.
                    if truncation_adjustments == 0:
                        truncation_adjustments += 1
                        current_max_tokens *= 2
                        prompt_text = _build_prompt(element_budget)
                        debug_log.warning(
                            f"OUTPUT TRUNCATED | adjusted retry 1/2: "
                            f"max_tokens={current_max_tokens}",
                            extra={"stage": "AI"},
                        )
                        continue
                    if truncation_adjustments == 1:
                        truncation_adjustments += 1
                        current_max_tokens *= 2
                        element_budget = 20
                        prompt_text = _build_prompt(element_budget)
                        debug_log.warning(
                            f"OUTPUT TRUNCATED | adjusted retry 2/2: "
                            f"max_tokens={current_max_tokens} "
                            f"element_budget={element_budget}",
                            extra={"stage": "AI"},
                        )
                        continue
                    debug_log.error(
                        "OUTPUT TRUNCATED after 2 adjusted retries — using fallback",
                        extra={"stage": "AI"},
                    )
                    return _fallback_decision(debug_log, "output_truncated")
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
        self.guard = SessionGuard(debug_log)

    def _extract_persona_keywords(self, page_text: str) -> List[str]:
        """Extract persona keywords relevant for dropdown matching.

        Looks for demographic/location keywords from the persona that
        might appear in dropdown options. This feeds the sniper mode
        fuzzy matching.
        """
        keywords = []
        # Common demographic dropdown categories
        dropdown_categories = [
            "city", "state", "country", "location", "region",
            "gender", "age", "income", "education", "employment",
            "industry", "occupation", "language", "ethnicity",
            "marital status", "household", "zip", "postal"
        ]

        # Try to load persona from profile
        try:
            persona = get_persona()
            # Extract values from persona (simple extraction)
            persona_lower = persona.lower()

            # Look for key-value patterns in persona
            for category in dropdown_categories:
                if category in persona_lower:
                    # Extract the value after the category
                    import re
                    patterns = [
                        rf"{category}[:\s]+([^\n,]+)",
                        rf"{category}\s+is\s+([^\n,]+)",
                        rf"my\s+{category}[:\s]+([^\n,]+)",
                    ]
                    for pattern in patterns:
                        match = re.search(pattern, persona_lower)
                        if match:
                            value = match.group(1).strip()
                            if value and len(value) > 1:
                                keywords.append(value)
        except Exception:
            logger.debug("Failed to extract persona keywords", exc_info=True)

        # Also add common answers from AIEngine
        keywords.extend([
            "Female", "Male",  # gender
            "32", "25", "28", "35",  # age
            "Mumbai", "Delhi", "Bangalore", "Chennai", "Kolkata",  # Indian cities
            "Graduate", "Post Graduate",  # education
            "Full-time", "Part-time", "Self-employed",  # employment
            "Information Technology",  # industry
        ])

        return list(set(keywords))  # deduplicate

    @staticmethod
    def detect_question_type(elements: List[dict]) -> str:
        """Detect question type from element structure.

        Analyzes the element map to determine if this is single-choice,
        multi-select, dropdown, text, or grid. Used to enforce correct
        execution behavior (e.g., only one click for single-choice).
        """
        radios = [e for e in elements if e.get("type") == "radio"]
        checkboxes = [e for e in elements if e.get("type") == "checkbox"]
        selects = [e for e in elements if e.get("tag") == "select"]
        text_inputs = [e for e in elements if e.get("type") in ("text", "number", "email", "tel", "date")]

        # Grid detection: many radios with row-like structure
        if len(radios) > 10:
            # Check if radios share names in a grid pattern (matrix question)
            name_groups = {}
            for r in radios:
                name = r.get("name", "")
                name_groups[name] = name_groups.get(name, 0) + 1
            # If we have multiple groups of radios, it's likely a grid
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
        editables = [e for e in elements if e.get("type") == "editable" or e.get("tag") == "[contenteditable]"]
        if editables:
            return "text"

        return "unknown"

    @staticmethod
    def enforce_question_type(actions: List[Action], question_type: str) -> List[Action]:
        """Enforce question type constraints on actions.

        For single_choice: only execute the first click action.
        For dropdown: prioritize select_option over click.
        For grid: ensure all rows are answered.

        This prevents the AI from hallucinating multiple clicks for
        single-choice questions.
        """
        if question_type == "single_choice":
            # Only allow one click action for single-choice
            click_actions = [a for a in actions if a.action_type == "click"]
            if len(click_actions) > 1:
                # Keep only the first click, plus any non-click actions
                first_click = click_actions[0]
                non_clicks = [a for a in actions if a.action_type != "click"]
                return [first_click] + non_clicks

        elif question_type == "grid":
            # For grid questions, ensure we have click actions for each row
            # Don't filter — the AI should provide row-by-row answers
            pass

        elif question_type == "dropdown":
            # For dropdowns, convert click actions to select_option if possible
            enforced = []
            for act in actions:
                if act.action_type == "click" and act.value:
                    enforced.append(Action(
                        action_type="select_option",
                        element_id=act.element_id,
                        value=act.value,
                        reasoning=act.reasoning,
                    ))
                else:
                    enforced.append(act)
            return enforced

        return actions

    @staticmethod
    def calculate_reading_time(question_type: str, elements: List[dict], page_text: str) -> float:
        """Calculate realistic reading time based on question complexity.

        Humans read at different speeds depending on question type:
        - Single radio: 2-4 seconds
        - Grid with 10 rows: 8-15 seconds
        - Open-ended text: 10-20 seconds
        - Multi-select: 4-8 seconds
        - Dropdown: 3-6 seconds

        Args:
            question_type: Detected question type
            elements: Element map for counting options
            page_text: Page text for word count

        Returns:
            Recommended reading delay in seconds
        """
        # Base times by question type
        base_times = {
            "single_choice": (2.0, 4.0),
            "multi_choice": (4.0, 8.0),
            "dropdown": (3.0, 6.0),
            "text": (10.0, 20.0),
            "grid": (8.0, 15.0),
            "mixed": (5.0, 10.0),
            "unknown": (3.0, 6.0),
        }

        min_time, max_time = base_times.get(question_type, (3.0, 6.0))

        # Adjust based on number of options (more options = more reading)
        num_options = len([e for e in elements if e.get("type") in ("radio", "checkbox")])
        if num_options > 5:
            # Add 0.5s per additional option beyond 5
            extra_options = min(num_options - 5, 20)  # cap at 20 extra
            min_time += extra_options * 0.5
            max_time += extra_options * 0.5

        # Adjust based on text length (for open-ended questions)
        if question_type == "text":
            word_count = len(page_text.split())
            if word_count > 50:
                # Add time for reading longer questions
                min_time += min(word_count / 50, 10)  # cap at 10 extra seconds
                max_time += min(word_count / 30, 15)

        return random.uniform(min_time, max_time)

    def _page_fingerprint(self) -> str:
        text = self.browser.get_page_text()[:3000]
        url = self.browser.get_url()
        return hashlib.md5(f"{url}::{text}".encode()).hexdigest()

    def _structural_fingerprint(self) -> str:
        """Hash the DOM structure of the question container only.

        Includes: tag names, name attributes, for attributes, option values.
        Excludes: text content, timers, ads, dynamic fluff.

        This catches actual question changes while ignoring:
        - Timer updates ("You have 4:32 remaining")
        - Ad rotations
        - Dynamic text that doesn't affect question structure
        """
        try:
            structural = self.browser.driver.execute_script("""
                // Find the question container — survey pages typically wrap
                // the active question in a container with role or class hints.
                // Fall back to body if no specific container found.
                const container = document.querySelector(
                    '[role="main"], .question-container, .survey-question, ' +
                    '.quiz-question, [data-question], #question, ' +
                    '.form-group, fieldset'
                ) || document.body;

                // Walk the DOM tree and build a structural signature
                const parts = [];
                const walker = document.createTreeWalker(
                    container,
                    NodeFilter.SHOW_ELEMENT,
                    {
                        acceptNode: (node) => {
                            // Skip dynamic/timer elements
                            const tag = node.tagName.toLowerCase();
                            const cls = (node.className || '').toString();
                            const id = node.id || '';

                            // Skip nav, footer, ads, timers, scripts
                            if (['nav', 'footer', 'script', 'style', 'noscript'].includes(tag)) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            if (cls.match(/\b(timer|countdown|clock|ad|advert|banner|social|share)\b/i)) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            if (id.match(/\b(timer|countdown|clock|ad|advert|banner)\b/i)) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            // Skip hidden elements
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

                    // Build structural token (NO text content)
                    let token = tag;
                    if (name) token += `[name=${name}]`;
                    if (forAttr) token += `[for=${forAttr}]`;
                    if (role) token += `[role=${role}]`;
                    if (type) token += `[type=${type}]`;
                    if (inputType && inputType !== tag) token += `[input=${inputType}]`;

                    // For select elements, include option values (not text)
                    if (tag === 'select') {
                        const opts = Array.from(node.options).map(o => o.value).join(',');
                        token += `{opts=${opts}}`;
                    }

                    // For radio/checkbox, include checked state
                    if (inputType === 'radio' || inputType === 'checkbox') {
                        token += `{checked=${node.checked ? 1 : 0}}`;
                    }

                    parts.push(token);
                }

                return parts.join('|');
            """)
            if structural:
                return hashlib.md5(structural.encode()).hexdigest()
        except Exception:
            logger.debug("structural fingerprint failed", exc_info=True)
        # Fallback to text-based fingerprint
        return self._page_fingerprint()

    @staticmethod
    def _nearest_element(
        elements: list, target_x: int, target_y: int
    ) -> Optional[dict]:
        """Find the nearest element in the map to (target_x, target_y).

        Used by the coordinate-only recovery path (when element_id is None)
        to re-project stale stale coordinates onto a fresh element read.
        """
        if not elements:
            return None
        best = None
        best_dist = float("inf")
        for el in elements:
            ex = el.get("x")
            ey = el.get("y")
            if ex is None or ey is None:
                continue
            dist = (ex - target_x) ** 2 + (ey - target_y) ** 2
            if dist < best_dist:
                best_dist = dist
                best = el
        return best

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
        debug_log.warning(
            f"Handling stuck state — taking debug screenshot "
            f"(counter={self.screenshot_counter})",
            extra={"stage": "StuckHandler"},
        )
        try:
            self.browser.driver.save_screenshot(
                f"debug_stuck_{self.screenshot_counter}.png"
            )
        except Exception as e:
            debug_log.error(
                f"Debug screenshot failed: {e}",
                extra={"stage": "StuckHandler"},
                exc_info=True,
            )
        if self.browser.click_next():
            debug_log.info(
                "Emergency Next clicked during stuck recovery",
                extra={"stage": "StuckHandler"},
            )
        else:
            debug_log.warning(
                "Could not click Next during stuck recovery",
                extra={"stage": "StuckHandler"},
            )
        self.stuck_fingerprint = None
        self.stuck_since = 0

    def _verify_action(self, pre_fingerprint: str) -> bool:
        time.sleep(0.8)
        post_fingerprint = self._page_fingerprint()
        if post_fingerprint == pre_fingerprint:
            print("[!] Action had no effect")
            return False
        return True

    def run(self, url: str):
        self.browser.start()
        self.browser.goto(url)
        print(f"[+] Loaded: {url}")
        self._run_survey_loop()

    def run_interactive(self):
        self.browser.start()
        self.guard.attach(self.browser.driver)
        self.guard.set_state(BotState.BROWSER_READY)
        log_startup_diagnostics(debug_log)
        log_network_environment(debug_log)  # Probe proxy/DNS/TCP before first AI call
        self.guard.set_state(BotState.WAITING_FOR_F12)
        debug_log.info(
            "Browser opened. Navigate to survey, press F12 to start.",
            extra={"stage": "Interactive"},
        )
        print("[+] Browser opened. Navigate to the survey, then press F12 to start.")
        print("    Press Ctrl+C in this terminal to stop.")

        started = False

        while True:
            time.sleep(0.5)
            self.guard.poll_count += 1
            try:
                driver = self.browser.driver
                if not driver:
                    debug_log.debug("driver is None in poll loop", extra={"stage": "Interactive"})
                    continue
                if not started:
                    url = self.guard.trace(
                        "get_current_url", lambda: driver.current_url)
                    self.guard.last_known_url = url
                    self.guard.maybe_heartbeat(self.guard.poll_count)
                    if not url or url == "about:blank":
                        if self.guard.poll_count % 20 == 0:
                            debug_log.debug(
                                f"Waiting for navigation (poll={self.guard.poll_count})",
                                extra={"stage": "Interactive"},
                            )
                        continue

                    self.guard.trace("install_key_listener", lambda: driver.execute_script(
                        "if (!window.__sentinelKeyListenerInstalled) {"
                        "window.__sentinelKeyListenerInstalled = true;"
                        "window.__sentinelStartKey = null;"
                        "document.addEventListener('keydown', function(e) {"
                        "window.__sentinelStartKey = e.key;}, true);}"))
                    last_key = self.guard.trace(
                        "read_start_key",
                        lambda: driver.execute_script("return window.__sentinelStartKey;"))
                    self.guard.reset_poll_failures()
                    if last_key == "F12":
                        started = True
                        self.guard.set_state(BotState.SURVEY_STARTED)
                        debug_log.info(
                            f"F12 detected on {url} — starting survey loop",
                            extra={"stage": "Interactive"},
                        )
                        print(f"[+] F12 detected on {url} — starting survey loop")
                        try:
                            self._run_survey_loop()
                        finally:
                            started = False
                            # Re-arm the start latch: without this the previous
                            # "F12" stays in window.__sentinelStartKey and the
                            # poll loop auto-restarts the survey 0.5 s after
                            # every loop exit (852f316 regression).
                            try:
                                driver.execute_script(
                                    "window.__sentinelStartKey = null;")
                            except Exception:
                                logger.debug(
                                    "sentinel start-key reset failed",
                                    exc_info=True,
                                )
                        if self.guard.state != BotState.STOPPED:
                            self.guard.set_state(BotState.WAITING_FOR_F12)
                            debug_log.info(
                                "Survey loop ended. Navigate to another page and press F12.",
                                extra={"stage": "Interactive"},
                            )
                            print("[+] Survey loop ended. Navigate to another page and press F12 again.")

            except KeyboardInterrupt:
                self.guard.set_state(BotState.STOPPED)
                debug_log.info("Interrupted by user (Ctrl+C)", extra={"stage": "Interactive"})
                raise

            except SessionDeadError as sde:
                if not self._handle_session_death(sde):
                    break
                started = False

            except (InvalidSessionIdException, NoSuchWindowException) as e:
                if not self._handle_session_death(
                        SessionDeadError(HealthStatus.SESSION_INVALID, str(e))):
                    break
                started = False

            except WebDriverException as e:
                if is_session_death(e):
                    if not self._handle_session_death(
                            SessionDeadError(HealthStatus.SESSION_INVALID, str(e))):
                        break
                    started = False
                else:
                    self._note_transient_poll_error(e)

            except Exception as e:
                # NOT a session death — count, one-line log, exponential backoff.
                self._note_transient_poll_error(e)

    def _note_transient_poll_error(self, e: Exception) -> None:
        """Non-fatal poll error: count + compact log + backoff. Never dump
        a full traceback at 2 Hz; escalate to session-loss only after
        WD_SESSION_FAILURE_THRESHOLD consecutive failures."""
        if self.guard.note_poll_failure():
            debug_log.error(
                "POLL FAILURES reached threshold=%d (last: %s: %s) — treating as session loss",
                WD_SESSION_FAILURE_THRESHOLD, type(e).__name__, str(e)[:200],
                extra={"stage": "Interactive"},
            )
            if not self._handle_session_death(
                    SessionDeadError(HealthStatus.UNKNOWN, str(e))):
                self.guard.set_state(BotState.STOPPED)
                return
        else:
            debug_log.warning(
                "run_interactive poll failed (%d/%d): %s: %s",
                self.guard.consecutive_poll_failures,
                WD_SESSION_FAILURE_THRESHOLD,
                type(e).__name__, str(e)[:200],
                extra={"stage": "Interactive"},
            )
            time.sleep(min(2 ** self.guard.consecutive_poll_failures, 15))

    def _handle_session_death(self, sde: SessionDeadError) -> bool:
        """One banner + one crash report + bounded recovery. Returns True
        if polling should resume on a NEW session, False to stop cleanly."""
        self.guard.set_state(BotState.WEBDRIVER_FAILURE)
        snap = self.guard.diagnose(sde)
        self.guard.log_failure_banner(snap)
        report = self.guard.write_crash_report(snap, sde)
        if report:
            debug_log.error("Crash report: %s", report, extra={"stage": "WDGuard"})

        while self.guard.recovery_attempts < WD_MAX_RECOVERY_ATTEMPTS:
            self.guard.recovery_attempts += 1
            self.guard.set_state(BotState.RECOVERY)
            debug_log.warning(
                "RECOVERY %d/%d — relaunching browser (backoff %.0fs)",
                self.guard.recovery_attempts, WD_MAX_RECOVERY_ATTEMPTS,
                WD_RECOVERY_BACKOFF, extra={"stage": "WDGuard"},
            )
            try:
                self.browser.stop()  # best-effort cleanup (HUNK 3)
            except Exception:
                logger.debug("browser.stop() during recovery raised", exc_info=True)
            time.sleep(WD_RECOVERY_BACKOFF)
            try:
                self.browser.start()
                self.guard.attach(self.browser.driver)
                if self.guard.health(force=True) == HealthStatus.SESSION_HEALTHY:
                    self.guard.set_state(BotState.WAITING_FOR_F12)
                    debug_log.info(
                        "RECOVERY OK — new session healthy. Navigate + press F12.",
                        extra={"stage": "WDGuard"},
                    )
                    print("[+] Browser relaunched after session failure. Press F12 when ready.")
                    self.guard.consecutive_poll_failures = 0
                    return True
            except Exception as e:
                debug_log.error(
                    "Recovery relaunch failed: %s: %s",
                    type(e).__name__, str(e)[:200],
                    extra={"stage": "WDGuard"}, exc_info=True,
                )

        debug_log.error(
            "WEBDRIVER SESSION FAILURE — recovery exhausted (%d attempts). Stopping cleanly.",
            WD_MAX_RECOVERY_ATTEMPTS, extra={"stage": "WDGuard"},
        )
        self.guard.set_state(BotState.STOPPED)
        return False

    def _run_survey_loop(self):
        """Loop driver. The body lives in ``_survey_loop_iteration`` so
        every WebDriver command is wrapped by a single typed exception
        boundary that converts session-death into ``SessionDeadError``,
        letting ``run_interactive`` engage bounded recovery instead of
        the legacy blanket-except spam loop."""
        log_startup_diagnostics(debug_log)
        loop_iteration = 0
        consecutive_timeouts = 0

        while True:
            loop_iteration += 1
            self.guard.iteration = loop_iteration
            iter_start = time.perf_counter()
            self.guard.set_state(BotState.QUESTION_DETECTION)
            debug_log.info(
                f"Iteration={loop_iteration} START",
                extra={"stage": "Loop"},
            )

            try:
                # The body manages its own ``break``/``continue`` and returns
                # only when the loop is truly done. ``consecutive_timeouts``
                # is read+written via a list-as-cell for mutation across calls.
                keep_going = self._survey_loop_iteration(
                    loop_iteration, iter_start,
                    [consecutive_timeouts],  # mutable cell
                )
                consecutive_timeouts = keep_going
                if not keep_going and keep_going is not None:
                    return
                if keep_going is None:
                    return
            except SessionDeadError:
                raise
            except (InvalidSessionIdException, NoSuchWindowException) as e:
                raise SessionDeadError(
                    HealthStatus.SESSION_INVALID,
                    f"iteration {loop_iteration}: {e}") from e
            except WebDriverException as e:
                if is_session_death(e):
                    raise SessionDeadError(
                        HealthStatus.SESSION_INVALID,
                        f"iteration {loop_iteration}: {e}") from e
                raise

    def _survey_loop_iteration(
        self,
        loop_iteration: int,
        iter_start: float,
        consecutive_timeouts_cell: list,
    ):
        """One iteration of the survey loop. Returns:
          - ``True``  → continue the loop (timeout counter unchanged)
          - ``False`` → continue the loop, but reset the timeout counter
          - ``None``  → break out of the loop (survey done, completion, DQ, or provider down)
        """
        consecutive_timeouts = consecutive_timeouts_cell[0]

        # --- Disqualification check ---
        with StageTimer(debug_log, "check_disqualified"):
            is_dq = self.browser.disqualified or self.browser.is_disqualified()
            if is_dq:
                debug_log.warning(
                    "DISQUALIFIED detected", extra={"stage": "Loop"}
                )
                self.ai.learn_from_disqualification(self.ai.memory)
                return None

        # --- Completion check ---
        with StageTimer(debug_log, "check_completion"):
            if self.browser.is_completion():
                debug_log.info(
                    "SURVEY COMPLETED", extra={"stage": "Loop"}
                )
                return None

        # --- CAPTCHA check ---
        with StageTimer(debug_log, "check_captcha"):
            if self.browser.handle_captcha():
                debug_log.info(
                    "CAPTCHA detected, waiting for manual solve",
                    extra={"stage": "Loop"},
                )
                return True

        # --- Stuck check ---
        with StageTimer(debug_log, "check_stuck", threshold=2.0):
            if self.is_stuck():
                debug_log.warning(
                    f"STUCK detected (threshold={STUCK_THRESHOLD_SECONDS}s)",
                    extra={"stage": "Loop"},
                )
                self._handle_stuck()
                return True

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

        # --- Tab monitoring ---
        # Survey routers (CPX Research, Dynata) often open new tabs mid-survey.
        # Check for new tabs and handle them.
        with StageTimer(debug_log, "check_tabs"):
            new_tab_url = self.browser.check_new_tabs()
            if new_tab_url:
                debug_log.info(
                    f"NEW TAB HANDLED | url={new_tab_url[:100]} — "
                    f"closing extra tabs to maintain single-tab flow",
                    extra={"stage": "TabMonitor"},
                )
                # Close extra tabs to maintain single-tab flow
                self.browser.close_extra_tabs()
                # Re-capture state after tab handling
                screenshot = self.browser.screenshot_b64()
                elements = self.browser.get_element_map()
                page_text = self.browser.get_page_text()
                current_url = self.browser.get_url()

            if not screenshot:
                debug_log.warning(
                    "Screenshot is empty, skipping iteration",
                    extra={"stage": "Capture"},
                )
                return True

        # --- Element analysis ---
        with StageTimer(debug_log, "analyze_elements"):
            elements = elements or []
            options_texts = [
                e.get("text", "") for e in elements if e.get("text")
            ]
            visible_elements = sum(
                1 for e in elements if e.get("text") or e.get("tag")
            )
            # Detect question type from element structure
            detected_type = self.detect_question_type(elements)
            debug_log.debug(
                f"Elements: total={len(elements)} visible={visible_elements} "
                f"with_text={len(options_texts)} detected_type={detected_type}",
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

        # --- Reading delay (variable based on question complexity) ---
        # Humans don't answer instantly — they read the question first.
        # Vary the delay based on question type and complexity.
        with StageTimer(debug_log, "reading_delay"):
            reading_time = self.calculate_reading_time(detected_type, elements, page_text)
            debug_log.debug(
                f"Reading delay: {reading_time:.1f}s (type={detected_type})",
                extra={"stage": "Timing"},
            )
            time.sleep(reading_time)

        # --- Decision (heuristic or AI) ---
        self.guard.set_state(BotState.AI_DECISION)
        decision = None
        decision_source = "none"
        with StageTimer(debug_log, "decide", threshold=5.0):
            if cached:
                decision = self.ai.decide(
                    screenshot, elements, current_url, page_text,
                    viewport=self.browser.get_viewport(),
                    page_title=page_title,
                    frame_info=self.browser.last_frame_info,
                    detected_type=detected_type,
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
                    # Try sniper mode for dropdowns with persona keywords
                    # This handles cases where dropdown values differ from
                    # persona text (e.g., "Mumbai" vs "01 - Mumbai / Maharashtra")
                    sniper_action = None
                    selects = [e for e in elements if e.get("tag") == "select"]
                    if selects:
                        # Extract persona keywords from page context
                        # Look for location/demographic keywords in persona
                        persona_keywords = self._extract_persona_keywords(page_text)
                        for keyword in persona_keywords:
                            sniper_action = self.ai.sniper_match_dropdown(
                                keyword, elements, threshold=0.75
                            )
                            if sniper_action:
                                debug_log.info(
                                    f"SNIPER path: matched '{keyword}'",
                                    extra={"stage": "Decide"},
                                )
                                break

                    if sniper_action:
                        decision = SurveyDecision(
                            page_summary="sniper match",
                            question_type="dropdown",
                            confidence=0.9,
                            actions=[sniper_action, Action(action_type="next", reasoning="proceed after sniper match")],
                            memory_note="sniper dropdown match",
                        )
                        decision_source = "sniper"
                    else:
                        debug_log.debug(
                            "No heuristic/sniper match, calling AI",
                            extra={"stage": "Decide"},
                        )
                        decision = self.ai.decide(
                            screenshot, elements, current_url, page_text,
                            viewport=self.browser.get_viewport(),
                            page_title=page_title,
                            frame_info=self.browser.last_frame_info,
                            detected_type=detected_type,
                        )
                        decision_source = "ai"

        # --- Decision result ---
        if not decision:
            debug_log.warning(
                f"DECISION EMPTY (source={decision_source}) — retrying",
                extra={"stage": "Decide"},
            )
            consecutive_timeouts += 1
            consecutive_timeouts_cell[0] = consecutive_timeouts
            if consecutive_timeouts >= 5:
                debug_log.error(
                    f"PROVIDER DOWN: {consecutive_timeouts} consecutive "
                    "empty decisions. Check that your LLM provider is "
                    "running at the configured BASE_URL.",
                    extra={"stage": "Decide"},
                )
                print("\n" + "=" * 60)
                print("PROVIDER DOWN — LOOP STOPPED")
                print(f"  {consecutive_timeouts} consecutive empty decisions")
                print("  Check that your LLM provider is running.")
                print(f"  BASE_URL: {self.ai.client.base_url}")
                print("=" * 60)
                return None
            return True

        consecutive_timeouts = 0
        consecutive_timeouts_cell[0] = 0
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
            return None

        # --- Question type detection & enforcement ---
        # Detect question type from elements and enforce constraints
        # on the AI's actions (e.g., only one click for single-choice)
        with StageTimer(debug_log, "enforce_question_type"):
            detected_type = self.detect_question_type(elements)
            # Use the more specific type: prefer detected over AI's guess
            # when detected is not unknown (AI may misclassify)
            effective_type = detected_type if detected_type != "unknown" else decision.question_type
            if effective_type != decision.question_type:
                debug_log.info(
                    f"QUESTION TYPE MISMATCH: AI said {decision.question_type}, "
                    f"detected {effective_type} — using detected type",
                    extra={"stage": "Enforce"},
                )
            # Enforce type constraints on actions
            original_action_count = len(decision.actions)
            enforced_actions = self.enforce_question_type(decision.actions, effective_type)
            if len(enforced_actions) != original_action_count:
                debug_log.info(
                    f"QUESTION TYPE ENFORCEMENT: filtered from "
                    f"{original_action_count} to {len(enforced_actions)} actions "
                    f"(type={effective_type})",
                    extra={"stage": "Enforce"},
                )
            decision = decision.model_copy(update={"actions": enforced_actions, "question_type": effective_type})

        # --- Execute actions ---
        self.guard.set_state(BotState.ANSWER_ACTION)
        pre_fp = self._page_fingerprint()
        action_results = []
        verified_results = []
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
                # An empty element map (or a scan that returned nothing —
                # page not rendered / wrong browsing context) means no
                # interactive target can exist. Refuse blind clicks/types
                # loudly instead of churning unverifiable guesses; escalate
                # via the stuck detector / human_help path.
                if not elements and act.action_type in (
                        "click", "type", "select_multi", "next"):
                    debug_log.error(
                        f"NO_ACTIONABLE_ELEMENTS | element map empty — refusing "
                        f"blind {act.action_type} (page likely not interactive); "
                        f"escalate via stuck/human_help path",
                        extra={"stage": "Action"},
                    )
                    action_results.append(False)
                    continue
                # Guard against hallucinated element_ids: the AI only sees the
                # element map captured this iteration, so any id outside it
                # cannot exist in the DOM (typical when the map is empty
                # because the quiz lives inside an iframe).
                if act.element_id is not None:
                    known_ids = {el.get("id") for el in elements}
                    if act.element_id not in known_ids:
                        debug_log.warning(
                            f"HALLUCINATED_ID | element_id={act.element_id} not "
                            f"in element map ({len(known_ids)} elements) — "
                            f"dropping to coords={act.coordinates}",
                            extra={"stage": "Action"},
                        )
                        act = act.model_copy(update={"element_id": None})
                pre_fp = self._page_fingerprint()
                action_ok = False
                try:
                    if act.action_type == "click":
                        if act.element_id is not None:
                            self.browser.click_element(act.element_id)
                            action_ok = True
                        elif act.coordinates:
                            action_ok = self.browser.click_coords(*act.coordinates)
                        else:
                            debug_log.error(
                                "ACTION: click has neither element_id nor coordinates",
                                extra={"stage": "Action"},
                            )
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
                        # Save cookies BEFORE navigation — if the bot crashes
                        # during page transition, session state is preserved
                        self.browser.save_session()
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
                    classification = _classify_click_failure(e)
                    debug_log.error(
                        f"ACTION FAILED: type={act.action_type} error={e} | "
                        f"classification={classification} | "
                        f"element_id={act.element_id} coords={act.coordinates}",
                        extra={"stage": "Action"},
                        exc_info=True,
                    )
                    action_ok = False
                    # ── Bounded, classified recovery (ONE re-acquisition,
                    #     never an identical repeat with stale coordinates) ──
                    if classification == "TARGET_OUTSIDE_VIEWPORT":
                        debug_log.warning(
                            "RECOVERY SKIPPED | geometry error is permanent "
                            "for these coordinates — not retrying",
                            extra={"stage": "Action"},
                        )
                    elif (
                        act.element_id is not None
                        and act.action_type == "click"
                    ):
                        # Re-acquire the element's LIVE center (after scroll)
                        # and click_coords with fresh, in-bounds coordinates.
                        live = self.browser.get_element_coords(act.element_id)
                        if live is not None:
                            try:
                                if self.browser.click_coords(*live):
                                    debug_log.debug(
                                        "RECOVERY attempt=1/1 | "
                                        "strategy=re-acquire-by-element_id | "
                                        f"element_id={act.element_id} | "
                                        "result=CLICK_SUCCESS | "
                                        f"live_coords={live}",
                                        extra={"stage": "Action"},
                                    )
                                    action_ok = True
                                else:
                                    debug_log.warning(
                                        "RECOVERY attempt=1/1 | "
                                        "strategy=re-project | "
                                        "result=CLICK_REFUSED | "
                                        f"coords={live}",
                                        extra={"stage": "Action"},
                                    )
                            except Exception as e2:
                                debug_log.error(
                                    "RECOVERY attempt=1/1 | "
                                    "strategy=re-acquire-by-element_id | "
                                    "result=CLICK_FAILED | "
                                    f"error={e2}",
                                    extra={"stage": "Action"},
                                    exc_info=True,
                                )
                        elif act.coordinates:
                            # Element is gone (stale id / re-rendered DOM /
                            # iframe content) — fall back ONCE to the
                            # AI-provided viewport coordinates. click_coords
                            # bounds-checks them, so an out-of-viewport guess
                            # is refused instead of throwing.
                            try:
                                if self.browser.click_coords(*act.coordinates):
                                    debug_log.warning(
                                        "RECOVERY attempt=1/1 | "
                                        "strategy=ai-coordinates | "
                                        f"element_id={act.element_id} not in "
                                        f"DOM — clicked coords={act.coordinates}",
                                        extra={"stage": "Action"},
                                    )
                                    action_ok = True
                                else:
                                    debug_log.warning(
                                        "RECOVERY: ai-coordinates "
                                        f"{act.coordinates} outside viewport "
                                        "— refusing",
                                        extra={"stage": "Action"},
                                    )
                            except Exception as e2:
                                debug_log.error(
                                    "RECOVERY attempt=1/1 | "
                                    "strategy=ai-coordinates | "
                                    f"result=CLICK_FAILED | error={e2}",
                                    extra={"stage": "Action"},
                                    exc_info=True,
                                )
                        else:
                            debug_log.warning(
                                f"RECOVERY: element_id={act.element_id} "
                                "not found and no fallback coordinates",
                                extra={"stage": "Action"},
                            )
                    elif act.coordinates and act.action_type == "click":
                        # No element_id — cannot re-identify the target.
                        # One bounded attempt: re-read a fresh element map
                        # and find the nearest interactive element.
                        fresh_map = self.browser.get_element_map()
                        nearest = self._nearest_element(
                            fresh_map, *act.coordinates
                        )
                        if nearest is not None:
                            try:
                                if self.browser.click_coords(
                                    nearest["x"], nearest["y"]
                                ):
                                    debug_log.debug(
                                        "RECOVERY attempt=1/1 | "
                                        "strategy=nearest-element | "
                                        "result=CLICK_SUCCESS | "
                                        f"reprojected={nearest['x']},{nearest['y']}",
                                        extra={"stage": "Action"},
                                    )
                                    action_ok = True
                                else:
                                    debug_log.warning(
                                        "RECOVERY: nearest element coords "
                                        "also outside viewport",
                                        extra={"stage": "Action"},
                                    )
                            except Exception as e2:
                                debug_log.error(
                                    "RECOVERY attempt=1/1 | "
                                    "strategy=nearest-element | "
                                    f"result=CLICK_FAILED | error={e2}",
                                    extra={"stage": "Action"},
                                    exc_info=True,
                                )
                        else:
                            debug_log.warning(
                                "RECOVERY: no element_id and no fresh "
                                "element near target coords — cannot re-project",
                                extra={"stage": "Action"},
                            )

                # Verify action had effect
                verified = None
                if act.action_type != "wait":
                    with StageTimer(debug_log, "verify_action"):
                        verified = self._verify_action(pre_fp)
                        if not verified:
                            debug_log.warning(
                                f"Action {act.action_type} had no effect",
                                extra={"stage": "Verify"},
                            )
                            debug_log.error(
                                f"VERIFICATION FAILED | type={act.action_type} | "
                                f"element_id={act.element_id} | "
                                f"coords={act.coordinates}",
                                extra={"stage": "Verify"},
                            )
                            # NO third identical click — stop touching this
                            # action.  The existing stuck detector and
                            # human_help escalation handle long-term recovery.
                if verified is not None:
                    verified_results.append(verified)
                action_results.append(action_ok)

        # --- Auto-click Next if needed ---
        self.guard.set_state(BotState.WAITING_FOR_NAVIGATION)
        with StageTimer(debug_log, "auto_next"):
            has_next = any(a.action_type == "next" for a in decision.actions)
            if not has_next:
                qtypes = ("single_choice", "multi_choice", "text", "dropdown", "grid")
                # Never auto-advance when NO answer action succeeded —
                # that would submit an unanswered question (secondary defect).
                if decision.question_type in qtypes and sum(action_results) > 0:
                    time.sleep(0.5)
                    if self.browser.click_next():
                        debug_log.info(
                            "Auto-clicked Next", extra={"stage": "Nav"}
                        )
                    else:
                        debug_log.warning(
                            "AUTO_NEXT FAILED | no next button found",
                            extra={"stage": "Nav"},
                        )
                elif decision.question_type in qtypes:
                    debug_log.warning(
                        "AUTO_NEXT SUPPRESSED | no answer action succeeded",
                        extra={"stage": "Nav"},
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
            f"actions_verified={sum(verified_results)}/{len(verified_results)} | "
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

        return False  # success → continue with timeout counter reset

    def stop(self):
        # Save cookies before stopping — preserves session state if bot crashes
        try:
            self.browser.save_session()
        except Exception:
            pass
        self.browser.stop()
