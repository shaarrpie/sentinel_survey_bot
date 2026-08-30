from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import httpx


class ProviderHealth:
    """Cached health probe for an externally managed OpenAI-compatible API."""

    def __init__(self, base_url: str, api_key: str, model: str,
                 cache_seconds: float = 30.0):
        # 30s (was 2s): a working-but-slow router flapped between "up" and
        # "demoted to heuristic" on every probe, and /decide paid a probe
        # round trip on nearly every call.
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._checked_at = 0.0

    def _probe(self) -> dict:
        started = time.perf_counter()
        parsed = urlparse(self.base_url)
        result = {
            "configured": bool(parsed.scheme and parsed.hostname and self.model),
            "base_url": self.base_url,
            "host": parsed.hostname,
            "port": parsed.port or (443 if parsed.scheme == "https" else 80),
            "model": self.model,
            "api_key_set": bool(self.api_key),
            "api_ready": False,
            "status_code": None,
            "latency_ms": None,
            "error": None,
            "mode": "heuristic",
            "managed_process": False,
        }

        if not result["configured"]:
            result["error"] = "BASE_URL or MODEL_NAME is missing"
            return result

        headers = (
            {"Authorization": f"Bearer {self.api_key}"}
            if self.api_key else {}
        )
        try:
            response = httpx.get(
                f"{self.base_url}/models",
                headers=headers,
                timeout=5.0,   # was 2.0 — slow-but-alive routers were being demoted
                follow_redirects=True,
            )
            result["status_code"] = response.status_code
            result["latency_ms"] = round(
                (time.perf_counter() - started) * 1000, 1
            )
            result["api_ready"] = response.is_success
            if response.is_success:
                result["mode"] = "provider"
            else:
                result["error"] = f"/models returned HTTP {response.status_code}"
        except Exception as error:
            result["latency_ms"] = round(
                (time.perf_counter() - started) * 1000, 1
            )
            result["error"] = f"{type(error).__name__}: {error}"
        return result

    def health(self, force: bool = False) -> dict:
        now = time.monotonic()
        with self._lock:
            if (
                not force and self._cached is not None and
                now - self._checked_at < self.cache_seconds
            ):
                return dict(self._cached)
            self._cached = self._probe()
            self._checked_at = time.monotonic()
            return dict(self._cached)

    def ready(self) -> bool:
        return bool(self.health().get("api_ready"))
