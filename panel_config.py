from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlparse


logger = logging.getLogger(__name__)
PANEL_CONFIG_FILE = Path(__file__).resolve().with_name("panel_config.json")
_lock = threading.RLock()
_domains: tuple[str, ...] = ()
_mtime_ns: int | None = None


def _normalize(entry) -> str | None:
    value = str(entry or "").strip().lower()
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    try:
        host = (urlparse(value).hostname or "").removeprefix("www.")
    except ValueError:
        return None
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
        return None
    if "." not in host or ".." in host:
        return None
    return host


def _read_unlocked() -> tuple[str, ...]:
    try:
        data = json.loads(PANEL_CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except Exception as error:
        logger.error("Failed reading %s: %s", PANEL_CONFIG_FILE, error)
        return _domains
    raw = data.get("panel_hub_domains", []) if isinstance(data, dict) else []
    return tuple(dict.fromkeys(filter(None, (_normalize(x) for x in raw))))


def load() -> tuple[str, ...]:
    global _domains, _mtime_ns
    with _lock:
        _domains = _read_unlocked()
        try:
            _mtime_ns = PANEL_CONFIG_FILE.stat().st_mtime_ns
        except FileNotFoundError:
            _mtime_ns = None
        return _domains


def save() -> None:
    """Atomic re-save of the current in-memory domains (round 30)."""
    with _lock:
        temp = PANEL_CONFIG_FILE.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps({"panel_hub_domains": list(_domains)}, indent=2),
            encoding="utf-8")
        os.replace(temp, PANEL_CONFIG_FILE)
        try:
            _set_mtime()
        except Exception:
            pass


def _set_mtime() -> None:
    global _mtime_ns
    try:
        _mtime_ns = PANEL_CONFIG_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        _mtime_ns = None


def get_panel_hub_domains() -> tuple[str, ...]:
    global _mtime_ns
    with _lock:
        try:
            current = PANEL_CONFIG_FILE.stat().st_mtime_ns
        except FileNotFoundError:
            current = None
        if current != _mtime_ns:
            return load()   # another process edited the file — pick it up live
        return _domains


def set_panel_hub_domains(entries) -> tuple[str, ...]:
    global _domains
    normalized = tuple(dict.fromkeys(
        filter(None, (_normalize(x) for x in entries or []))))
    payload = json.dumps({"panel_hub_domains": list(normalized)}, indent=2)
    with _lock:
        temp = PANEL_CONFIG_FILE.with_suffix(".json.tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, PANEL_CONFIG_FILE)   # atomic swap
        _domains = normalized
        _set_mtime()
        return _domains


load()

