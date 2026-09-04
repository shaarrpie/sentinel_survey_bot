import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Literal

from dotenv import load_dotenv
load_dotenv()                            # must run BEFORE os.getenv

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from openai import OpenAI, APITimeoutError
from PIL import Image

# Local module now that the backend/ package name collision is gone.
# No guard: if this import fails, the server is broken and should say so.
from sentinel_heuristic import (
    heuristic_decide,
    heuristic_preanswer,
    detect_captcha,
    real_input_kind,
)

logger = logging.getLogger(__name__)

from provider_health import ProviderHealth


BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
API_KEY = os.getenv("API_KEY", "")
MODEL_NAME = os.getenv("MODEL_NAME", "")
AUTH_TOKEN = os.getenv("SENTINEL_TOKEN", "")
DEBUG_ON = os.getenv("SENTINEL_DEBUG", "0") == "1"
EXTENSION_ID = os.getenv("SENTINEL_EXTENSION_ID", "").strip()

provider_health = ProviderHealth(
    base_url=BASE_URL,
    api_key=API_KEY,
    model=MODEL_NAME,
)

# ── hardening: keyring, rate limit, origin whitelist ──────────────────

def get_api_key() -> str:
    try:
        import keyring
        key = keyring.get_password("sentinel_survey_bot", "api_key")
        if key:
            return key
    except Exception:
        pass
    return API_KEY

RATE_LIMIT_STORE = defaultdict(list)
RATE_LIMIT_MAX = int(os.getenv("SENTINEL_RATE_LIMIT_MAX", "10"))
RATE_LIMIT_WINDOW = int(os.getenv("SENTINEL_RATE_LIMIT_WINDOW", "60"))

def check_rate_limit(session_id: str) -> bool:
    now = time.time()
    window = RATE_LIMIT_STORE[session_id]
    window[:] = [t for t in window if now - t < RATE_LIMIT_WINDOW]
    if len(window) >= RATE_LIMIT_MAX:
        return False
    window.append(now)
    return True

ALLOWED_ORIGINS = [o.strip() for o in os.getenv("SENTINEL_ALLOWED_ORIGINS", "*").split(",") if o.strip()]

def check_origin(url: str) -> bool:
    if "*" in ALLOWED_ORIGINS:
        return True
    return any(url.startswith(o) for o in ALLOWED_ORIGINS)

CONFIRMATION_GATE = os.getenv("SENTINEL_CONFIRMATION_GATE", "0") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed: without a token every /decide, /learn and /config
    # request is open to anything on this machine that can reach 8000.
    if not AUTH_TOKEN:
        raise SystemExit(
            "[!] SENTINEL_TOKEN is not set — refusing to start. Put a long "
            "random secret in .env (see .env.example) and mirror it into "
            "extension/config.local.js."
        )
    if not API_KEY:
        logger.error("[!] API_KEY missing — running heuristic-only; the model path is dead until it is set")
    status = await asyncio.to_thread(provider_health.health, True)
    if status["api_ready"]:
        logger.info(
            "[+] AI provider ready: %s via %s (%sms)",
            status["model"], status["base_url"], status["latency_ms"],
        )
    else:
        logger.warning(
            "[!] AI provider unavailable: %s - heuristic fallback remains active",
            status["error"],
        )
    yield


app = FastAPI(lifespan=lifespan)


# ── trace bus (round two): ring buffer + /traces poll endpoint ──
from sentinel_traces import bus, omni_call, probe_omni  # noqa: E402
from sentinel_traces import router as trace_router      # noqa: E402
from sentinel_traces import trace_middleware            # noqa: E402
from panel_config import (get_panel_hub_domains, set_panel_hub_domains,
                          host_matches_hubs)
app.include_router(trace_router)
app.middleware("http")(trace_middleware)

# ── trace template ───────────────────────────────────────────────
# Lives in templates/trace.html; loaded once at import. The __PAYLOAD__
# token is replaced per trace.
TRACE_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "trace.html"
TRACE_TEMPLATE = TRACE_TEMPLATE_PATH.read_text(encoding="utf-8")
@app.middleware("http")
async def check_token(request: Request, call_next):
    # Fail closed on EVERY route (the old version left /status, /omni,
    # /traces, /debug/last and /config/panel-hub wide open — an unauth
    # POST /config/panel-hub {"url":""} cleared the login-wall stop).
    # /status stays public: it is the liveness probe launch.bat curls.
    if request.method == "OPTIONS":
        return await call_next(request)
    if request.url.path != "/status":
        provided = request.headers.get("X-Sentinel-Token", "")
        if not hmac.compare_digest(provided.encode("utf-8"),
                                   AUTH_TOKEN.encode("utf-8")):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)

