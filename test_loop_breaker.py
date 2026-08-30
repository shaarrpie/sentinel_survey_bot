"""Round-three loop-breaker proof: drives /decide through both phases of the
prison scenario and asserts the bot now progresses instead of looping.

Run from the repo root:  python test_loop_breaker.py
"""
import base64
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The backend refuses to boot without a token (fail-closed auth) and /decide
# requires the header — set both before importing.
os.environ.setdefault("SENTINEL_TOKEN", "loop-test-token")

from PIL import Image
import backend
backend.DRY_RUN = False                      # real actions in responses

from fastapi.testclient import TestClient

img = Image.new("RGB", (4, 4), (20, 24, 32))
buf = io.BytesIO(); img.save(buf, "JPEG", quality=70)
B64 = base64.b64encode(buf.getvalue()).decode()

AUTH = {"X-Sentinel-Token": os.environ["SENTINEL_TOKEN"]}

def payload(checked):
    return {
        "session_id": "looptest",
        "screenshot_b64": B64,
        "elements": [
            {"id": 21, "sid": "nl", "tag": "label",
             "text": "None of the above", "checked": checked},
            {"id": 22, "sid": "nr", "tag": "input", "type": "radio",
             "text": "None of the above", "checked": checked},
        ],
        "url": "http://x/survey",
        "page_text": ("Do you work in any of the following industries? "
                      "Advertising Market Research Engineering "
                      "None of the above"),
    }

failures = []
def check(name, cond, detail=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ((" — " + str(detail)) if detail else ""))
    if not cond:
        failures.append(name)

# Auth: no header -> 401 on /decide, /status stays public
with TestClient(backend.app) as c:
    r = c.post("/decide", json=payload(False))
    check("auth: /decide without token -> 401", r.status_code == 401, r.status_code)
    r = c.get("/status")
    check("auth: /status stays public", r.status_code == 200, r.status_code)
    r = c.get("/debug/last", headers=AUTH)
    check("debug: /debug/last gated off by default", r.status_code == 404, r.status_code)

    r1 = c.post("/decide", json=payload(False), headers=AUTH).json()
    check("phase1: unanswered -> click", r1["actions"] and
          r1["actions"][0]["action_type"] == "click", r1["actions"])
    # heuristic_decide reports 0.4 confidence for an action-producing page
    check("phase1: heuristic confidence 0.4", abs(r1["confidence"] - 0.4) < 1e-6,
          r1["confidence"])

    r2 = c.post("/decide", json=payload(True), headers=AUTH).json()
    check("phase2: answered -> next (LOOP BROKEN)",
          r2["actions"] and r2["actions"][0]["action_type"] == "next",
          r2["actions"])

    last, evs = backend.bus.since(0)
    kinds = [e["kind"] for e in evs]
    check("tracebus: decide events visible", "decide" in kinds, kinds)
    check("tracebus: heuristic bypass visible",
          "heuristic" in kinds and
          any("LLM bypassed" in e["msg"] for e in evs if e["kind"] == "heuristic"))

print("\nRESULT:", "ALL LOOP-BREAKER CHECKS PASSED" if not failures
      else f"FAILURES: {failures}")
sys.exit(1 if failures else 0)
