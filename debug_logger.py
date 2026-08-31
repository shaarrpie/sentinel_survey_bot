"""
Structured debug logging for the survey loop.

Provides millisecond-precision timestamps, iteration tracking, timing
measurements, and stuck-loop detection. All secrets are redacted.
"""

import copy
import functools
import logging
import os
import platform
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Configuration (override via .env)
# ---------------------------------------------------------------------------
DEBUG_LOGGING = os.getenv("DEBUG_LOGGING", "0") == "1"
STUCK_LOOP_TIMEOUT = float(os.getenv("STUCK_LOOP_TIMEOUT", "120"))  # seconds
STUCK_LOOP_MAX_ITERATIONS = int(os.getenv("STUCK_LOOP_MAX_ITERATIONS", "20"))
WAIT_TIMEOUT = float(os.getenv("WAIT_TIMEOUT", "30"))  # seconds
LOG_DIR = os.getenv("DEBUG_LOG_DIR", "logs")
LOG_FILE = os.path.join(LOG_DIR, "debug.log")

# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------
_SECRET_PATTERNS = [
    re.compile(r'(api[_-]?key\s*[=:]\s*)["\']?[\w\-]+["\']?', re.I),
    re.compile(r'(token\s*[=:]\s*)["\']?[\w\-]+["\']?', re.I),
    re.compile(r'(authorization\s*[=:]\s*(?:bearer\s+)?)["\']?[\w\-\.]+["\']?', re.I),
    re.compile(r'(cookie\s*[=:]\s*)["\']?[^"\'\s;]+', re.I),
]
_SECRET_REDACTED = "[REDACTED]"


def _redact(value: Any) -> str:
    """Return a string representation with secrets scrubbed."""
    s = str(value)
    for pat in _SECRET_PATTERNS:
        s = pat.sub(rf"\1{_SECRET_REDACTED}", s)
    return s


# ---------------------------------------------------------------------------
# Custom formatter with millisecond precision
# ---------------------------------------------------------------------------
class SurveyFormatter(logging.Formatter):
    """Format: 2026-08-31 17:05:12.381 | DEBUG | SurveyLoop | msg"""

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        record.asctime = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        # Add milliseconds
        ms = int(record.msecs)
        header = f"{record.asctime}.{ms:03d} | {record.levelname:7s} | {record.name}"
        if hasattr(record, "stage") and record.stage:
            header += f" | {record.stage}"
        return f"{header} | {record.message}"