if not EXTENSION_ID:
    raise SystemExit(
        "[!] SENTINEL_EXTENSION_ID must be set. "
        "Get your ID from chrome://extensions and pin it in .env."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"chrome-extension://{EXTENSION_ID}"],
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["X-Sentinel-Token", "Content-Type"],
)

MODEL = MODEL_NAME

# ── debugger config ──────────────────────────────────────────────
TRACES_DIR = Path(os.getenv("SENTINEL_TRACES", "traces"))
TRACE_ON = os.getenv("SENTINEL_TRACE", "1") == "1"
DRY_RUN = os.getenv("SENTINEL_DRY_RUN", "0") == "1"
TRACE_KEEP = int(os.getenv("SENTINEL_TRACE_KEEP", "400"))
TRACE_AGE_H = float(os.getenv("SENTINEL_TRACE_AGE_HOURS", "72"))  # 0 = no age cap
SNAP_MAX = int(os.getenv("SENTINEL_SNAP_MAX", "1200"))   # 0 = full
TRACES_DIR.mkdir(parents=True, exist_ok=True)

_last_debug: dict = {}                    # newest call, fully assembled

# 30s: a vision call with a 1280px JPEG under an 8s budget was timing out
# on every slow router hop. 30 + one retry stays under the extension's
# 45s /decide abort. (core.py and the extension use the same 30/45 pair.)
client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=30.0, max_retries=1) if API_KEY else None

# Trace-bus boot state: which LLM endpoint we're wired to (round two, §E)
if client is not None:
    logger.info(f"[omni] base_url={client.base_url} model={MODEL} key_set={bool(API_KEY)}")
    probe_omni({"provider": "openai-compat",
                "model": MODEL,
                "base_url": str(BASE_URL or ""),
                "api_key_set": bool(API_KEY)})
else:
    logger.error("[omni] client NOT LOADED — API_KEY missing")
    bus.record("sys", "state",
               "omni router NOT LOADED — API_KEY missing",
               {"api_key_set": False}, level="error")

# ── trace recorder ───────────────────────────────────────────────

def _prune_traces():
    # Two independent caps: a file count (TRACE_KEEP) AND an age (hours).
    # Count-only pruning meant a quiet install kept months of answers.
    try:
        now = time.time()
        cutoff = now - TRACE_AGE_H * 3600
        files = sorted(TRACES_DIR.glob("*.html"),
                       key=lambda p: p.stat().st_mtime)
        for p in files:
            mtime = p.stat().st_mtime
            if mtime < cutoff or p in files[:-TRACE_KEEP]:
                p.unlink()
    except Exception:
        logger.warning("[trace] prune failed", exc_info=True)

def _save_trace(rec: dict):
    """Archives one /decide call as a self-viewing HTML file."""
    try:
        rec_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        safe_url = re.sub(r"[^A-Za-z0-9.-]", "_", rec.get("url", "x"))[:60]
        qtype = rec.get("decision", {}).get("question_type", "q") \
            if isinstance(rec.get("decision"), dict) else "q"
        conf = rec.get("confidence")
        tag = f"{qtype}-{conf:.2f}" if isinstance(conf, (int, float)) else "err"
        fname = TRACES_DIR / f"{rec_id}_{safe_url}_{tag}.html"
        blob = urllib.parse.quote(
            json.dumps(rec, ensure_ascii=False), safe='')
        html_out = TRACE_TEMPLATE.replace("__PAYLOAD__", blob)
        fname.write_text(html_out, encoding="utf-8")
        _prune_traces()
        return str(fname.name)
    except Exception as e:
        print(f"[trace] save failed: {e}")
        return None

def record_trace(rec: dict):
    if TRACE_ON:
        _save_trace(rec)

# ── structured output (unchanged) ────────────────────────────────

class Action(BaseModel):
    action_type: Literal["click", "type", "select_option", "select_multi",
                         "scroll", "next", "wait", "human_help"]
    element_id: Optional[int] = None
    coordinates: Optional[List[int]] = None
    value: Optional[str] = None
    reasoning: str

class SurveyDecision(BaseModel):
    page_summary: str
    question_type: Literal["single_choice", "multi_choice", "dropdown",
                           "text", "grid", "mixed", "completion",
                           "navigation", "unknown"]
    confidence: float = Field(..., ge=0, le=1)
    actions: List[Action]
    memory_note: Optional[str] = None
    source: Optional[str] = None   # llm-structured | llm-raw | heuristic (r29)
    page_state: Optional[Literal["normal", "completed", "disqualified", "captcha"]] = None

# ── persona / memory / rules (unchanged) ─────────────────────────

from participant_profile import get_persona

MEMORY: dict = {}
SESSION_LAST_SEEN: dict = {}
LEARNED_RULES: List[str] = []
RULES_FILE = Path(__file__).resolve().with_name("learned_rules.json")
SESSION_TTL = 7200
_state_lock = threading.Lock()

