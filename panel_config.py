"""Runtime-configurable panel hub domains.

Single source of truth shared by core.py (Playwright flow) and backend.py
(extension flow), so the extension popup can add a panel hub link at runtime
without code changes. Landing on a configured hub means the survey TERMINATED
the session and bounced back to the panel — the bot must STOP and hand control
back; it must never fill out a login wall.
"""
import json
import os
import re
from urllib.parse import urlparse

PANEL_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "panel_config.json")

_domains = ()   # tuple of normalized hosts, e.g. ("panel.example.com",)


def _normalize(entry):
    """Accept a bare domain ('panel.example.com') or a full URL with a path
    ('https://research.example.com/panel?t=1'); return the bare host."""
    s = str(entry or "").strip().lower()
    if not s:
        return None
    if "://" not in s:
        s = "http://" + s          # make urlparse treat it as a URL
    try:
        host = urlparse(s).netloc or urlparse(s).path.split("/")[0]
    except Exception:
        return None
    host = host.split("@")[-1].split(":")[0]        # strip userinfo + port
    if host.startswith("www."):
        host = host[4:]
    if not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host:
        return None                                  # not a plausible domain
    return host


def load():
    global _domains
    try:
        with open(PANEL_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("panel_hub_domains", []) if isinstance(data, dict) else []
    except Exception:
        raw = []
    seen = []
    for r in raw:
        h = _normalize(r)
        if h and h not in seen:
            seen.append(h)
    _domains = tuple(seen)
    return _domains


def save():
    try:
        with open(PANEL_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"panel_hub_domains": list(_domains)}, f, indent=2)
    except Exception:
        pass


def get_panel_hub_domains():
    return _domains


def set_panel_hub_domains(entries):
    """Replace the whole list from URLs or bare domains (deduped).
    Returns the normalized tuple."""
    global _domains
    seen = []
    for e in entries or []:
        h = _normalize(e)
        if h and h not in seen:
            seen.append(h)
    _domains = tuple(seen)
    save()
    return _domains


load()
