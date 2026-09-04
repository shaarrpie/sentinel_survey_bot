"""
WebDriver session lifecycle guard.

Prevents the "invalid session id spam loop" by:
  - Detecting session death via typed exceptions (InvalidSessionIdException,
    NoSuchWindowException, WebDriverException with known messages).
  - Maintaining a state machine (STARTING -> BROWSER_READY -> WAITING_FOR_F12
    -> SURVEY_STARTED -> QUESTION_DETECTION -> AI_DECISION -> ANSWER_ACTION ->
    WAITING_FOR_NAVIGATION -> WEBDRIVER_FAILURE -> RECOVERY -> STOPPED).
  - Rate-limited health checks (free when session is healthy; full probe
    when needed).
  - Bounded recovery: relaunch browser up to N times, then stop cleanly.
  - Crash reports written to <LOG_DIR>/webdriver_failure_<timestamp>.log.
  - Heartbeat logging so a real hang is visible as a missing heartbeat.

No secrets are logged. URLs are trimmed. No cookies/keys/headers are
ever formatted.
"""

import os
import platform
import time
import traceback
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Optional


# ── Configuration ──────────────────────────────────────────────────
WD_SESSION_FAILURE_THRESHOLD = int(os.getenv("WD_SESSION_FAILURE_THRESHOLD", "3"))
WD_MAX_RECOVERY_ATTEMPTS = int(os.getenv("WD_MAX_RECOVERY_ATTEMPTS", "2"))
WD_RECOVERY_BACKOFF = float(os.getenv("WD_RECOVERY_BACKOFF", "5"))
WD_HEALTH_INTERVAL = float(os.getenv("WD_HEALTH_INTERVAL", "5"))
WD_HEARTBEAT_INTERVAL = float(os.getenv("WD_HEARTBEAT_INTERVAL", "30"))


class BotState(Enum):
    STARTING = "STARTING"
    BROWSER_READY = "BROWSER_READY"
    WAITING_FOR_F12 = "WAITING_FOR_F12"
    SURVEY_STARTED = "SURVEY_STARTED"
    QUESTION_DETECTION = "QUESTION_DETECTION"
    AI_DECISION = "AI_DECISION"
    ANSWER_ACTION = "ANSWER_ACTION"
    WAITING_FOR_NAVIGATION = "WAITING_FOR_NAVIGATION"
    WEBDRIVER_FAILURE = "WEBDRIVER_FAILURE"
    RECOVERY = "RECOVERY"
    STOPPED = "STOPPED"


class HealthStatus(Enum):
    SESSION_HEALTHY = "SESSION_HEALTHY"
    SESSION_UNRESPONSIVE = "SESSION_UNRESPONSIVE"
    SESSION_INVALID = "SESSION_INVALID"
    BROWSER_CLOSED = "BROWSER_CLOSED"
    DRIVER_PROCESS_DEAD = "DRIVER_PROCESS_DEAD"
    UNKNOWN = "UNKNOWN"


# Messages that indicate session death
SESSION_DEATH_PATTERNS = [
    "invalid session id",
    "invalid session",
    "no such window",
    "no active session",
    "session deleted",
    "not connected to devtools",
    "chrome not reachable",
    "tab crashed",
    "aw, snap",
]


class SessionDeadError(Exception):
    """Raised when the WebDriver session is confirmed dead."""

    def __init__(self, health: HealthStatus, detail: str = ""):
        self.health = health
        self.detail = detail
        super().__init__(f"{health.value}: {detail}")


def is_session_death(exc: Exception) -> bool:
    """Check if an exception indicates session death."""
    msg = str(exc).lower()
    for pattern in SESSION_DEATH_PATTERNS:
        if pattern in msg:
            return True
    # Check exception type names
    type_name = type(exc).__name__.lower()
    if "invalidsession" in type_name:
        return True
    if "nosuchwindow" in type_name:
        return True
    return False


