import json
import logging
import os
import time
import hashlib
from typing import List, Optional, Union

logger = logging.getLogger(__name__)


class AnswerCache:
    """Persistent per-profile Q&A cache.

    Storage format is a dict of key -> record. Records written by this
    version are plain dicts ({"answer": ..., "ts": ...}); older versions
    wrote JSON-encoded strings and even bare strings, so get() resolves
    all three instead of relying on json.loads throwing and the except
    returning the raw value — "works by accident" is not a contract.
    """

    def __init__(self, path: str):
        self.path = path
        self._data = {}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self._data = loaded if isinstance(loaded, dict) else {}
        except Exception:
            logger.warning("could not load answer cache at %s — starting empty",
                           self.path, exc_info=True)
            self._data = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception:
            logger.warning("could not save answer cache at %s", self.path, exc_info=True)

    def _make_key(self, page_text: str, options: List[str]) -> str:
        # Use ALL options to prevent key collision on large dropdowns
        # Hash the page text and options separately for better distribution
        page_hash = hashlib.sha256(page_text.encode()).hexdigest()[:16]
        options_hash = hashlib.sha256("|".join(options).encode()).hexdigest()[:16]
        return f"{page_hash}:{options_hash}"

    @staticmethod
    def _extract_answer(entry: Union[dict, str]) -> Optional[str]:
        if isinstance(entry, dict):
            return (entry.get("answer") or entry.get("memory_note")
                    or entry.get("answer_summary"))
        if isinstance(entry, str):
            try:
                data = json.loads(entry)
            except (ValueError, TypeError):
                return entry or None     # legacy bare string
            if isinstance(data, dict):
                return (data.get("answer") or data.get("memory_note")
                        or data.get("answer_summary"))
        return None

    def get(self, page_text: str, options: List[str]) -> Optional[str]:
        key = self._make_key(page_text, options)
        entry = self._data.get(key)
        if entry is None:
            return None
        return self._extract_answer(entry)

    def set(self, page_text: str, options: List[str],
            value: Union[str, dict]) -> None:
        key = self._make_key(page_text, options)
        if isinstance(value, dict):
            record = dict(value)
        else:
            record = {"answer": str(value)}
        record.setdefault("ts", time.time())
        self._data[key] = record
        self._save()