# ---------------------------------------------------------------------------
# Logger setup
# ---------------------------------------------------------------------------
def _setup_file_handler() -> Optional[RotatingFileHandler]:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = RotatingFileHandler(
            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(SurveyFormatter())
        return fh
    except Exception:
        return None


def get_survey_logger(name: str = "SurveyLoop") -> logging.Logger:
    """Get a logger configured for survey-loop debugging."""
    log = logging.getLogger(name)
    if log.handlers:
        return log  # already configured

    log.setLevel(logging.DEBUG if DEBUG_LOGGING else logging.INFO)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(SurveyFormatter())
    ch.setLevel(logging.DEBUG if DEBUG_LOGGING else logging.INFO)
    log.addHandler(ch)

    # File handler
    fh = _setup_file_handler()
    if fh:
        fh.setLevel(logging.DEBUG)  # file always captures DEBUG
        log.addHandler(fh)

    log.propagate = False
    return log


# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------
class StageTimer:
    """Context manager that logs elapsed time for a named stage."""

    def __init__(self, log: logging.Logger, stage: str, threshold: float = 0.5):
        self.log = log
        self.stage = stage
        self.threshold = threshold
        self.start = 0.0
        self.elapsed = 0.0

    def __enter__(self):
        self.start = time.perf_counter()
        self.log.debug(f"[{self.stage}] START", extra={"stage": self.stage})
        return self

    def __exit__(self, *exc):
        self.elapsed = time.perf_counter() - self.start
        level = logging.WARNING if self.elapsed > self.threshold else logging.DEBUG
        self.log.log(
            level,
            f"[{self.stage}] END elapsed={self.elapsed:.3f}s",
            extra={"stage": self.stage},
        )
        if exc[0] is not None:
            self.log.error(
                f"[{self.stage}] EXCEPTION: {exc[1]}", extra={"stage": self.stage}
            )


def timed(log: logging.Logger, stage: str, threshold: float = 0.5):
    """Decorator that times a function call."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with StageTimer(log, stage, threshold):
                return func(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Stuck-loop detector
# ---------------------------------------------------------------------------
class StuckDetector:
    """Detects when the loop is stuck on the same state."""

    def __init__(self, log: logging.Logger):
        self.log = log
        self.iteration = 0
        self.last_url: Optional[str] = None
        self.last_fingerprint: Optional[str] = None
        self.last_question: Optional[str] = None
        self.same_state_since = 0.0
        self.same_state_count = 0

    def update(self, url: str, fingerprint: str, question: str) -> dict:
        """Update state and return diagnostics."""
        self.iteration += 1
        now = time.time()

        changed = []
        if url != self.last_url:
            changed.append("URL")
        if fingerprint != self.last_fingerprint:
            changed.append("FINGERPRINT")
        if question != self.last_question:
            changed.append("QUESTION")

        if changed:
            self.same_state_since = now
            self.same_state_count = 0
        else:
            self.same_state_count += 1

        self.last_url = url
        self.last_fingerprint = fingerprint
        self.last_question = question

        elapsed_stuck = now - self.same_state_since if self.same_state_since else 0

        diag = {
            "iteration": self.iteration,
            "url_changed": "URL" in changed,
            "fingerprint_changed": "FINGERPRINT" in changed,
            "question_changed": "QUESTION" in changed,
            "same_state_count": self.same_state_count,
            "stuck_elapsed_s": round(elapsed_stuck, 1),
        }

        # Warnings
        if elapsed_stuck > STUCK_LOOP_TIMEOUT:
            self.log.warning(
                f"POSSIBLE STUCK LOOP: unchanged for {elapsed_stuck:.0f}s "
                f"({self.same_state_count} iterations) | "
                f"url={url[:80]} | question={question[:80]}",
                extra={"stage": "StuckDetector"},
            )
        elif self.same_state_count > STUCK_LOOP_MAX_ITERATIONS:
            self.log.warning(
                f"POSSIBLE STUCK LOOP: {self.same_state_count} iterations "
                f"without change | url={url[:80]}",
                extra={"stage": "StuckDetector"},
            )

        return diag

    def summary(self) -> str:
        return (
            f"Iteration {self.iteration} | "
            f"same_state_count={self.same_state_count} | "
            f"stuck_for={time.time() - self.same_state_since:.1f}s"
            if self.same_state_since
            else f"Iteration {self.iteration}"
        )


# ---------------------------------------------------------------------------
# Startup diagnostics
# ---------------------------------------------------------------------------
def log_startup_diagnostics(log: logging.Logger):
    """Log safe system info at startup."""
    log.info("=" * 60, extra={"stage": "Startup"})
    log.info(f"Python: {sys.version}", extra={"stage": "Startup"})
    log.info(f"OS: {platform.system()} {platform.release()}", extra={"stage": "Startup"})
    log.info(f"Debug logging: {'ON' if DEBUG_LOGGING else 'OFF'}", extra={"stage": "Startup"})
    log.info(f"Stuck loop timeout: {STUCK_LOOP_TIMEOUT}s", extra={"stage": "Startup"})
    log.info(f"Stuck max iterations: {STUCK_LOOP_MAX_ITERATIONS}", extra={"stage": "Startup"})
    log.info(f"Wait timeout: {WAIT_TIMEOUT}s", extra={"stage": "Startup"})
    log.info(f"Log file: {LOG_FILE}", extra={"stage": "Startup"})
    log.info("=" * 60, extra={"stage": "Startup"})