def _prune_sessions():
    with _state_lock:
        now = time.time()
        stale = [sid for sid, ts in SESSION_LAST_SEEN.items()
                 if now - ts > SESSION_TTL]
        for sid in stale:
            MEMORY.pop(sid, None)
            SESSION_LAST_SEEN.pop(sid, None)

def load_rules():
    global LEARNED_RULES
    try:
        if RULES_FILE.exists():
            with _state_lock:
                LEARNED_RULES = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[rules] could not load %s", RULES_FILE, exc_info=True)

def save_rules():
    with _state_lock:
        try:
            RULES_FILE.write_text(json.dumps(LEARNED_RULES, indent=2), encoding="utf-8")
        except Exception:
            logger.warning("[rules] could not save %s", RULES_FILE, exc_info=True)

load_rules()

# ── panel-hub host matching (mirrors core.is_survey_router_hub) ──
def is_panel_hub(url: str) -> bool:
    """Thin alias — the implementation lives in panel_config
    (host_matches_hubs) and is asserted by the shared test tables
    next to the JS twin. Three matchers is how the trailing-dot
    and path-stripping variants crept in."""
    return host_matches_hubs(url)


# ── endpoints ────────────────────────────────────────────────────

# Payload bounds: /decide used to cap only the screenshot; a hostile or
# buggy page could still ship megabytes of elements/page_text into the
# prompt AND straight into the trace file.
MAX_ELEMENTS = 400
MAX_PAGE_TEXT = 20_000


def _heuristic_or_stop(rec, req, session_id, t0, cycle, reason):
    """Serve the deterministic fallback when the model path is off the
    table (provider down, no key, LLM timeout). One implementation —
    /decide used to carry three copies of this block."""
    try:
        heuristic = heuristic_decide(req.elements, req.page_text)
    except Exception:
        logger.warning("[heuristic] heuristic_decide raised (cycle %s)", cycle, exc_info=True)
        heuristic = None
    if heuristic and heuristic.get("actions"):
        bus.record("backend", "heuristic",
                   f"cycle {cycle}: LLM bypassed ({reason}) — heuristic serves "
                   f"({heuristic.get('page_summary', 'heuristic')})",
                   {"cycle": cycle, "reason": reason}, level="warn")
        decision = SurveyDecision(
            page_summary=heuristic.get("page_summary", "heuristic"),
            question_type=heuristic.get("question_type", "unknown"),
            confidence=heuristic.get("confidence", 0.2),
            actions=heuristic["actions"],
            memory_note=heuristic.get("memory_note"),
            source="heuristic",
        )
        return _finish(rec, req, session_id, decision, t0, path="heuristic")
        decision = SurveyDecision(
            page_summary=f"heuristic: {reason}, no actions",
            question_type="completion",
            confidence=0.2,
            actions=[],
            memory_note=f"provider_down: {reason}",
            source="heuristic",
            page_state="normal",
        )
        return _finish(rec, req, session_id, decision, t0, path="heuristic")



class DecideRequest(BaseModel):
    session_id: str
    screenshot_b64: str
    elements: List[dict]
    url: str
    page_text: str

