"""One-shot live validation harness for survey-test.html.

Serves the project folder over localhost, drives the specimen end-to-end
with Playwright Chromium (one TRUSTED CDP click + a fully synthetic DOM
run, mirroring how Sentinel's two paths behave), and asserts:
  - zero console/page errors
  - trap image data-URI armed, 60-option dense list rendered
  - all 17 self-scoring checks pass on the DONE screen
  - completion keywords match what core.py/content.js listen for
  - DQ overlay toggles
"""
import threading, functools
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = r"c:\Users\tiajungba\.gemini\antigravity-ide\scratch\sentinel_survey_bot"

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)
    def log_message(self, *a):
        pass

srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{PORT}/survey-test.html"
print(f"[srv] serving on {URL}")

from playwright.sync_api import sync_playwright

SYNTH_RUN = r"""
() => {
  const $  = s => document.querySelector(s);
  const $$ = s => [...document.querySelectorAll(s)];
  const fire = (el, type) => el.dispatchEvent(new Event(type, {bubbles:true}));
  const setText = (el, v) => { el.value = v; fire(el,'input'); fire(el,'change'); };
  const pick = (name, val) => {
    const el = $$(`input[name="${name}"]`).find(r => r.value === val);
    el.checked = true; fire(el,'input'); fire(el,'change');
  };
  setText($('#f-age'), '32');
  setText($('#f-zip'), '400001');
  pick('industry', 'none');                       // dodge the industry trap
  ['milk','bread','phone'].forEach(v => {         // select-all, skip None-trap
    const el = $$('input[name="bought"]').find(c => c.value === v);
    el.checked = true; fire(el,'change');
  });
  const sel = $('select[name="employ"]');
  sel.value = 'Full-time'; fire(sel,'input'); fire(sel,'change');
  ['g1|4','g2|4','g3|5'].forEach(p => { const [g,v] = p.split('|'); pick(g, v); });
  $('#ce').textContent = 'Battery life and repairability.';
  fire($('#ce'), 'input');
  setText($('textarea[name="comments"]'), 'No further comments.');
  setText($('input[name="trap_word"]'), 'PANDA'); // image trap
  pick('freq', 'weekly');                         // aria-label-only radios
  pick('brand', 'Kestrel');                       // sniper pick from 60 options
  document.querySelector('[data-next="2"]').click();
  document.querySelector('[data-next="3"]').click();
  document.querySelector('[data-next="4"]').click();
  $('#next4').click();                            // i18n sticky bar -> page 5
}
"""

IFRAME_READY = r"""
() => {
  try {
    const d = document.querySelector('#frame5').contentDocument;
    return !!(d && d.querySelectorAll('input[name="region"]').length === 4);
  } catch (e) { return false; }
}
"""

IFRAME_ANSWER = r"""
() => {
  const doc = document.querySelector('#frame5').contentDocument;
  const east = [...doc.querySelectorAll('input[name="region"]')]
                 .find(r => r.value === 'East');
  east.checked = true;
  east.dispatchEvent(new Event('change', {bubbles:true}));
}
"""

STATS = r"""
() => ({
  rows: [...document.querySelectorAll('#scorecard .srow')].map(r => ({
    ok:  r.classList.contains('pass'),
    lbl: r.querySelector('.lbl').textContent,
    val: r.querySelector('.val').textContent })),
  ev:  document.querySelector('#mEv').textContent,
  t:   document.querySelector('#mT').textContent,
  s:   document.querySelector('#mS').textContent,
  note:document.querySelector('#trustNote').textContent,
  body:document.body.innerText.toLowerCase(),
})
"""

failures = []
def check(name, cond, detail=""):
    tag = "[ok]" if cond else "[FAIL]"
    print(f"{tag} {name}{(' — ' + str(detail)) if detail else ''}")
    if not cond:
        failures.append(name)

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1360, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append("console:" + m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append("pageerror:" + str(e)))
    page.goto(URL, wait_until="load")
    page.wait_for_timeout(300)

    check("boot: no JS errors", not errors, errors[:3])
    check("trap image data-uri armed",
          (page.get_attribute("#trapImg", "src") or "").startswith("data:image/svg+xml"))
    check("dense list renders 60 options",
          page.eval_on_selector_all("#mega input", "els => els.length") == 60)

    # ── TRUSTED path: real CDP input (what TRUSTED_MOUSE delivers) ──
    page.locator('input[name="gender"][value="Male"]').check()

    # ── SYNTHETIC path: DOM-dispatched run through all 5 systems ──
    page.evaluate(SYNTH_RUN)
    page.wait_for_function(IFRAME_READY, timeout=5000)
    page.evaluate(IFRAME_ANSWER)            # legacy frame -> postMessage -> parent
    page.wait_for_function("() => typeof state !== 'undefined' && state.iframeAnswer !== null", timeout=5000)
    print("    iframe reported:", page.evaluate("() => state.iframeAnswer"))
    page.evaluate("() => document.querySelector('[data-next=\"done\"]').click()")
    page.wait_for_selector("#scorecard .srow", timeout=5000)
    st = page.evaluate(STATS)

    bad = [r for r in st["rows"] if not r["ok"]]
    for r in st["rows"]:
        print(f"    {'PASS' if r['ok'] else 'FAIL':4} · {r['lbl']:42} · {r['val']}")
    check("scorecard: 17 rows scored", len(st["rows"]) == 17, len(st["rows"]))
    check("scorecard: all checks pass", not bad, [r["lbl"] for r in bad])
    check("rail captured plenty of events", int(st["ev"]) >= 25, f"EVT={st['ev']} T={st['t']} S={st['s']}")
    check("trusted badges present (T>0)", int(st["t"]) > 0, f"T={st['t']}")

    comp_keywords = ["thank you", "completed", "finished", "success",
                     "your responses have been recorded"]
    check("completion detected like core.py/content.js",
          any(x in st["body"] for x in comp_keywords))
    check("DQ overlay toggles",
          page.evaluate("() => { document.querySelector('#dqBtn').click(); return document.querySelector('#dq').classList.contains('on'); }"))

    page.screenshot(path=ROOT + r"\screenshots\specimen_scorecard.png", full_page=True)
    browser.close()

srv.shutdown()
print("\ntrust verdict:", st["note"])
print("\nRESULT:", "ALL CHECKS PASSED ✔" if not failures else f"FAILURES: {failures}")
raise SystemExit(1 if failures else 0)
