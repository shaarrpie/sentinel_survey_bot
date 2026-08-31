"""
AI request diagnostics: exception classification, secret redaction,
request context capture. Reveals the REAL transport failure hidden by
the OpenAI SDK's generic "Connection error." wrapper.
"""

import os
import re
import socket
import ssl
import time
import traceback

import httpx

try:
    import openai
except ImportError:
    openai = None

# ---------------- configuration (env-overridable) ----------------
AI_MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES", "3"))
AI_REQUEST_TIMEOUT = float(os.getenv("AI_REQUEST_TIMEOUT", "30"))
AI_FAILURE_COOLDOWN = float(os.getenv("AI_FAILURE_COOLDOWN", "15"))
MAX_CONSECUTIVE_AI_FAILURES = int(os.getenv("MAX_CONSECUTIVE_AI_FAILURES", "5"))

# ---------------- secret redaction ----------------
_REDACT = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***REDACTED***"),
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?bearer\s+)[^\"'\s,}]+"), r"\1***REDACTED***"),
    (re.compile(r"(?i)(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"), r"\1***REDACTED***"),
    (re.compile(r"://([^:/@]+):([^@]+)@"), r"://\1:***REDACTED***@"),
]


def redact(text: str) -> str:
    for rx, sub in _REDACT:
        text = rx.sub(sub, text)
    return text


# ---------------- exception chain walking ----------------
def _chain(exc):
    seen, e = [], exc
    while e is not None and e not in seen:
        seen.append(e)
        e = e.__cause__ or e.__context__
    return seen


def classify_failure(exc):
    """
    Return (CATEGORY, DETAIL) describing the REAL transport failure.
    Walks the exception chain to find the root cause hidden by the SDK.
    """
    chain = _chain(exc)

    # OpenAI HTTP-status errors first
    if openai is not None and isinstance(exc, openai.APIStatusError):
        code = getattr(exc, "status_code", None)
        if code == 401:
            return "HTTP_401_UNAUTHORIZED", "invalid API key or org"
        if code == 403:
            return "HTTP_403_FORBIDDEN", "key lacks permission for this model"
        if code == 404:
            return "HTTP_404_NOT_FOUND", "wrong endpoint or unknown model"
        if code == 429:
            return "HTTP_429_RATE_LIMITED", "rate limited or quota exhausted"
        if code and code >= 500:
            return "HTTP_5XX_SERVER", f"provider returned {code}"
        return f"HTTP_{code}", redact(str(exc))[:200]

    if openai is not None and isinstance(exc, openai.BadRequestError):
        return "MALFORMED_REQUEST", redact(str(exc))[:200]

    # Walk the chain for transport errors
    for e in chain:
        if isinstance(e, socket.gaierror):
            return "DNS_FAILURE", f"cannot resolve host ({e})"
        if isinstance(e, ConnectionRefusedError):
            return "CONNECTION_REFUSED", "target port closed (relay/server down?)"
        if isinstance(e, ConnectionResetError):
            return "CONNECTION_RESET", "peer reset (firewall/proxy?)"
        if isinstance(e, (ssl.SSLCertVerificationError, ssl.CertificateError)):
            return "TLS_FAILURE", f"certificate problem ({e})"
        if isinstance(e, ssl.SSLError):
            return "TLS_FAILURE", f"handshake failed ({e})"
        if isinstance(e, httpx.ProxyError):
            return "PROXY_FAILURE", redact(str(e))[:200] or "proxy refused CONNECT"
        if isinstance(e, httpx.ConnectTimeout):
            return "CONNECT_TIMEOUT", "TCP connect exceeded timeout (proxy blackhole?)"
        if isinstance(e, httpx.ReadTimeout):
            return "READ_TIMEOUT", "server accepted connection but never answered"
        if isinstance(e, (socket.timeout, TimeoutError)):
            return "CONNECT_TIMEOUT", str(e) or "generic timeout"

    names = " <- ".join(type(e).__name__ for e in chain)
    return "UNKNOWN", redact(f"{names}: {exc}")[:300]


def target_of(exc):
    """scheme://host:port of the failed request."""
    for e in _chain(exc):
        req = getattr(e, "request", None)
        url = getattr(req, "url", None)
        if url is not None:
            port = url.port or (443 if url.scheme == "https" else 80)
            return f"{url.scheme}://{url.host}:{port}"
    return "unknown-host"


def log_request_failure(log, exc, attempt, started):
    """Log the full classified failure with traceback."""
    category, detail = classify_failure(exc)
    log.error(
        "Connection failure | Category: %s | Exception type: %s | "
        "Detail: %s | Target: %s | Elapsed: %.2f s | Attempt: %d/%d | "
        "Traceback:\n%s",
        category,
        type(exc).__name__,
        detail,
        target_of(exc),
        time.time() - started,
        attempt,
        AI_MAX_RETRIES,
        redact(traceback.format_exc()),
    )
    return category


def _retryable(category):
    """Auth/config/parse errors cannot be fixed by retrying."""
    return category not in {
        "HTTP_401_UNAUTHORIZED",
        "HTTP_403_FORBIDDEN",
        "HTTP_404_NOT_FOUND",
        "MALFORMED_REQUEST",
        "RESPONSE_PARSE_ERROR",
        "SDK_CONFIG",
    }


# ---------------- startup environment probe ----------------
def log_network_environment(log):
    """Call once at startup to print network config and probe connectivity."""
    proxy_vars = {k: redact(v) for k, v in os.environ.items() if "proxy" in k.lower()}
    log.info("NET-ENV | proxy vars in effect: %s", proxy_vars or "none")

    if openai is not None:
        try:
            c = openai.OpenAI(api_key=os.getenv("API_KEY", "not-needed"))
            log.info(
                "NET-ENV | SDK default base_url: %s | max_retries: %s | timeout: %s",
                c.base_url,
                c.max_retries,
                c.timeout,
            )
            host = str(c.base_url.host) if hasattr(c.base_url, "host") else str(c.base_url).split("/")[2].split(":")[0]
            # DNS probe
            t0 = time.time()
            try:
                infos = socket.getaddrinfo(host, 443)
                log.info(
                    "NET-PROBE | DNS for %s OK in %.2f s (%d record(s))",
                    host,
                    time.time() - t0,
                    len(infos),
                )
            except socket.gaierror as e:
                log.error(
                    "NET-PROBE | DNS for %s FAILED in %.2f s: %s",
                    host,
                    time.time() - t0,
                    e,
                )
            # TCP probe
            t0 = time.time()
            try:
                s = socket.create_connection((host, 443), timeout=5)
                s.close()
                log.info(
                    "NET-PROBE | TCP connect %s:443 OK in %.2f s",
                    host,
                    time.time() - t0,
                )
            except OSError as e:
                log.error(
                    "NET-PROBE | TCP connect %s:443 FAILED in %.2f s: %s",
                    host,
                    time.time() - t0,
                    e,
                )
        except Exception as e:
            log.error(
                "NET-ENV | SDK config probe failed: %s (%s)",
                redact(str(e)),
                type(e).__name__,
            )
