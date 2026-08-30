# Sentinel Survey Bot

AI-assisted survey automation. A Chrome extension watches a survey page,
sends a screenshot + element map to a local FastAPI backend, and executes
the returned decisions (click / type / select / next). An LLM persona
(loaded from your real `profile.json`) answers the questions; a
deterministic heuristic takes over when the provider is unavailable.

> ## Read this first: what this is for
>
> **This project is a local testing/QA tool, built against
> `survey-test.html` and your own forms.** Pointing it at live,
> incentivized survey panels is a terms-of-service violation on virtually
> every panel, is plausibly fraud in many jurisdictions (you would be
> claiming rewards for fabricated responses), and poisons research data
> that other people depend on. The panel-hub login-wall stop exists partly
> so the bot refuses to fill out the machinery around those panels — do
> not remove it, and do not build around it.
>
> If you want this for real survey-taking, take the surveys. If you want
> it for form QA, it's good at that.

## Layout

| Path | What |
|---|---|
| `extension/` | MV3 Chrome extension: HUD, polling loop, element map, CDP "trusted" clicks, panel-hub stop |
| `backend.py` | FastAPI server: `/decide` (LLM primary, heuristic fallback), `/learn`, `/status`, `/omni`, `/traces`, `/config/panel-hub` |
| `sentinel_heuristic.py` | The one deterministic fallback + `real_input_kind()` element classifier |
| `core.py` | Standalone Selenium bot (F12-driven) — the older headless-style loop |
| `bot.py` | Standalone Selenium bot (CLI + `gui.py`) with CDP mouse controller |
| `sentinel_traces.py` | Thread-safe trace bus, `/traces` poll endpoint, `omni_call()` wrapper |
| `provider_health.py` | Cached `/models` health probe (30s cache, 5s timeout) |
| `panel_config.py` | Panel-hub domain store + the shared host matcher |
| `participant_profile.py` | Persona assembly from `profile.json` (your real details — gitignored) |
| `survey-test.html` | The self-scoring specimen form the smoke test drives |
| `templates/trace.html` | Self-viewing HTML archive written per `/decide` call |
| `examples/mock_form_bot.py` | Minimal form bot against the specimen |

There are three bot loops because the project evolved; the extension +
`backend.py` pair is the current product. `core.py` and `bot.py` are the
standalone variants.

## Element map schema

One contract, documented in `sentinel_heuristic.py`, asserted by the
matcher/loop tests:

```
{ id, sid?, frame?, name, tag, type, role?, text, option_value?,
  required?, disabled?, x, y, context?,
  checked?    (radio/checkbox),
  value?, options? (select: [{value, text, disabled}]),
  editable?   (contenteditable) }
```

- `type` is the **raw** html input type for inputs (`"radio"`, `"text"`,
  …). Producers derive it from the associated control so a
  `<label><input type=radio></label>` reads as a radio.
- Python readers go through `real_input_kind()`, which tolerates the
  extension payload, the `core.py` map, and legacy shapes. If a producer
  stops conforming, fix it there — not in three consumers.

## Install

```bat
:: Windows (launch.bat target)
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

1. Put a real `SENTINEL_TOKEN` in `.env` and mirror it into
   `extension/config.local.js` (gitignored):
   ```js
   window.SENTINEL_LOCAL_CONFIG = { token: "…same secret…" };
   ```
2. Start the backend (`python backend.py`) and your LLM router
   (OmniRoute on port 20128 is the documented default).
3. Load `extension/` unpacked in Chrome (`chrome://extensions`, dev
   mode). Note the extension ID and set it in `.env`
   (`SENTINEL_EXTENSION_ID`) to pin CORS.
4. Create `profile.json` with your **real** participant details
   (`profile.json.example` shows the shape). It is loaded from the repo
   directory regardless of your CWD, and it is gitignored.

## Running

- **Extension (current):** open a survey (e.g.
  `http://127.0.0.1:<port>/survey-test.html`), open the HUD (toolbar icon
  → Open HUD) and press **Start**. Content script injection is on demand:
  the extension injects `content.js` into the tab when you start a run and
  re-injects after hard navigations of the running tab.
- **Standalone:** `python main.py` (`core.py` loop, F12 starts it on the
  active tab) or `python run_gui.bat` / `gui.py` (`bot.py` with GUI).
- **Specimen smoke test:** `python smoke_survey_test.py`
  (needs `playwright` + `playwright install chromium`).

`SENTINEL_DRY_RUN=1` (the example default) makes the backend return
decisions while executing nothing — leave it on until the target is a
form you control.

## Security model (local, single-user)

- **Auth is fail-closed.** The backend refuses to boot without
  `SENTINEL_TOKEN`; every route except `GET /status` requires the
  `X-Sentinel-Token` header, compared with `hmac.compare_digest`.
- **CORS** is pinned to your extension ID when `SENTINEL_EXTENSION_ID` is
  set; otherwise it accepts any well-formed `chrome-extension://` origin
  and warns loudly.
- **Extension CSP** is `script-src 'self'` — no code is ever loaded from
  localhost; data flows over HTTP.
- The extension holds `debugger` (CDP input) plus host permissions so it
  can script-inject into the survey tab on demand. That is a real
  privilege; keep the extension on a machine/profile where you'd rather
  not have other extensions' bugs.
- **Traces** (`SENTINEL_TRACES`, default `./traces`, gitignored) contain
  page text, the element map, and prompt material. They are pruned by age
  (`SENTINEL_TRACE_AGE_HOURS`, default 72h) *and* count (400). Treat the
  folder like personal data. `GET /debug/last` (full prompt + persona) is
  off unless `SENTINEL_DEBUG=1`.
- **Single uvicorn worker.** `MEMORY` / `SESSION_LAST_SEEN` /
  `LEARNED_RULES` are module-level; running `--workers > 1` silently
  splits state. The launcher and tests run one worker.
- `--no-sandbox` is not passed to Chrome. If you must run inside a
  container, do it there deliberately.

## Tests

```bat
python test_loop_breaker.py       :: /decide auth + loop-break scenario (FastAPI TestClient)
python test_panel_hub_matcher.py  :: both Python matchers vs the shared table
node test_hub_matcher.node.js     :: the JS matcher vs the SAME table
```

All three tables must agree on every row — a login wall is what happens
the day they drift. CI runs all of them plus a full-bytecode compile
(`.github/workflows/ci.yml`).

## Known sharp edges

- `core.py` / `bot.py` and `extension/` + `backend.py` still each carry
  their own bot loop; consolidating to one is the next big refactor.
- `content.js` is 50 KB and was reviewed by the same person who wrote
  it. The element collector and the panel-hub stop are the load-bearing
  parts.
- Heuristic confidence is a fixed 0.4 — "I am a dumb autofiller",
  honestly stated.