@app.post("/decide")
async def decide(req: DecideRequest):
    t0 = time.time()
    cycle = uuid.uuid4().hex[:6]
    rec = {"ts": datetime.now().isoformat(), "session": req.session_id,
           "url": req.url, "dry_run": DRY_RUN, "cycle": cycle}
    shot_kb = len(req.screenshot_b64) * 3 / 4 / 1024   # b64 ≈ bytes × ¾
    bus.record("backend", "decide",
               f"cycle {cycle}: {len(req.elements)} elements, shot {shot_kb:.0f} KB",
               {"cycle": cycle, "elements": len(req.elements),
                "kb": round(shot_kb, 1), "url": req.url})

    # Panel-hub guard (mirrors core.is_survey_router_hub): landing on a
    # configured panel domain means the survey bounced back to a login wall.
    # STOP immediately — never let the AI fill out a login form. Domains are
    # user-configured via the extension popup (/config/panel-hub). Checked
    # FIRST so the stop is guaranteed even with an unconfigured LLM key.
    if is_panel_hub(req.url):
        bus.record("backend", "hub_stop",
                   f"cycle {cycle}: landed on panel hub — stopping",
                   {"cycle": cycle, "url": req.url}, level="warn")
        rec.update(path="panel_hub_stop", latency_ms=int((time.time() - t0) * 1000))
        record_trace(rec)
        return SurveyDecision(
            page_summary="Panel hub reached — survey terminated by the panel "
                         "(login wall). Bot stopped for human re-routing.",
            question_type="completion",
            confidence=1.0,
            actions=[],
            memory_note="panel_hub_stop",
            page_state="disqualified",
        )

    if not check_origin(req.url):
        raise HTTPException(status_code=403, detail=f"origin not allowed: {req.url}")

    if not check_rate_limit(req.session_id):
        raise HTTPException(status_code=429, detail="rate limit exceeded")

    _prune_sessions()
    session_id = req.session_id
    with _state_lock:
        SESSION_LAST_SEEN[session_id] = time.time()
        if session_id not in MEMORY:
            MEMORY[session_id] = []

    # ── Compress screenshot BEFORE size gate ───────────────────────
    # Raw PNGs from extensions can exceed 4MB; compress first, then check.
    screenshot_b64 = req.screenshot_b64
    try:
        img_data = base64.b64decode(req.screenshot_b64)
        img = Image.open(io.BytesIO(img_data))
        if img.width > 1280:
            ratio = 1280 / img.width
            img = img.resize((1280, int(img.height * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            screenshot_b64 = base64.b64encode(buf.getvalue()).decode()
        else:
            # Re-encode as JPEG to reduce size even if not resized
            buf = io.BytesIO()
            img = img.convert("RGB") if img.mode == "RGBA" else img
            img.save(buf, format="JPEG", quality=70)
            screenshot_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        screenshot_b64 = req.screenshot_b64

    if len(screenshot_b64) > 8_000_000:
        rec.update(path="error", error="screenshot too large",
                   latency_ms=int((time.time() - t0) * 1000))
        record_trace(rec)
        raise HTTPException(status_code=413, detail="screenshot too large")
    if len(req.elements) > MAX_ELEMENTS:
        raise HTTPException(status_code=413,
                            detail=f"too many elements ({len(req.elements)} > {MAX_ELEMENTS})")
    if len(req.page_text) > MAX_PAGE_TEXT:
        raise HTTPException(status_code=413,
                            detail=f"page text too long ({len(req.page_text)} > {MAX_PAGE_TEXT})")

    # ── provider health gate — the model is PRIMARY; the heuristic only
    #    serves when the provider is genuinely unavailable (the contract
    #    sentinel_heuristic.py's docstring has always claimed). Health is
    #    cached 30s, so this costs at most one local /models round trip
    #    per half-minute on the request path.
    if client is None:
        return _heuristic_or_stop(rec, req, session_id, t0, cycle,
                                  reason="no API key configured")
    health = await asyncio.to_thread(provider_health.health)
    if not health["api_ready"]:
        return _heuristic_or_stop(rec, req, session_id, t0, cycle,
                                  reason=f"provider unavailable: {health['error']}")

    # ── model path: screenshot already compressed above ─────────────
    img_mime = "image/jpeg"

    # ── sniper mode: for dense lists (>50 options), ask for keywords only ──
    # (The old trigger also matched three literal strings from
    # survey-test.html — test fixtures living in production.)
    if len(req.elements) > 50:
        sniper_prompt = f"Question: \"{req.page_text[:1500]}\"\nThis is a large single-choice list. Look at your Persona and tell me exactly what 1-2 keywords I should search for in the list to find your answer (e.g., 'Mumbai', 'Maharashtra', or '1994'). Reply ONLY with the keywords, nothing else."
        try:
            with omni_call(provider="openai-compat", model=MODEL,
                           cycle=rec.get("cycle")):
                resp = client.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "user", "content": sniper_prompt}],
                    max_tokens=50, temperature=0.2
                )
            keyword = resp.choices[0].message.content.strip().strip("*'\"` ")
            # Guard rails: an empty/1-char keyword matches element 0 and
            # friends; a keyword that matches several options is a coin
            # flip. Both fall through to the full model path instead.
            if len(keyword) >= 3:
                matches = [e for e in req.elements
                           if keyword.lower() in (e.get("text") or "").lower()]
                if len(matches) == 1:
                    match = matches[0]
                    decision = SurveyDecision(
                        page_summary=f"sniper: {keyword}",
                        question_type="single_choice",
                        confidence=0.9,
                        actions=[Action(action_type="click", element_id=match["id"],
                                        reasoning=f"Sniper hit: {keyword}")],
                        memory_note=f"sniper selected {match.get('text','')}"
                    )
                    return _finish(rec, req, session_id, decision, t0, path="sniper")
                if len(matches) > 1:
                    logger.warning("[sniper] keyword %r matched %d options — "
                                   "refusing to guess", keyword, len(matches))
        except Exception:
            logger.warning("[sniper] path failed — falling through to full model call",
                           exc_info=True)

    # ── image trap OCR: if an image is present and a text input nearby, try OCR ──
    img_text = ""
    try:
        imgs = [e for e in req.elements if e.get("tag") == "img"]
        if imgs and req.screenshot_b64:
            img_b64 = req.screenshot_b64
            if img_b64:
                img_data = base64.b64decode(img_b64)
                img = Image.open(io.BytesIO(img_data))
                w, h = img.size
                for ie in imgs:
                    ix, iy = ie.get("x", 0), ie.get("y", 0)
                    cw, ch = 320, 120
                    left = max(0, int(ix - cw/2))
                    top = max(0, int(iy - ch/2))
                    right = min(w, int(ix + cw/2))
                    bottom = min(h, int(iy + ch/2))
                    if right > left and bottom > top:
                        crop = img.crop((left, top, right, bottom))
                        try:
                            import pytesseract
                            img_text = pytesseract.image_to_string(crop).strip()
                        except Exception:
                            img_text = ""
                        if img_text:
                            break
    except Exception:
        img_text = ""

    if img_text:
        req.page_text += f"\n[IMAGE TEXT: {img_text}]"

    # ── heuristic pre-answer: deterministic fields are answered here ──
    # The LLM only sees judgment-needing elements, which is cheaper and
    # avoids the truncation cliff (req.elements[:80]).
    pre = heuristic_preanswer(req.elements, req.page_text)
    heuristic_actions = pre["actions"]
    remaining_elements = pre["remaining"]

    # ── page_state detection (demotes to heuristic fallback if bad state) ──
    page_state = "normal"
    lower_url = req.url.lower()
    lower_text = req.page_text.lower()
    if is_panel_hub(req.url):
        page_state = "disqualified"
    elif any(k in lower_text for k in ("disqualified", "screened out", "screenout",
                                         "do not qualify", "does not qualify", "quota full",
                                         "quota is full", "reward=0", "terminated")):
        page_state = "disqualified"
    elif any(k in lower_text for k in ("thank you", "your responses have been recorded",
                                        "gracias por completar", "completed", "finished")):
        page_state = "completed"
    elif any(e.get("tag") == "iframe" and "recaptcha" in (e.get("src") or "").lower()
             for e in req.elements):
        page_state = "captcha"
    elif detect_captcha(req.page_text):
        page_state = "captcha"

    if page_state == "completed":
        return SurveyDecision(
            page_summary="Page state: completed",
            question_type="completion",
            confidence=1.0,
            actions=[],
            memory_note="page_state_completed",
            source="heuristic",
            page_state=page_state,
        )
    if page_state == "disqualified":
        return SurveyDecision(
            page_summary="Page state: disqualified",
            question_type="completion",
            confidence=1.0,
            actions=[],
            memory_note="page_state_disqualified",
            source="heuristic",
            page_state=page_state,
        )
    if page_state == "captcha":
        return SurveyDecision(
            page_summary="Page state: captcha detected",
            question_type="completion",
            confidence=1.0,
            actions=[Action(action_type="human_help", reasoning="CAPTCHA detected")],
            memory_note="page_state_captcha",
            source="heuristic",
            page_state=page_state,
        )

    # If heuristic pre-answered everything, skip the LLM entirely
    if not remaining_elements and heuristic_actions:
        return SurveyDecision(
            page_summary=f"heuristic pre-answered {len(heuristic_actions)} deterministic action(s)",
            question_type="mixed",
            confidence=0.5,
            actions=[Action(**a) for a in heuristic_actions],
            memory_note=None,
            source="heuristic",
            page_state=page_state,
        )

    # ── iframe hint ──
    iframe_hint = ""
    if any(e.get("frame") for e in remaining_elements):
        iframe_hint = ("\nNOTE: Some elements are inside an iframe (they carry a "
                       "frame flag). Answer the main page first, then the iframe's question.")

    with _state_lock:
        memory_block = ("\n".join(MEMORY[session_id][-12:])
                        if MEMORY[session_id] else "None yet.")
        rules_block = ("\n".join(f"- {r}" for r in LEARNED_RULES)
                       if LEARNED_RULES else "None yet.")

    # Chunk remaining elements if still over budget (multiple /decide calls
    # instead of one amputated one — cheaper and correct).
    llm_elements = remaining_elements
    if len(llm_elements) > MAX_ELEMENTS:
        llm_elements = llm_elements[:MAX_ELEMENTS]

    prompt = f"""You are a survey completion assistant. These are the ONLY elements that need your judgment — deterministic fields (obvious radios, empty selects, empty text inputs) have already been answered by a heuristic engine.

Rules:
- Answer ONLY the elements listed below. Do NOT re-answer fields the heuristic already handled.
- Use click for radio/checkbox/button, type for text inputs, select_option for dropdowns.
- For radios, pick the best option per group. For checkboxes, select all that apply unless "none" is appropriate.
- To advance: click the advance/submit button directly. NEVER emit a "next" action.
- If the page appears fully answered and you see an advance button, click it directly.
- Do not ask for clarification or request more info.
- Return JSON matching the SurveyDecision schema.

Page state: {page_state}
URL: {req.url}
Page text: {req.page_text[:3000]}
Elements: {json.dumps(llm_elements)}
Memory: {memory_block}
Rules: {rules_block}{iframe_hint}

Return JSON matching the SurveyDecision schema with actions for ONLY the judgment-needing elements above."""

    rec["prompt"] = prompt
    with _state_lock:
        rec["rules"] = list(LEARNED_RULES)
        rec["memory_ctx"] = list(MEMORY[session_id][-12:])
    rec["model"] = MODEL
    _last_debug.update({"ts": rec["ts"], "url": req.url, "prompt": prompt,
                        "persona": get_persona(), "model": MODEL})

    try:
        try:
            logger.info(f"[omni] structured -> base_url={client.base_url} model={MODEL}")
            with omni_call(provider="openai-compat", model=MODEL,
                           cycle=rec.get("cycle")):
                resp = await asyncio.to_thread(
                    client.chat.completions.parse,
                    model=MODEL,
                    messages=[
                            {"role": "system", "content": get_persona()},
                        {"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url",
                             "image_url": {"url": f"data:{img_mime};base64,{screenshot_b64}"}},
                        ]},
                    ],
                    response_format=SurveyDecision,
                    max_tokens=2500,
                    temperature=0.2,
                )
            decision = resp.choices[0].message.parsed
            if decision is None:
                raise ValueError("Model returned no parseable decision")
            decision = decision.model_copy(update={"source": "llm-structured"})
            logger.info(f"[omni] structured OK -> {decision.question_type} ({decision.confidence})")

        except Exception as parse_err:
            err_type = type(parse_err).__name__
            err_msg = str(parse_err)
            logger.warning(f"[omni] structured FAILED: {err_type} — {err_msg} — base_url={client.base_url}")
            bus.record("omni", "parse_error",
                       f"structured failed: {err_type}: {err_msg}",
                       {"cycle": rec.get("cycle"), "err": err_msg[:300],
                        "base_url": str(client.base_url)},
                       level="warn")
            try:
                logger.info(f"[omni] raw fallback -> base_url={client.base_url} model={MODEL}")
                with omni_call(provider="openai-compat", model=MODEL,
                               cycle=rec.get("cycle")):
                    resp = await asyncio.to_thread(
                        client.chat.completions.create,
                        model=MODEL,
                        messages=[
                        {"role": "system", "content": get_persona()},
                            {"role": "user", "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url",
                                 "image_url": {"url": f"data:{img_mime};base64,{screenshot_b64}"}},
                            ]},
                        ],
                        max_tokens=2500,
                        temperature=0.2,
                    )
                raw = resp.choices[0].message.content.strip()
                logger.info(f"[omni] raw fallback OK -> {len(raw)} chars")
                if "```json" in raw:
                    raw = raw.split("```json")[1].split("```")[0]
                decision = SurveyDecision.model_validate_json(raw)
                decision = decision.model_copy(update={"source": "llm-raw"})
                logger.info(f"[omni] raw parsed -> {decision.question_type} ({decision.confidence})")
            except Exception as fallback_err:
                ftype = type(fallback_err).__name__
                fmsg = str(fallback_err)
                logger.warning(f"[omni] raw fallback FAILED: {ftype} — {fmsg}")
                rec.update(path="error", error=f"Parse fallback failed: {fallback_err}",
                           latency_ms=int((time.time() - t0) * 1000))
                bus.record("omni", "error",
                           "fallback failed: "
                           + (raw[:400] if "raw" in locals() else "<no raw captured>"),
                           {"cycle": rec.get("cycle"), "err": fmsg[:300],
                            "base_url": str(client.base_url)},
                           level="error")
                record_trace(rec)
                raise HTTPException(status_code=502, detail=str(fallback_err))

        if DRY_RUN:
            print(f"[DRY] {decision.question_type} "
                  f"({decision.confidence:.2f}) -> "
                  + ", ".join(f"{a.action_type}:{a.element_id}"
                              for a in decision.actions))

        # Combine heuristic pre-answers with LLM judgment actions
        combined_actions = list(heuristic_actions) + [a.model_dump() for a in decision.actions]
        decision = decision.model_copy(update={
            "actions": [Action(**a) for a in combined_actions],
            "page_state": page_state,
        })
        if heuristic_actions:
            decision = decision.model_copy(update={
                "page_summary": decision.page_summary + f" (+ {len(heuristic_actions)} heuristic)"
            })

        rec["snap_b64"] = (screenshot_b64[:SNAP_MAX]
                           + ("__TRUNC__" if SNAP_MAX and len(screenshot_b64) > SNAP_MAX else "")) \
            if SNAP_MAX else screenshot_b64
        rec["snap_mime"] = img_mime
        rec["snap_truncated"] = bool(SNAP_MAX) and len(screenshot_b64) > SNAP_MAX
        decision = _finish(rec, req, session_id, decision, t0, path="model")
        return decision

    except HTTPException:
        raise
    except APITimeoutError as e:
        logger.warning(f"[!] LLM timeout: {e} — heuristic fallback")
        bus.record("backend", "timeout",
                   f"cycle {cycle}: LLM timeout — heuristic fallback",
                   {"cycle": cycle, "err": str(e)[:300]}, level="warn")
        try:
            pre = heuristic_preanswer(req.elements, req.page_text)
            heuristic_actions = pre["actions"]
        except Exception:
            logger.warning("[heuristic] heuristic_preanswer raised during timeout fallback",
                           exc_info=True)
            heuristic_actions = []
        if heuristic_actions:
            return _finish(rec, req, session_id, SurveyDecision(
                page_summary=f"heuristic pre-answered {len(heuristic_actions)} action(s) after timeout",
                question_type="mixed",
                confidence=0.4,
                actions=[Action(**a) for a in heuristic_actions],
                memory_note="timeout_heuristic_preanswer",
                source="heuristic",
                page_state=page_state,
            ), t0, path="heuristic")
        decision = SurveyDecision(
            page_summary="heuristic: timeout, no actions",
            question_type="navigation",
            confidence=0.2,
            actions=[Action(action_type="next", reasoning="llm timeout — navigate")],
            memory_note="timeout_no_actions",
            source="heuristic",
            page_state=page_state,
        )
        return _finish(rec, req, session_id, decision, t0, path="heuristic")
    except Exception as e:
        rec.update(path="error", error=f"{type(e).__name__}: {e}",
                   latency_ms=int((time.time() - t0) * 1000))
        record_trace(rec)
        raise HTTPException(status_code=500, detail=str(e))


