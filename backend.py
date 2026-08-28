import base64
import html
import io
import json
import logging
import os
import re
import time
import urllib.parse
import uuid
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

try:
    from backend.sentinel_heuristic import heuristic_decide
except Exception:
    heuristic_decide = None

logger = logging.getLogger(__name__)

import asyncio
from contextlib import asynccontextmanager

from provider_health import ProviderHealth


BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
API_KEY = os.getenv("API_KEY", "")
MODEL_NAME = os.getenv("MODEL_NAME", "")

provider_health = ProviderHealth(
    base_url=BASE_URL,
    api_key=API_KEY,
    model=MODEL_NAME,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
from panel_config import get_panel_hub_domains, set_panel_hub_domains
app.include_router(trace_router)
app.middleware("http")(trace_middleware)

AUTH_TOKEN = os.getenv("SENTINEL_TOKEN", "")

# ── trace template ───────────────────────────────────────────────
# NOTE: this string must live at MODULE LEVEL in backend.py (it is
# referenced by _save_trace). Keep it above _save_trace, exactly
# as shown — the __PAYLOAD__ token is replaced per trace.

TRACE_TEMPLATE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="script-src 'self' 'unsafe-inline'"><title>Sentinel trace</title>
<style>
:root{ --bg0:#0d1320; --bg1:#121b2d; --line:#20304c; --ink:#dbe6f5;
       --dim:#7e90ac; --faint:#4d5f7c; --ok:#3ddc84; --err:#ff6161;
       --warn:#ffb347; --ai:#59c2ff; --act:#9db4d6; }
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;padding:16px;color:var(--ink);
  font-family:"Segoe UI","Helvetica Neue",sans-serif;
  background:
   radial-gradient(460px 240px at -10% -20%, rgba(89,194,255,.10), transparent 60%),
   radial-gradient(420px 260px at 112% 122%, rgba(61,220,132,.08), transparent 62%),
   repeating-linear-gradient(0deg, transparent 0 23px, rgba(126,144,172,.05) 23px 24px),
   linear-gradient(160deg, var(--bg1), var(--bg0));}
.display{font-family:Bahnschrift,"Avenir Next Condensed","Arial Narrow",sans-serif}
.mono{font-family:ui-monospace,Consolas,monospace}
header{display:flex;align-items:center;gap:14px;padding:6px 4px 14px;border-bottom:1px solid var(--line)}
.word{font-size:26px;font-weight:700;letter-spacing:.14em}
.chip{font-size:10px;letter-spacing:.08em;padding:3px 8px;border-radius:4px;border:1px solid var(--line);color:var(--dim)}
.chip.ok{color:var(--ok);border-color:rgba(61,220,132,.4)}
.chip.ai{color:var(--ai);border-color:rgba(89,194,255,.4)}
.chip.err{color:var(--err);border-color:rgba(255,97,97,.4)}
.lat{margin-left:auto;text-align:right;font-size:12px;color:var(--dim)}
.lat b{display:block;font-size:18px;color:var(--ink)}
.grid{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,4fr);gap:14px;margin-top:14px}
.panel{border:1px solid var(--line);border-radius:6px;background:rgba(8,12,21,.7);padding:12px}
.panel h2{font-size:9px;letter-spacing:.26em;color:var(--faint);margin-bottom:9px}
pre{white-space:pre-wrap;word-break:break-word;font-size:11px;line-height:1.6;color:#c3d2e8;max-height:420px;overflow:auto}
img.shot{width:100%;border:1px solid var(--line);border-radius:4px;display:block}
table{width:100%;border-collapse:collapse;font-size:10.5px}
th{font-size:8.5px;letter-spacing:.18em;color:var(--faint);text-align:left;padding:4px 6px;border-bottom:1px solid var(--line)}
td{padding:4px 6px;border-bottom:1px solid rgba(32,48,76,.5);color:#c3d2e8;font-family:ui-monospace,Consolas,monospace;vertical-align:top}
tr.hot td{color:var(--warn);background:rgba(255,179,71,.06)}
tr.hot td:first-child{box-shadow:inset 3px 0 0 var(--warn)}
.foot{margin-top:12px;font-size:9px;color:var(--faint)}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
</style></head>
<body>
<header>
  <div class="word display" id="word">TRACE</div>
  <div id="chips" style="display:flex;gap:6px;flex-wrap:wrap"></div>
  <div class="lat mono"><b id="lat">–</b>ms latency</div>
</header>
<div class="grid">
  <div>
    <div class="panel"><h2>SCREENSHOT (as sent, may be truncated)</h2><div id="shot"></div></div>
    <div class="panel" style="margin-top:14px"><h2>PAGE TEXT (first 4000)</h2><pre id="ptext"></pre></div>
    <div class="panel" style="margin-top:14px"><h2>ELEMENT MAP</h2><div id="emap"></div></div>
  </div>
  <div>
    <div class="panel"><h2>DECISION</h2><pre id="dec"></pre></div>
    <div class="panel" style="margin-top:14px"><h2>ASSEMBLED PROMPT</h2><pre id="prom"></pre></div>
    <div class="panel" style="margin-top:14px"><h2>LEARNED RULES IN EFFECT</h2><pre id="rul"></pre></div>
    <div class="panel" style="margin-top:14px"><h2>SESSION MEMORY CONTEXT</h2><pre id="mem"></pre></div>
  </div>
</div>
<div class="foot mono" id="foot"></div>
<script>
const R = JSON.parse(decodeURIComponent("__PAYLOAD__"));
document.title = "trace " + (R.ts || "");
const chip = (t, c) => `<span class="chip ${c||''}">${t}</span>`;
document.getElementById("chips").innerHTML =
  chip(R.path, R.path === "error" ? "err" : (R.path === "model" ? "ai" : "ok")) +
  chip((R.question_type || (R.decision && R.decision.question_type) || "–")) +
  (R.dry_run ? chip("DRY RUN", "err") : "") +
  (R.snap_truncated ? chip("image truncated in trace", "") : "");
document.getElementById("word").textContent =
  R.path === "heuristic" ? "HEURISTIC" : (R.path === "model" ? "MODEL CALL" : (R.path === "error" ? "FAILURE" : "TRACE"));
document.getElementById("lat").textContent = R.latency_ms != null ? R.latency_ms : "–";
const shot = document.getElementById("shot");
if (R.snap_b64 && !R.snap_truncated) {
  const img = new Image();
  img.className = "shot"; img.src = "data:" + (R.snap_mime||"image/jpeg") + ";base64," + R.snap_b64;
  shot.appendChild(img);
} else if (R.snap_b64) {
  shot.innerHTML = '<div style="font-size:11px;color:var(--warn)">stored prefix only (' +
    R.snap_b64.length + ' chars) — raise SENTINEL_SNAP_MAX to embed the full frame</div>';
} else { shot.innerHTML = '<div style="font-size:11px;color:var(--faint)">no image on this path</div>'; }
document.getElementById("ptext").textContent = R.page_text || "–";
const hot = new Set(((R.decision||{}).actions||[]).map(a => a.element_id).filter(v => v !== null && v !== undefined));
let rows = "<table><tr><th>ID</th><th>TAG</th><th>TYPE</th><th>TEXT</th><th>X,Y</th></tr>";
(R.elements||[]).forEach(e => {
  const cls = hot.has(e.id) ? ' class="hot"' : '';
  rows += `<tr${cls}><td>${e.id}</td><td>${e.tag||''}</td><td>${e.type||''}</td>` +
    `<td>${(e.text||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')}</td><td>${e.x||''},${e.y||''}</td></tr>`;
});
document.getElementById("emap").innerHTML = rows + "</table>";
document.getElementById("dec").textContent = R.decision ? JSON.stringify(R.decision, null, 2) : (R.error || "–");
document.getElementById("prom").textContent = R.prompt || "– (heuristic path: no model prompt)";
document.getElementById("rul").textContent = (R.rules||[]).join("\n") || "– none –";
document.getElementById("mem").textContent = (R.memory_ctx||[]).join("\n") || "– empty –";
document.getElementById("foot").textContent =
  (R.ts||"") + "  ·  session " + (R.session||"–") + "  ·  " + (R.url||"") +
  (R.model ? "  ·  model " + R.model : "");
</script></body></html>"""

@app.middleware("http")
async def check_token(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    if request.url.path in ("/decide", "/learn"):
        if AUTH_TOKEN and request.headers.get("X-Sentinel-Token") != AUTH_TOKEN:
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://.*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

MODEL = MODEL_NAME

# ── debugger config ──────────────────────────────────────────────
TRACES_DIR = Path(os.getenv("SENTINEL_TRACES", "traces"))
TRACE_ON = os.getenv("SENTINEL_TRACE", "1") == "1"
DRY_RUN = os.getenv("SENTINEL_DRY_RUN", "0") == "1"
TRACE_KEEP = int(os.getenv("SENTINEL_TRACE_KEEP", "400"))
SNAP_MAX = int(os.getenv("SENTINEL_SNAP_MAX", "1200"))   # 0 = full
TRACES_DIR.mkdir(parents=True, exist_ok=True)

_last_debug: dict = {}                    # newest call, fully assembled

client = OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=8.0, max_retries=1) if API_KEY else None

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
    try:
        files = sorted(TRACES_DIR.glob("*.html"),
                       key=lambda p: p.stat().st_mtime)
        for p in files[:-TRACE_KEEP]:
            p.unlink()
    except Exception:
        pass

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
                           "text", "grid", "mixed", "completion", "unknown"]
    confidence: float = Field(..., ge=0, le=1)
    actions: List[Action]
    memory_note: Optional[str] = None
    source: Optional[str] = None   # llm-structured | llm-raw | heuristic (r29)

# ── persona / memory / rules (unchanged) ─────────────────────────

PERSONA = """You are a real human taking online surveys. Persona:
- Female, 32, Mumbai (Maharashtra), India, Pin 400001
- B.Tech/MBA, IT Director, household income ₹12L
- Married, 2 kids (4, 7), caregiver for elderly parent (diabetes, hypertension)
- 2 dogs, 2 cats, urban homeowner
- Primary decision-maker for FMCG, groceries, education, appliances

STRICT RULES:
1. NEVER say you work in Marketing/Advertising/Market Research.
2. ALWAYS express extreme interest in products/topics.
3. For multi-select, select ALMOST ALL except obvious fake/trap items.
4. ALWAYS claim to be the sole/final decision-maker.
5. If survey targets a niche, PIVOT persona to match it.
6. Follow instruction traps literally.
7. Never contradict previous answers."""

MEMORY: dict = {}
SESSION_LAST_SEEN: dict = {}
LEARNED_RULES: List[str] = []
RULES_FILE = "learned_rules.json"
SESSION_TTL = 7200

def _prune_sessions():
    now = time.time()
    stale = [sid for sid, ts in SESSION_LAST_SEEN.items()
             if now - ts > SESSION_TTL]
    for sid in stale:
        MEMORY.pop(sid, None)
        SESSION_LAST_SEEN.pop(sid, None)

def load_rules():
    global LEARNED_RULES
    try:
        if os.path.exists(RULES_FILE):
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                LEARNED_RULES = json.load(f)
    except Exception:
        pass

def save_rules():
    try:
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(LEARNED_RULES, f, indent=2)
    except Exception:
        pass

load_rules()

# ── heuristic engine (unchanged) ─────────────────────────────────

COMMON_ANSWERS = {
    "age": "32", "gender": "Female", "postal code": "400001",
    "zip": "400001", "pincode": "400001", "income": "₹12,00,000",
    "education": "Graduate", "employment": "Full-time",
    "industry": "Information Technology", "decision maker": "Yes",
}

def try_heuristic(question: str, elements: List[dict]) -> Optional[tuple[List[Action], str]]:
    """
    Local heuristic fallback for /decide when LLM is unreachable.
    Returns (actions, memory_note) or None to fall through to LLM path.

    Phases:
      0. Categorize elements by REAL interactive type (handles content script payload format)
      1. Industry trap
      2. Select-all checkbox groups
      3. Keyword-matched text inputs
      4. Generic text-input filler for unmatched fields
      5. Select dropdowns
      6. Checkboxes — check all unchecked
      7. Radio buttons — pick first unselected per group
      8. Navigation button ONLY when zero fields remain
    """
    q = question.lower()
    actions: List[Action] = []
    notes: List[str] = []
    targeted_ids: set = set()

    # ── Helper: determine REAL interactive type ─────────────────────
    # The content script sends `type` as high-level category:
    #   "input" for ALL <input> elements (text, checkbox, radio, etc.)
    #   "select" for <select> dropdowns
    #   "textarea" for <textarea>
    #   "button" for <button>
    #   "label" for <label>
    # The actual HTML input type is in input_type, tagType, or tag.
    def _real_type(el):
        etype = el.get("type", "")
        if etype == "select":
            return "select"
        if etype == "textarea":
            return "textarea"
        if etype == "button":
            return "button"
        if etype == "label":
            return "label"
        if etype == "input":
            # Check all possible fields for actual HTML input type
            real = str(el.get("input_type", "") or el.get("tagType", "") or el.get("tag", "")).lower().strip()
            if real in ("checkbox", "radio", "text", "email", "tel", "number", "date", "password", "url", "search", "hidden"):
                return real
            # Infer from properties
            if "checked" in el:
                return "checkbox"  # safest default; radio also has checked but we group later
            if el.get("options"):
                return "select"
            return "text"
        return etype

    def _is_text_input(el):
        return _real_type(el) in ("text", "email", "tel", "number", "date", "password", "url", "search", "hidden")

    def _is_checkbox(el):
        return _real_type(el) == "checkbox"

    def _is_radio(el):
        return _real_type(el) == "radio"

    def _is_select(el):
        return _real_type(el) == "select"

    # ── helpers ──────────────────────────────────────────────────────
    def _search_text(el):
        return " ".join(str(p) for p in filter(None, [
            el.get("name", ""), el.get("placeholder", ""),
            el.get("text", ""), el.get("id", "")
        ])).lower().replace("_", " ").replace("-", " ")

    def _generic_value(el):
        name = _search_text(el)
        real = _real_type(el)
        if any(x in name for x in ["first", "fname"]):
            return "Alex"
        if any(x in name for x in ["last", "lname"]):
            return "Morgan"
        if "email" in name:
            return "alex.morgan@example.com"
        if any(x in name for x in ["phone", "tel", "mobile"]):
            return "555-0123"
        if any(x in name for x in ["company", "org"]):
            return "Acme Corporation"
        if any(x in name for x in ["job", "title", "position"]):
            return "Software Engineer"
        if any(x in name for x in ["comment", "message", "note", "feedback"]):
            return "Interested in your services. Please contact me."
        if "address" in name and "email" not in name:
            return "123 Main Street"
        if "city" in name:
            return "Springfield"
        if any(x in name for x in ["zip", "postal"]):
            return "12345"
        if "state" in name:
            return "CA"
        if "country" in name:
            return "United States"
        if _real_type(el) == "textarea":
            return "Interested in learning more about your services."
        if real == "number":
            return "42"
        if real == "date":
            return "1990-01-01"
        if real == "password":
            return "SecurePass123!"
        return "N/A"

    # ── Phase 0: Count total answerable fields ───────────────────────
    answerable = [e for e in elements if _real_type(e) in (
        "text", "email", "tel", "number", "date", "password", "url", "search",
        "textarea", "select", "checkbox", "radio"
    )]

    # ── Phase 1: industry trap ───────────────────────────────────────
    industry_words = ("market research", "advertising", "marketing")
    is_industry_q = ((re.search(r"\bwork in\b", q) and any(t in q for t in industry_words))
                     or "do you work in any of the following" in q
                     or "work in the following industries" in q)
    if is_industry_q:
        for e in elements:
            if "none" in e.get("text", "").lower():
                if e.get("checked") is True:
                    return [Action(action_type="next",
                                   reasoning="Industry trap already answered -> next")],                            "industry already answered"
                return [Action(action_type="click", element_id=e["id"],
                               reasoning="Industry trap: none of the above")], "industry trap: none"
        for e in elements:
            if "other" in e.get("text", "").lower():
                if e.get("checked") is True:
                    continue
                return [Action(action_type="click", element_id=e["id"],
                               value="Engineering",
                               reasoning="Industry trap via Other")], "industry trap: other"

    # ── Phase 2: select-all checkbox groups ──────────────────────────
    if any(w in q for w in ("select all that apply", "check all that apply",
                            "check all", "select any that")):
        for e in elements:
            if not _is_checkbox(e) and e.get("tag") not in ("label", "li"):
                continue
            text = e.get("text", "").lower()
            if any(bad in text for bad in ("none", "don't know", "prefer not", "not sure")):
                continue
            if e.get("checked") is True:
                continue
            actions.append(Action(action_type="click", element_id=e["id"],
                                  reasoning="Select-all heuristic"))
            targeted_ids.add(e["id"])
        if actions:
            actions.append(Action(action_type="next",
                                  reasoning="Proceed after select-all"))
            return actions, "select-all heuristic"
        if any(_is_checkbox(e) or e.get("tag") in ("label", "li") for e in elements):
            return [Action(action_type="next",
                           reasoning="Select-all already satisfied -> next")],                    "select-all already answered"

    # ── Phase 3: keyword-matched text inputs ─────────────────────────
    for e in elements:
        if _is_text_input(e) or _real_type(e) == "textarea":
            if e["id"] in targeted_ids:
                continue
            if (e.get("value") or "").strip():
                targeted_ids.add(e["id"])
                continue
            search = _search_text(e)
            for kw, ans in COMMON_ANSWERS.items():
                if kw in search:
                    actions.append(Action(action_type="type",
                                          element_id=e["id"],
                                          value=ans,
                                          reasoning=f"keyword_match:{kw}"))
                    targeted_ids.add(e["id"])
                    notes.append(f"filled '{e.get('name','')}' via keyword '{kw}'")
                    break

    # ── Phase 4: generic text-input filler ───────────────────────────
    for e in elements:
        if _is_text_input(e) or _real_type(e) == "textarea":
            if e["id"] in targeted_ids:
                continue
            if (e.get("value") or "").strip():
                targeted_ids.add(e["id"])
                continue
            val = _generic_value(e)
            actions.append(Action(action_type="type",
                                  element_id=e["id"],
                                  value=val,
                                  reasoning="generic_fill:unmatched_text_input"))
            targeted_ids.add(e["id"])
            notes.append(f"generic-filled '{e.get('name', e.get('placeholder',''))}' with '{val}'")

    # ── Phase 5: select dropdowns ────────────────────────────────────
    for e in elements:
        if _is_select(e) and e["id"] not in targeted_ids:
            opts = [opt for opt in (e.get("options") or [])
                    if opt.get("value") is not None and not opt.get("disabled")]
            if opts:
                # Pick first non-placeholder option
                chosen = opts[0]
                for opt in opts:
                    txt = str(opt.get("text", "")).lower().strip()
                    if txt and txt not in ("", "select...", "choose...", "—", "-", "none", "pick one", "select an option"):
                        chosen = opt
                        break
                actions.append(Action(action_type="select_option",
                                      element_id=e["id"],
                                      value=chosen.get("text", chosen.get("value", "")),
                                      reasoning=f"select {chosen.get('text','')[:40]}"))
                targeted_ids.add(e["id"])
                notes.append(f"selected dropdown '{e.get('name','')}'")

    # ── Phase 6: checkboxes — check ALL unchecked ────────────────────
    for e in elements:
        if _is_checkbox(e) and e["id"] not in targeted_ids:
            if e.get("checked") is True:
                targeted_ids.add(e["id"])
                continue
            actions.append(Action(action_type="click",
                                  element_id=e["id"],
                                  reasoning="checkbox_check"))
            targeted_ids.add(e["id"])
            notes.append(f"checked '{e.get('name', e.get('text','checkbox'))}'")

    # ── Phase 7: radio buttons — pick first unselected per group ─────
    radio_groups = {}
    for e in elements:
        if _is_radio(e):
            key = e.get("name") or e.get("sid") or e.get("id")
            radio_groups.setdefault(key, []).append(e)

    chosen_groups: set = set()
    for e in elements:
        if not _is_radio(e):
            continue
        if e.get("checked") is True:
            targeted_ids.add(e["id"])
            continue
        if e["id"] in targeted_ids:
            continue
        name = (e.get("name") or "").strip()
        text = (e.get("text", "") or "").lower()
        if "none" in text or "don't know" in text or "prefer not" in text:
            continue
        if name:
            if name in chosen_groups:
                continue
            if any(g.get("checked") for g in radio_groups.get(name, [])):
                continue
            chosen_groups.add(name)
        actions.append(Action(action_type="click", element_id=e["id"],
                              reasoning=f"pick {text[:40]}"))
        targeted_ids.add(e["id"])
        notes.append(f"picked '{text[:40]}'")
        break  # one radio action per cycle

    # ── Phase 8: navigation button ONLY when truly done ──────────────
    # Count how many answerable fields are still NOT targeted
    pending = sum(1 for e in answerable if e["id"] not in targeted_ids)

    if pending == 0:
        for e in elements:
            if e.get("type") == "button" or (e.get("tag") == "button"):
                btn_text = (e.get("text", "") + " " + e.get("name", "")).lower()
                if any(x in btn_text for x in ["next", "continue", "start", "begin", "submit", "send"]):
                    actions.append(Action(action_type="next",
                                          reasoning=f"navigation_button:{e.get('text','')[:40]}"))
                    targeted_ids.add(e["id"])
                    notes.append(f"clicked navigation button '{e.get('text','')}'")
                    break

    if actions:
        note = "; ".join(notes) if notes else "heuristic"
        return actions, note

    return None

# ── panel-hub host matching (mirrors core.is_survey_router_hub) ──
def is_panel_hub(url: str) -> bool:
    try:
        u = urllib.parse.urlsplit((url or "").lower().strip())
        host = u.netloc or u.path
    except Exception:
        host = (url or "").lower()
    if host.startswith("www."):
        host = host[4:]          # leading label only — matches hub_match.js
    host = host.split(":")[0].split("/")[0]
    return any(host == d or host.endswith("." + d)
               for d in get_panel_hub_domains())

# ── endpoints ────────────────────────────────────────────────────

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
        )

    _prune_sessions()
    session_id = req.session_id
    SESSION_LAST_SEEN[session_id] = time.time()
    if session_id not in MEMORY:
        MEMORY[session_id] = []

    if len(req.screenshot_b64) > 4_000_000:
        rec.update(path="error", error="screenshot too large",
                   latency_ms=int((time.time() - t0) * 1000))
        record_trace(rec)
        raise HTTPException(status_code=413, detail="screenshot too large")

    heuristic = None
    if heuristic_decide is not None:
        try:
            heuristic = heuristic_decide(req.elements, req.page_text)
        except Exception:
            heuristic = None
    if heuristic and heuristic.get("actions"):
        actions = heuristic["actions"]
        note = heuristic.get("page_summary", "heuristic")
        bus.record("backend", "heuristic",
                   f"cycle {cycle}: served locally — LLM bypassed ({note})",
                   {"cycle": cycle, "note": note}, level="info")
        decision = SurveyDecision(
            page_summary=heuristic.get("page_summary", "heuristic"),
            question_type=heuristic.get("question_type", "unknown"),
            confidence=heuristic.get("confidence", 0.2),
            actions=actions,
            memory_note=heuristic.get("memory_note"),
            source="heuristic",
        )
        decision = _finish(rec, req, session_id, decision, t0, path="heuristic")
        return decision

    # ── provider health gate — if the external API is unreachable, use heuristic ──
    health = await asyncio.to_thread(provider_health.health)
    if not health["api_ready"] or client is None:
        if heuristic is None:
            try:
                heuristic = heuristic_decide(req.elements, req.page_text)
            except Exception:
                heuristic = None
        if heuristic and heuristic.get("actions"):
            actions = heuristic["actions"]
            note = heuristic.get("page_summary", "heuristic")
            bus.record("backend", "heuristic",
                       f"cycle {cycle}: provider down — LLM bypassed ({note})",
                       {"cycle": cycle, "note": note}, level="warn")
            decision = SurveyDecision(
                page_summary=heuristic.get("page_summary", "heuristic"),
                question_type=heuristic.get("question_type", "unknown"),
                confidence=heuristic.get("confidence", 0.2),
                actions=actions,
                memory_note=heuristic.get("memory_note"),
                source="heuristic",
            )
            decision = _finish(rec, req, session_id, decision, t0, path="heuristic")
            return decision
        decision = SurveyDecision(
            page_summary="heuristic: provider down, no actions",
            question_type="completion",
            confidence=0.2,
            actions=[],
            memory_note=f"provider_down: {health['error']}",
            source="heuristic",
        )
        decision = _finish(rec, req, session_id, decision, t0, path="heuristic")
        return decision

    # ── model path: build the image the model ACTUALLY sees ──────
    screenshot_b64 = req.screenshot_b64
    img_mime = "image/jpeg"
    try:
        img_data = base64.b64decode(req.screenshot_b64)
        img = Image.open(io.BytesIO(img_data))
        if img.width > 1280:
            ratio = 1280 / img.width
            img = img.resize((1280, int(img.height * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            screenshot_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        screenshot_b64 = req.screenshot_b64

    # ── sniper mode: for dense lists (>50 options), ask for keywords only ──
    if any(k in req.page_text.lower() for k in ["choose the brand you trust most", "60 options", "sniper-path threshold"]) or len(req.elements) > 50:
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
            match = next((e for e in req.elements if keyword.lower() in e.get("text", "").lower()), None)
            if match:
                decision = SurveyDecision(
                    page_summary=f"sniper: {keyword}",
                    question_type="single_choice",
                    confidence=0.9,
                    actions=[Action(action_type="click", element_id=match["id"],
                                    reasoning=f"Sniper hit: {keyword}")],
                    memory_note=f"sniper selected {match.get('text','')}"
                )
                return _finish(rec, req, session_id, decision, t0, path="sniper")
        except Exception:
            pass

    # ── image trap OCR: if an image is present and a text input nearby, try OCR ──
    img_text = ""
    try:
        imgs = [e for e in req.elements if e.get("tag") == "img"]
        if imgs:
            import base64, io
            from PIL import Image
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

    # ── iframe hint: if page has iframe, add instructions to prompt ──
    iframe_hint = ""
    try:
        iframes = self.driver.find_elements(By.TAG_NAME, "iframe") if hasattr(self, 'driver') else []
        if iframes:
            iframe_hint = "\nNOTE: This page contains an iframe. After answering the main page, switch to the iframe context to answer its question."
    except Exception:
        pass

    memory_block = ("\n".join(MEMORY[session_id][-12:])
                    if MEMORY[session_id] else "None yet.")
    rules_block = ("\n".join(f"- {r}" for r in LEARNED_RULES)
                   if LEARNED_RULES else "None yet.")

    prompt = f"""Analyze the survey screenshot and element map. Decide the next action(s).

URL: {req.url}
Page text: {req.page_text[:2500]}
Elements: {json.dumps(req.elements[:40])}
Memory: {memory_block}
Rules: {rules_block}{iframe_hint}

Return JSON matching the SurveyDecision schema."""

    rec["prompt"] = prompt
    rec["rules"] = list(LEARNED_RULES)
    rec["memory_ctx"] = list(MEMORY[session_id][-12:])
    rec["model"] = MODEL
    _last_debug.update({"ts": rec["ts"], "url": req.url, "prompt": prompt,
                        "persona": PERSONA, "model": MODEL})

    try:
        try:
            logger.info(f"[omni] structured -> base_url={client.base_url} model={MODEL}")
            with omni_call(provider="openai-compat", model=MODEL,
                           cycle=rec.get("cycle")):
                resp = client.beta.chat.completions.parse(
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": PERSONA},
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
                    resp = client.chat.completions.create(
                        model=MODEL,
                        messages=[
                            {"role": "system", "content": PERSONA},
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
            heuristic = heuristic_decide(req.elements, req.page_text)
        except Exception:
            heuristic = None
        if heuristic and heuristic.get("actions"):
            decision = SurveyDecision(
                page_summary=heuristic.get("page_summary", "heuristic"),
                question_type=heuristic.get("question_type", "unknown"),
                confidence=heuristic.get("confidence", 0.2),
                actions=heuristic["actions"],
                memory_note=heuristic.get("memory_note"),
                source="heuristic",
            )
            decision = _finish(rec, req, session_id, decision, t0, path="heuristic")
            return decision
        decision = SurveyDecision(
            page_summary="heuristic: timeout, no actions",
            question_type="navigation",
            confidence=0.2,
            actions=[Action(action_type="next", reasoning="llm timeout — navigate")],
            memory_note="timeout_no_actions",
            source="heuristic",
        )
        decision = _finish(rec, req, session_id, decision, t0, path="heuristic")
        return decision
    except Exception as e:
        rec.update(path="error", error=f"{type(e).__name__}: {e}",
                   latency_ms=int((time.time() - t0) * 1000))
        record_trace(rec)
        raise HTTPException(status_code=500, detail=str(e))


def _finish(rec, req, session_id, decision, t0, path):
    """Shared tail: memory bookkeeping, trace archive, dry-run guard."""
    if decision.memory_note:
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
            resp = client.chat.completions.create(
                model=MODEL, messages=[{"role": "user", "content": prompt}],
                max_tokens=100, temperature=0.3,
            )
        rule = resp.choices[0].message.content.strip()
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
    """Safe key fingerprint for the debug panel — never the full key."""
    if not key:
        return None
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "set"


def _omni_detail() -> dict:
    """Payload for GET /omni — tolerates fields the omni layer doesn't
    populate (they render as '—' in the console)."""
    o = bus.omni
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
        bus.omni["last_ping"] = {"t": time.time(), "ms": round(ms, 1),
                                 "ok": True, "reply": str(reply)[:120]}
        bus.record("omni", "ping",
                   f"ping ok in {ms:.0f} ms — {str(reply)[:60]}",
                   dict(bus.omni["last_ping"]), ms=ms)
    except Exception as e:
        ms = (time.time() - t0) * 1000
        bus.omni["last_ping"] = {"t": time.time(), "ms": round(ms, 1),
                                 "ok": False, "reply": str(e)[:120]}
        bus.record("omni", "ping",
                   f"ping FAILED after {ms:.0f} ms — {e}",
                   dict(bus.omni["last_ping"]), level="error", ms=ms)
    return bus.omni["last_ping"]


# ── panel hub configuration (edited from the extension popup) ────

class PanelHubRequest(BaseModel):
    url: str = Field(..., description="Panel link/domain to add; empty string clears all")


@app.get("/config/panel-hub")
async def read_panel_hubs():
    return {"panel_hubs": list(get_panel_hub_domains())}


@app.post("/config/panel-hub")
async def write_panel_hub(req: PanelHubRequest):
    if req.url.strip():
        domains = set_panel_hub_domains(list(get_panel_hub_domains()) + [req.url])
        bus.record("backend", "config",
                   f"panel hub added: {domains[-1] if domains else '(rejected)'}",
                   {"panel_hubs": list(domains)}, level="info")
    else:
        domains = set_panel_hub_domains([])
        bus.record("backend", "config", "panel hubs cleared",
                   {"panel_hubs": []}, level="info")
    return {"panel_hubs": list(domains)}

# ── prompt inspector ─────────────────────────────────────────────

@app.get("/debug/last")
async def debug_last():
    if not _last_debug:
        return {"detail": "no calls yet"}
    return _last_debug

if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("[!] Set API_KEY in .env "
                         "or the environment before starting.")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)