class SessionGuard:
    """Tracks WebDriver session state and detects death."""

    def __init__(self, log: logging.Logger):
        self.log = log
        self.state: BotState = BotState.STARTING
        self._state_since: float = time.time()
        self.iteration: int = 0
        self.poll_count: int = 0
        self.consecutive_poll_failures: int = 0
        self.last_successful_command: str = ""
        self._last_success_at: float = time.time()
        self.last_known_url: Optional[str] = None
        self.recovery_attempts: int = 0
        self.driver: Any = None
        self._last_health_check: float = 0.0
        self._last_heartbeat: float = 0.0
        self._healthy: bool = False

    def attach(self, driver: Any) -> None:
        """Attach a new driver instance."""
        self.driver = driver
        self._healthy = True
        self._last_success_at = time.time()
        self._last_health_check = 0.0  # Force immediate health check
        self._last_heartbeat = time.time()

    def set_state(self, new_state: BotState) -> None:
        """Transition state, logging the change."""
        if new_state == self.state:
            return
        elapsed = time.time() - self._state_since
        if elapsed > 0.1:
            self.log.info(
                f"STATE: {self.state.value} -> {new_state.value} (after {elapsed:.1f}s)",
                extra={"stage": "WDGuard"},
            )
        else:
            self.log.debug(
                f"STATE: {self.state.value} -> {new_state.value}",
                extra={"stage": "WDGuard"},
            )
        self.state = new_state
        self._state_since = time.time()

    def trace(self, command: str, func, *args, **kwargs):
        """Wrap a WebDriver command with tracing and failure detection."""
        self.last_successful_command = command
        start = time.perf_counter()
        try:
            result = func(*args, **kwargs)
            elapsed = time.perf_counter() - start
            self._last_success_at = time.time()
            self.log.debug(
                f"WD CMD | command={command} | elapsed={elapsed:.3f}s | "
                f"session_valid=YES",
                extra={"stage": "WDTrace"},
            )
            return result
        except Exception as e:
            elapsed = time.perf_counter() - start
            session_valid = "NO"
            if is_session_death(e):
                session_valid = "NO"
                self.log.error(
                    f"WD CMD | command={command} | elapsed={elapsed:.3f}s | "
                    f"session_valid={session_valid} | "
                    f"exception={type(e).__name__}: {str(e)[:200]}",
                    extra={"stage": "WDTrace"},
                )
                raise SessionDeadError(
                    self._classify_death(e), str(e)[:300]
                ) from e
            session_valid = "UNKNOWN"
            self.log.debug(
                f"WD CMD | command={command} | elapsed={elapsed:.3f}s | "
                f"session_valid={session_valid} | "
                f"exception={type(e).__name__}: {str(e)[:200]}",
                extra={"stage": "WDTrace"},
            )
            raise

    def _classify_death(self, exc: Exception) -> HealthStatus:
        """Classify the type of session death."""
        msg = str(exc).lower()
        exc_name = type(exc).__name__

        # Check for browser closed patterns
        if "chrome not reachable" in msg or "browser" in msg and "closed" in msg:
            # Check if it's really the browser process
            if self.driver:
                try:
                    # This will fail if browser process is dead
                    self.driver.session_id
                    return HealthStatus.SESSION_INVALID
                except Exception:
                    return HealthStatus.BROWSER_CLOSED
            return HealthStatus.BROWSER_CLOSED

        if "invalid session" in msg:
            return HealthStatus.SESSION_INVALID

        if "no such window" in msg:
            return HealthStatus.BROWSER_CLOSED

        if "not connected to devtools" in msg:
            return HealthStatus.DRIVER_PROCESS_DEAD

        if exc_name == "InvalidSessionIdException":
            return HealthStatus.SESSION_INVALID

        if exc_name == "NoSuchWindowException":
            return HealthStatus.BROWSER_CLOSED

        return HealthStatus.UNKNOWN

    def health(self, force: bool = False) -> HealthStatus:
        """Check session health. Rate-limited unless forced."""
        now = time.time()
        if not force and (now - self._last_health_check) < WD_HEALTH_INTERVAL:
            return HealthStatus.SESSION_HEALTHY if self._healthy else HealthStatus.UNKNOWN
        self._last_health_check = now

        if not self.driver:
            return HealthStatus.UNKNOWN

        # Free checks first (no driver round-trips)
        if not getattr(self.driver, "session_id", None):
            return HealthStatus.DRIVER_PROCESS_DEAD

        # If the driver process is still alive, do a lightweight probe
        try:
            self.driver.current_url
            self._healthy = True
            self._last_success_at = time.time()
            return HealthStatus.SESSION_HEALTHY
        except Exception as e:
            if is_session_death(e):
                self._healthy = False
                return self._classify_death(e)
            return HealthStatus.SESSION_UNRESPONSIVE

    def note_poll_failure(self) -> bool:
        """Record a transient poll failure. Returns True if threshold reached."""
        self.consecutive_poll_failures += 1
        self.log.debug(
            f"POLL FAILURE | count={self.consecutive_poll_failures}/"
            f"{WD_SESSION_FAILURE_THRESHOLD} | "
            f"command={self.last_successful_command}",
            extra={"stage": "WDGuard"},
        )
        if self.consecutive_poll_failures >= WD_SESSION_FAILURE_THRESHOLD:
            return True
        return False

    def reset_poll_failures(self) -> None:
        """Reset the poll failure counter on success."""
        if self.consecutive_poll_failures > 0:
            self.log.debug(
                f"POLL FAILURE counter reset (was {self.consecutive_poll_failures})",
                extra={"stage": "WDGuard"},
            )
        self.consecutive_poll_failures = 0

    def maybe_heartbeat(self, poll_count: int) -> None:
        """Emit a heartbeat at regular intervals."""
        now = time.time()
        if now - self._last_heartbeat >= WD_HEARTBEAT_INTERVAL:
            self._last_heartbeat = now
            elapsed_since_success = now - self._last_success_at
            self.log.debug(
                f"HEARTBEAT | state={self.state.value} | poll={poll_count} | "
                f"last_cmd={self.last_successful_command} | "
                f"last_success={elapsed_since_success:.1f}s ago",
                extra={"stage": "WDGuard"},
            )

    def diagnose(self, sde: SessionDeadError) -> dict:
        """Build a diagnostic snapshot at failure time."""
        snap = {
            "state": self.state.value,
            "iteration": self.iteration,
            "poll_count": self.poll_count,
            "health": sde.health.value if hasattr(sde, "health") else HealthStatus.UNKNOWN.value,
            "detail": sde.detail[:500] if hasattr(sde, "detail") else str(sde)[:500],
            "consecutive_failures": self.consecutive_poll_failures,
            "last_successful_command": self.last_successful_command,
            "seconds_since_last_success": round(time.time() - self._last_success_at, 1),
            "last_known_url": self.last_known_url,
            "recovery_attempts": self.recovery_attempts,
        }

        # Try to get URL/title/window handles — but only if still alive
        if self.driver:
            try:
                snap["current_url"] = str(self.driver.current_url)[:200]
            except Exception:
                snap["current_url"] = "Unable to retrieve URL: session invalid"
            try:
                snap["title"] = str(self.driver.title or "")[:200]
            except Exception:
                snap["title"] = "Unable to retrieve title: session invalid"
            try:
                snap["window_count"] = len(self.driver.window_handles)
            except Exception:
                snap["window_count"] = "Unable to retrieve windows: session invalid"
        else:
            snap["current_url"] = "driver is None"
            snap["title"] = "driver is None"
            snap["window_count"] = "driver is None"

        # Driver versions (read from local metadata, no driver round-trips)
        snap["versions"] = {}
        try:
            snap["versions"]["python"] = platform.python_version()
        except Exception:
            pass
        try:
            snap["versions"]["os"] = f"{platform.system()} {platform.release()}"
        except Exception:
            pass

        return snap

    def log_failure_banner(self, snap: dict) -> None:
        """Log the prominent failure banner."""
        self.log.error("=" * 60, extra={"stage": "WDGuard"})
        self.log.error("WEBDRIVER SESSION FAILURE", extra={"stage": "WDGuard"})
        self.log.error("=" * 60, extra={"stage": "WDGuard"})
        self.log.error(f"  State: {snap['state']}", extra={"stage": "WDGuard"})
        self.log.error(f"  Health: {snap['health']}", extra={"stage": "WDGuard"})
        self.log.error(f"  Detail: {snap['detail']}", extra={"stage": "WDGuard"})
        self.log.error(f"  Last URL: {snap['last_known_url']}", extra={"stage": "WDGuard"})
        self.log.error(f"  Last command: {snap['last_successful_command']}", extra={"stage": "WDGuard"})
        self.log.error(f"  Last success: {snap['seconds_since_last_success']}s ago", extra={"stage": "WDGuard"})
        self.log.error(f"  Consecutive failures: {snap['consecutive_failures']}", extra={"stage": "WDGuard"})
        self.log.error(
            f"  Versions: {snap.get('versions', {})}",
            extra={"stage": "WDGuard"},
        )
        self.log.error("=" * 60, extra={"stage": "WDGuard"})

        # Console banner too
        print("\n" + "=" * 60)
        print("WEBDRIVER SESSION FAILURE")
        print(f"  Health:  {snap['health']}")
        print(f"  Detail:  {snap['detail']}")
        print(f"  Last URL: {snap['last_known_url']}")
        print(f"  Last cmd: {snap['last_successful_command']} ({snap['seconds_since_last_success']}s ago)")
        print(f"  Recovery: attempt {snap['recovery_attempts'] + 1}/{WD_MAX_RECOVERY_ATTEMPTS}")
        print("=" * 60)

    def write_crash_report(self, snap: dict, sde: SessionDeadError) -> Optional[str]:
        """Write a detailed crash report to a file. Returns the path."""
        try:
            log_dir = os.getenv("DEBUG_LOG_DIR", "logs")
            os.makedirs(log_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(log_dir, f"webdriver_failure_{ts}.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write("=" * 60 + "\n")
                f.write("WEBDRIVER FAILURE CRASH REPORT\n")
                f.write(f"Generated: {datetime.now().isoformat()}\n")
                f.write("=" * 60 + "\n\n")
                f.write("--- Snapshot ---\n")
                for k, v in snap.items():
                    if k == "versions":
                        f.write(f"versions:\n")
                        for vk, vv in v.items():
                            f.write(f"  {vk}: {vv}\n")
                    else:
                        f.write(f"{k}: {v}\n")
                f.write("\n--- Exception Chain ---\n")
                f.write(f"Type: {type(sde).__name__}\n")
                f.write(f"Message: {str(sde)[:500]}\n")
                f.write("\n--- Full Traceback ---\n")
                f.write(traceback.format_exc())
                f.write("\n--- Config ---\n")
                f.write(f"WD_SESSION_FAILURE_THRESHOLD: {WD_SESSION_FAILURE_THRESHOLD}\n")
                f.write(f"WD_MAX_RECOVERY_ATTEMPTS: {WD_MAX_RECOVERY_ATTEMPTS}\n")
                f.write(f"WD_RECOVERY_BACKOFF: {WD_RECOVERY_BACKOFF}\n")
                f.write(f"WD_HEALTH_INTERVAL: {WD_HEALTH_INTERVAL}\n")
                f.write(f"WD_HEARTBEAT_INTERVAL: {WD_HEARTBEAT_INTERVAL}\n")
            self.log.info(f"Crash report written: {path}", extra={"stage": "WDGuard"})
            return path
        except Exception as e:
            self.log.error(f"Failed to write crash report: {e}", extra={"stage": "WDGuard"})
            return None