def _finish(rec, req, session_id, decision, t0, path):
    """Shared tail: memory bookkeeping, trace archive, dry-run guard."""
    if decision.memory_note:
        with _state_lock:
            MEMORY[session_id].append(decision.memory_note)
            if len(MEMORY[session_id]) > 50:
                MEMORY[session_id] = MEMORY[session_id][-50:]
    latency_ms = int((time.time() - t0) * 1000)
    rec.update(path=path,
               confidence=decision.confidence,
               decision=decision.model_dump(),
               page_text=req.page_text[:4000],
               elements=req.elements,
               latency_ms=latency_ms)
    bus.record("backend", "decision",
               f"cycle {rec.get('cycle')}: {path} -> " +
               ", ".join(f"{a.action_type}:"
                         f"{a.element_id if a.element_id is not None else '-'}"
                         for a in decision.actions[:6]),
               {"cycle": rec.get("cycle"), "path": path,
                "qtype": decision.question_type,
                "confidence": decision.confidence,
                "actions": [a.model_dump() for a in decision.actions]},
               level="info", ms=float(latency_ms))
    record_trace(rec)
    if DRY_RUN:
        decision.actions = []          # phantom hands: nothing executes
    return decision


class LearnRequest(BaseModel):
    memory: List[str]

@app.post("/learn")
async def learn_rule(req: LearnRequest):
    if client is None:
        raise HTTPException(status_code=503, detail="API key not set")
    if not req.memory:
        return {"learned": None}
    recent = "\n".join(req.memory[-6:])
    prompt = (f"Disqualified after:\n{recent}\n\n"
              "1-sentence rule starting with NEVER or ALWAYS:")
    try:
        with omni_call(provider="openai-compat", model=MODEL):
            resp = await asyncio.to_thread(
                client.chat.completions.create,
                model=MODEL, messages=[{"role": "user", "content": prompt}],
                max_tokens=100, temperature=0.3,
            )
        rule = resp.choices[0].message.content.strip()
        with _state_lock:
            if rule and rule not in LEARNED_RULES:
                LEARNED_RULES.append(rule)
                save_rules()
        bus.record("backend", "learn",
                   f"rule stored: {rule[:80]}",
                   {"rule": rule}, level="info")
        return {"learned": rule}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/status")
