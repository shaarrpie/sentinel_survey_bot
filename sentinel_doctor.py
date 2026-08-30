"""
sentinel_doctor.py — standalone diagnostic for sentinel_survey_bot.

Run from the sentinel_survey_bot project root:
    python sentinel_doctor.py

Checks, in order:
  1. Bot .env exists and has BASE_URL / MODEL_NAME / FREELLM_DIR
  2. omniroute port is open
  3. omniroute API actually answers (GET /v1/models) and lists models
  4. omniroute storage state (STORAGE_ENCRYPTION_KEY vs storage.sqlite)
  5. sentinel-router.log for known failure signatures

Uses stdlib only. Prints a verdict + exact fix steps.
"""

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

HOME = Path.home()
OMNI_DIR = HOME / ".omniroute"
OMNI_ENV = OMNI_DIR / ".env"
OMNI_DB = OMNI_DIR / "storage.sqlite"
BOT_ENV = Path(".env")
DEFAULT_BASE = "http://127.0.0.1:20128"


def read_env_file(path: Path) -> dict:
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def http_get(url: str, timeout: float = 4.0, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def check(label: str, ok: bool, detail: str = ""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    print("sentinel_survey_bot doctor\n" + "-" * 40)
    fixes: list[str] = []

    # 1. Bot .env
    bot_env = read_env_file(BOT_ENV)
    if not check("bot .env found", BOT_ENV.is_file(), str(BOT_ENV.resolve())):
        fixes.append("Create .env from .env.example in the project root.")
    base_url = (bot_env.get("BASE_URL") or DEFAULT_BASE + "/v1").rstrip("/")
    root = base_url[:-3] if base_url.endswith("/v1") else base_url
    model = bot_env.get("MODEL_NAME") or "auto/best-chat"
    api_key = bot_env.get("API_KEY") or ""
    free_dir = Path(bot_env.get("FREELLM_DIR") or ".")
    parsed = urlparse(root)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 20128
    print(f"       BASE_URL={base_url}  MODEL_NAME={model}  FREELLM_DIR={free_dir}")

    # 2. Port
    up = port_open(host, port)
    if not check(f"router port {host}:{port} open", up):
        fixes.append(
            f"Router is not listening on {port}. Start it via the backend lifespan "
            f"or run `npm run dev` in {free_dir}."
        )

    # 3. API answers + models
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    status, body = http_get(f"{root}/v1/models", headers=headers) if up else (None, "skipped")
    api_ok = status == 200
    model_count = 0
    if api_ok:
        try:
            data = json.loads(body)
            model_count = len(data.get("data") or [])
        except Exception:
            model_count = -1
    if not check("GET /v1/models returns 200", api_ok, f"status={status}"):
        fixes.append(
            "Router port is open but the API does not answer — the omniroute server "
            "is still starting or wedged. See its console / sentinel-router.log."
        )
    if api_ok and not check("model list is non-empty", model_count > 0, f"models={model_count}"):
        fixes.append(
            "No models are connected, so auto/* routes resolve to empty pools "
            "('provider down'). Re-add at least one provider in omniroute, or set "
            "MODEL_NAME in .env to a specific connected model."
        )

    # 4. Storage encryption state
    omni_env = read_env_file(OMNI_ENV)
    key_set = bool(omni_env.get("STORAGE_ENCRYPTION_KEY"))
    db_exists = OMNI_DB.is_file()
    if db_exists and not key_set:
        check("omniroute storage decryptable", False,
              "storage.sqlite exists but STORAGE_ENCRYPTION_KEY is missing")
        fixes.append(
            f"Restore STORAGE_ENCRYPTION_KEY in {OMNI_ENV} (the key that created "
            f"{OMNI_DB}). If it is lost, move/delete {OMNI_DB} to start fresh."
        )
    else:
        check("omniroute storage decryptable", True,
              "key present" if key_set else "no database yet")

    # 5. Router log signatures
    log_path = free_dir / "sentinel-router.log"
    if log_path.is_file():
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
        sigs = {
            "Cannot decrypt": "STORAGE_ENCRYPTION_KEY missing (confirms fix #4)",
            "matched no connected models": "auto pools are empty (confirms fix #3)",
            "did not respond within": "omniroute server failed to finish booting",
        }
        found = [s for s in sigs if s in tail]
        check("router log clean", not found,
              "; ".join(f"'{s}' -> {sigs[s]}" for s in found) if found else "no known signatures in last 20KB")
    else:
        print(f"[INFO] {log_path} not found (router may not have been spawned yet)")

    # Verdict
    print("-" * 40)
    if fixes:
        print("VERDICT: provider path broken. Fix order:")
        for i, f in enumerate(fixes, 1):
            print(f"  {i}. {f}")
        return 1
    print("VERDICT: provider path healthy. If /decide still times out, "
          "inspect backend.py's /decide handler directly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
