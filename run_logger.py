"""
Per-run structured logging for survey bot.

Creates a subfolder per bot type, with a log file per run detailing
what the bot saw and clicked at each step. Designed for easy grepping
and post-run analysis.

Directory structure:
    runs/
        swagbucks/
            2026-09-02_00-01-30_run.log
            2026-09-02_00-05-12_run.log
        prolific/
            2026-09-02_00-02-15_run.log
        generic/
            2026-09-02_00-03-00_run.log

Log format (grep-friendly):
    [2026-09-02 00:01:30.123] [QUESTION] What is your age?
    [2026-09-02 00:01:30.124] [TYPE] SELECT
    [2026-09-02 00:01:30.125] [OPTIONS] 0: "18-24" | 1: "25-34" | 2: "35-44"
    [2026-09-02 00:01:32.456] [AI_DECISION] Click option 1 (25-34) - matches persona age 28
    [2026-09-02 00:01:32.789] [ACTION] CLICK idx=1 method=click success=True
    [2026-09-02 00:01:33.012] [PAGE_STATE] url=https://example.com/q2 fingerprint=abc123
"""

import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

RUNS_DIR = os.getenv("RUNS_DIR", "runs")
SESSION_FILE = "session.json"


class RunLogger:
    """Structured logger for a single bot run.

    Logs every step in a grep-friendly format with timestamps,
    making it easy to search for specific questions, actions,
    or outcomes.
    """

    def __init__(self, bot_type: str, run_id: Optional[str] = None):
        self.bot_type = bot_type.lower().strip() or "generic"
        self.run_id = run_id or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.start_time = datetime.datetime.now()
        self.step_count = 0

        self.run_dir = Path(RUNS_DIR) / self.bot_type
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.log_file = self.run_dir / f"{self.run_id}_run.log"
        self.json_file = self.run_dir / f"{self.run_id}_run.jsonl"

        self._log = logging.getLogger(f"run.{self.bot_type}.{self.run_id}")
        self._log.setLevel(logging.DEBUG)
        self._log.propagate = False

        if not self._log.handlers:
            fh = logging.FileHandler(self.log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter("%(message)s"))
            self._log.addHandler(fh)

        self._json_fh = open(self.json_file, "w", encoding="utf-8")

        self._session = {
            "run_id": self.run_id,
            "bot_type": self.bot_type,
            "started": self.start_time.isoformat(),
            "steps": [],
        }

        self._write_header()

    def _write_header(self):
        self._log.info(f"{'='*60}")
        self._log.info(f"[RUN START] {self.run_id} | bot_type={self.bot_type}")
        self._log.info(f"[START TIME] {self.start_time.isoformat()}")
        self._log.info(f"{'='*60}")

    def _timestamp(self) -> str:
        now = datetime.datetime.now()
        elapsed = (now - self.start_time).total_seconds()
        return f"[{now.strftime('%Y-%m-%d %H:%M:%S')}.{now.microsecond // 1000:03d}] [{elapsed:07.2f}s]"

    def _log_step(self, stage: str, message: str, data: Optional[dict] = None):
        self.step_count += 1
        line = f"{self._timestamp()} [{stage}] {message}"
        self._log.info(line)

        entry = {
            "step": self.step_count,
            "timestamp": datetime.datetime.now().isoformat(),
            "stage": stage,
            "message": message,
        }
        if data:
            entry["data"] = data

        self._session["steps"].append(entry)
        try:
            self._json_fh.write(json.dumps(entry, default=str) + "\n")
            self._json_fh.flush()
        except Exception:
            pass

    def log_question(self, question: str, question_type: str, options: list):
        """Log a detected question with its type and options."""
        opt_str = " | ".join(f'{i}: "{opt.get("text", "")[:50]}"' for i, opt in enumerate(options))
        self._log_step("QUESTION", question[:200])
        self._log_step("TYPE", question_type)
        self._log_step("OPTIONS", opt_str, {"options": options})

    def log_ai_decision(self, reasoning: str, chosen_index: int, chosen_text: str):
        """Log what the AI decided and why."""
        msg = f'Option {chosen_index}: "{chosen_text[:50]}" — {reasoning}'
        self._log_step("AI_DECISION", msg)

    def log_action(self, action_type: str, **kwargs):
        """Log a bot action (click, type, select, etc.)."""
        parts = [f"{k}={v}" for k, v in kwargs.items()]
        self._log_step("ACTION", f"{action_type} {' '.join(parts)}")

    def log_page_state(self, url: str, fingerprint: str = ""):
        """Log current page state after an action."""
        self._log_step("PAGE_STATE", f"url={url[:150]} fingerprint={fingerprint[:32]}")

    def log_native_api(self, api_name: str, success: bool):
        """Log native API call result."""
        status = "OK" if success else "FAIL"
        self._log_step("NATIVE_API", f"{api_name} status={status}")

    def log_error(self, error_msg: str, context: str = ""):
        """Log an error with optional context."""
        msg = f"{context}: {error_msg}" if context else error_msg
        self._log_step("ERROR", msg)

    def log_warning(self, warning_msg: str):
        """Log a warning."""
        self._log_step("WARNING", warning_msg)

    def log_disqualification(self, reason: str = ""):
        """Log disqualification event."""
        self._log_step("DISQUALIFIED", reason or "Disqualification detected")

    def log_completion(self, reward: str = ""):
        """Log survey completion."""
        elapsed = (datetime.datetime.now() - self.start_time).total_seconds()
        msg = f"elapsed={elapsed:.1f}s"
        if reward:
            msg += f" reward={reward}"
        self._log_step("COMPLETED", msg)

    def log_raw(self, stage: str, message: str):
        """Log raw message with a stage tag."""
        self._log_step(stage, message)

    def close(self):
        """Finalize the run log."""
        elapsed = (datetime.datetime.now() - self.start_time).total_seconds()
        self._log.info(f"{'='*60}")
        self._log.info(f"[RUN END] {self.run_id} | elapsed={elapsed:.1f}s | steps={self.step_count}")
        self._log.info(f"{'='*60}")

        self._session["ended"] = datetime.datetime.now().isoformat()
        self._session["elapsed_seconds"] = elapsed
        self._session["total_steps"] = self.step_count

        try:
            self._json_fh.close()
        except Exception:
            pass

        session_path = self.run_dir / f"{self.run_id}_session.json"
        try:
            with open(session_path, "w", encoding="utf-8") as f:
                json.dump(self._session, f, indent=2, default=str)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def detect_bot_type(url: str) -> str:
    """Detect bot type from URL for folder organization."""
    if not url:
        return "generic"

    url_lower = url.lower()

    patterns = {
        "swagbucks": ["swagbucks.com", "sbix", "swagcode"],
        "prolific": ["prolific.co"],
        "qualtrics": ["qualtrics.com"],
        "surveymonkey": ["surveymonkey.com"],
        "typeform": ["typeform.com"],
        "toluna": ["toluna.com"],
        "lifepoints": ["lifepoints.com", "lifepanel"],
        "prizerebel": ["prizerebel.com"],
        "yoodlize": ["yoodlize.com"],
        "cpzresearch": ["cpzresearch.com", "cpxresearch"],
        "dynata": ["dynata.com"],
        "lucid": ["lucidhq.com", " sampler"],
        "intellizoom": ["intellizoom.com"],
        "userinterviews": ["userinterviews.com"],
        "respondent": ["respondent.io"],
        "surveyrouter": ["surveyrouter.com", "router"],
    }

    for bot_type, keywords in patterns.items():
        for kw in keywords:
            if kw in url_lower:
                return bot_type

    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.lower()
        domain = re.sub(r"^www\.", "", domain)
        domain = domain.split(".")[0]
        if domain and domain != "localhost":
            return domain
    except Exception:
        pass

    return "generic"


