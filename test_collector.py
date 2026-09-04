"""Fixture-zoo test harness for the new collector.

Loads each HTML fixture, runs the collector against it, and asserts
what the element map + question text should contain.

Run: python test_collector.py
"""
import os
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.abspath(__file__))

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)
    def log_message(self, *a):
        pass

srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

with open(os.path.join(ROOT, "scripts", "get_question.js"), "r", encoding="utf-8") as fh:
    COLLECTOR = fh.read()

failures = []

def check(name, cond, detail=""):
    tag = "[ok]" if cond else "[FAIL]"
    print(f"{tag} {name}{(' — ' + str(detail)) if detail else ''}")
    if not cond:
        failures.append(name)

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 800})

    # ── 1. Shadow DOM radios ─────────────────────────────────────────────
    page.goto(f"{BASE}/fixtures/shadow-radio.html")
    data = page.evaluate(COLLECTOR)
    radios = [e for e in data["elements"] if e["semanticType"] == "radio"]
    check("shadow: radios visible in collector", len(radios) >= 3, f"got {len(radios)}")
    check("shadow: question text captured", "color" in data["question"].lower())

    # ── 2. Visible label wraps hidden input (regression) ─────────────────
    page.goto(f"{BASE}/fixtures/visible-label-hidden-input.html")
    data = page.evaluate(COLLECTOR)
    radios = [e for e in data["elements"] if e["semanticType"] == "radio"]
    check("label-hidden: radios found", len(radios) >= 2, f"got {len(radios)}")
    check("label-hidden: accessible names present", all(r["accessibleName"] for r in radios))

    # ── 3. role=slider widget ───────────────────────────────────────────
    page.goto(f"{BASE}/fixtures/slider.html")
    data = page.evaluate(COLLECTOR)
    sliders = [e for e in data["elements"] if e["semanticType"] == "range"]
    check("slider: semanticType=range", len(sliders) >= 1, f"got {len(sliders)}")
    check("slider: accessible name present", sliders[0]["accessibleName"].lower().startswith("volume") if sliders else False)

    # ── 4. Virtualized list ─────────────────────────────────────────────
    page.goto(f"{BASE}/fixtures/virtualized-list.html")
    data = page.evaluate(COLLECTOR)
    checkboxes = [e for e in data["elements"] if e["semanticType"] == "checkbox"]
    check("virtualized: checkboxes rendered", len(checkboxes) >= 1, f"got {len(checkboxes)}")

    # ── 5. Dense 300-question page ──────────────────────────────────────
    page.goto(f"{BASE}/fixtures/dense-300.html")
    data = page.evaluate(COLLECTOR)
    radios = [e for e in data["elements"] if e["semanticType"] == "radio"]
    check("dense-300: all 300*3 radios in element map", len(radios) >= 900, f"got {len(radios)}")
    check("dense-300: question text present", len(data["question"]) > 0)

    # ── 6. Non-English completion text ─────────────────────────────────
    page.goto(f"{BASE}/fixtures/non-english.html")
    page.click('input[value="25-34"]')
    page.click('#next')
    page.wait_for_timeout(500)
    data = page.evaluate(COLLECTOR)
    body = data["rawText"].lower()
    check("non-english: completion detected", "gracias" in body, body[:80])

    # ── 7. Contenteditable ──────────────────────────────────────────────
    page.goto(f"{BASE}/fixtures/contenteditable.html")
    data = page.evaluate(COLLECTOR)
    edits = [e for e in data["elements"] if e["semanticType"] == "text" and e["tag"] == "div"]
    check("contenteditable: editor found", len(edits) >= 1, f"got {len(edits)}")

    # ── 8. Cross-origin iframe ──────────────────────────────────────────
    # Serve a second server on a different port for the iframe src
    srv2 = ThreadingHTTPServer(("127.0.0.1", 20129), Handler)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    page.goto(f"{BASE}/fixtures/cross-origin-iframe.html")
    page.wait_for_timeout(1000)
    data = page.evaluate(COLLECTOR)
    iframe_radios = [e for e in data["elements"] if e.get("frameId") != "top"]
    check("cross-origin iframe: elements from other frame visible (KNOWN LIMITATION — Step 3 per-frame injection fixes this)",
          len(iframe_radios) == 0, f"got {len(iframe_radios)} (expected 0 until per-frame injection)")
    srv2.shutdown()

    # ── Summary ────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"RESULT: {'ALL CHECKS PASSED' if not failures else 'FAILURES: ' + str(failures)}")
    print(f"{'='*50}")
    browser.close()

srv.shutdown()
raise SystemExit(1 if failures else 0)