async def status():
    provider = await asyncio.to_thread(provider_health.health)
    with _state_lock:
        out = {
            "status": "ok",
            "backend_ready": client is not None,
            "dry_run": DRY_RUN,
            "trace_on": TRACE_ON,
            "traces": len(list(TRACES_DIR.glob("*.html"))),
            "memory_count": sum(len(v) for v in MEMORY.values()),
            "sessions": len(MEMORY),
            "rules_count": len(LEARNED_RULES),
        }
    bus.set_omni_health(
        loaded=provider["api_ready"],
        model=provider["model"],
        base_url=provider["base_url"],
        error=provider["error"],
        api_key_set=provider["api_key_set"],
    )
    out.update(bus.snapshot())
    out["provider"] = provider
    out["router"] = provider
    out["brain"] = provider["mode"]
    out["panel_hubs"] = list(get_panel_hub_domains())
    return out

# ── omni detail panel (round eleven: live router visibility) ─────

def _key_hint(key: str):
    """Key fingerprint for the debug panel — a hash prefix only.
    (The old form leaked first-6 + last-4 of the real key, which is more
    than an unauthenticated-ish panel needs.)"""
    if not key:
        return None
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _omni_detail() -> dict:
    """Payload for GET /omni — tolerates fields the omni layer doesn't
    populate (they render as '—' in the console)."""
    o = bus.get_omni()   # deep copy under the bus lock — no torn reads
    lat = o.get("latency_history", [])
    srt = sorted(lat)
    models = {k: {**v, "avg_ms": round(v["total_ms"] / max(1, v["calls"]), 1)}
              for k, v in o.get("models", {}).items()}
    calls = o.get("calls", 0)
    errors = o.get("errors", 0)
    return {
        "connected": bool(o.get("loaded")),
        "provider": o.get("provider"),
        "model": o.get("model"),
        "base_url": o.get("base_url"),
        "api_key_set": bool(o.get("api_key_set")),
        "key_hint": _key_hint(API_KEY),
        "fallback_chain": o.get("fallback_chain", []),
        "calls": calls,
        "errors": errors,
        "error_rate": round(errors / max(1, calls) * 100, 1),
        "tokens_in": o.get("tokens_in", 0),
        "tokens_out": o.get("tokens_out", 0),
        "latency": lat,
        "p50_ms": srt[len(srt) // 2] if srt else None,
        "p95_ms": srt[int(len(srt) * 0.95)] if srt else None,
        "last_ms": o.get("last_ms"),
        "per_model": models,
        "recent_calls": list(reversed(o.get("recent_calls", []))),
        "last_ping": o.get("last_ping"),
    }


@app.get("/omni")
async def omni_detail():
    return _omni_detail()


@app.get("/omni/ping")
async def omni_ping():
    """Live probe: one tiny completion so the console proves END-TO-END
    reachability (not just HTTP layer up) and the model self-identifies."""
    t0 = time.time()
    try:
        if client is None:
            raise RuntimeError("client not configured — API_KEY missing")
        with omni_call(provider="openai-compat", model=MODEL, cycle="ping"):
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user",
                           "content": "Reply with exactly: pong - <your model name>"}],
                max_tokens=40, temperature=0.0,
            )
        reply = (resp.choices[0].message.content or "").strip()
        ms = (time.time() - t0) * 1000
        ping = {"t": time.time(), "ms": round(ms, 1), "ok": True,
                "reply": str(reply)[:120]}
        bus.note_omni_ping(ping)
        bus.record("omni", "ping",
                   f"ping ok in {ms:.0f} ms — {str(reply)[:60]}",
                   dict(ping), ms=ms)
    except Exception as e:
        ms = (time.time() - t0) * 1000
        ping = {"t": time.time(), "ms": round(ms, 1), "ok": False,
                "reply": str(e)[:120]}
        bus.note_omni_ping(ping)
        bus.record("omni", "ping",
                   f"ping FAILED after {ms:.0f} ms — {e}",
                   dict(ping), level="error", ms=ms)
    return ping