def list_runs(bot_type: Optional[str] = None, limit: int = 20) -> list:
    """List recent runs, optionally filtered by bot type."""
    runs = []
    base = Path(RUNS_DIR)

    if bot_type:
        search_dirs = [base / bot_type] if (base / bot_type).exists() else []
    else:
        search_dirs = [d for d in base.iterdir() if d.is_dir()] if base.exists() else []

    for d in search_dirs:
        for log_file in d.glob("*_run.log"):
            runs.append({
                "bot_type": d.name,
                "run_id": log_file.stem.replace("_run", ""),
                "log_file": str(log_file),
                "modified": datetime.datetime.fromtimestamp(log_file.stat().st_mtime).isoformat(),
            })

    runs.sort(key=lambda r: r["modified"], reverse=True)
    return runs[:limit]


def grep_runs(pattern: str, bot_type: Optional[str] = None) -> list:
    """Search all run logs for a pattern. Returns matching lines."""
    results = []
    base = Path(RUNS_DIR)

    if bot_type:
        search_dirs = [base / bot_type] if (base / bot_type).exists() else []
    else:
        search_dirs = [d for d in base.iterdir() if d.is_dir()] if base.exists() else []

    for d in search_dirs:
        for log_file in d.glob("*_run.log"):
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    for line_num, line in enumerate(f, 1):
                        if pattern.lower() in line.lower():
                            results.append({
                                "bot_type": d.name,
                                "run_id": log_file.stem.replace("_run", ""),
                                "line": line_num,
                                "content": line.strip(),
                            })
            except Exception:
                pass

    return results
