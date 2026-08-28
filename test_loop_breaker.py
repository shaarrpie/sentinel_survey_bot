"""Round-three loop-breaker proof: drives /decide through both phases of the
prison scenario and asserts the bot now progresses instead of looping."""
import base64, io, sys
sys.path.insert(0, r"c:\Users\tiajungba\.gemini\antigravity-ide\scratch\sentinel_survey_bot")

from PIL import Image
import backend
backend.DRY_RUN = False                      # real actions in responses

from fastapi.testclient import TestClient

img = Image.new("RGB", (4, 4), (20, 24, 32))
buf = io.BytesIO(); img.save(buf, "JPEG", quality=70)
B64 = base64.b64encode(buf.getvalue()).decode()

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

with TestClient(backend.app) as c:
    r1 = c.post("/decide", json=payload(False)).json()
    check("phase1: unanswered -> click", r1["actions"] and
          r1["actions"][0]["action_type"] == "click", r1["actions"])
    check("phase1: honest confidence 0.2", abs(r1["confidence"] - 0.2) < 1e-6,
          r1["confidence"])

    r2 = c.post("/decide", json=payload(True)).json()
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