# ── panel hub configuration (edited from the extension popup) ────

MAX_PANEL_HUBS = 50


class PanelHubRequest(BaseModel):
    url: str = Field(..., description="Panel link/domain; empty string clears all")
    remove: bool = Field(False, description="Set true to remove this domain instead of adding it")


@app.get("/config/panel-hub")
async def read_panel_hubs():
    return {"panel_hubs": list(get_panel_hub_domains())}


@app.post("/config/panel-hub")
async def write_panel_hub(req: PanelHubRequest):
    """Append, remove-one, or nuke. (There used to be no way to remove a
    single domain — a typo'd hub could only be cleared out with the whole
    safety list.)"""
    current = list(get_panel_hub_domains())
    if not req.url.strip():
        domains = set_panel_hub_domains([])
        bus.record("backend", "config", "panel hubs cleared",
                   {"panel_hubs": []}, level="info")
        return {"panel_hubs": list(domains)}

    from panel_config import extract_host
    target = extract_host(req.url)
    if req.remove:
        if not target:
            raise HTTPException(status_code=400, detail="unparseable domain")
        domains = set_panel_hub_domains([d for d in current if d != target])
        bus.record("backend", "config", f"panel hub removed: {target}",
                   {"panel_hubs": list(domains)}, level="info")
        return {"panel_hubs": list(domains)}

    if not target:
        raise HTTPException(status_code=400, detail="unparseable domain")
    if target not in current and len(current) >= MAX_PANEL_HUBS:
        raise HTTPException(status_code=400,
                            detail=f"panel hub list is full (max {MAX_PANEL_HUBS})")
    domains = set_panel_hub_domains(current + [req.url])
    bus.record("backend", "config", f"panel hub added: {target}",
               {"panel_hubs": list(domains)}, level="info")
    return {"panel_hubs": list(domains)}

# ── (debug endpoint removed — was leaking persona/profile data) ──

if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("[!] Set API_KEY in .env "
                         "or the environment before starting.")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)



