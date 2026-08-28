import json
import os
import hashlib
from typing import List, Optional


class AnswerCache:
    def __init__(self, path: str):
        self.path = path
        self._data = {}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
        except Exception:
            self._data = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception:
            pass

    def _make_key(self, page_text: str, options: List[str]) -> str:
        raw = page_text + "|" + "|".join(options[:10])
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, page_text: str, options: List[str]) -> Optional[str]:
        key = self._make_key(page_text, options)
        entry = self._data.get(key)
        if entry:
            try:
                data = json.loads(entry)
                return data.get("answer") or data.get("memory_note")
            except Exception:
                return entry
        return None

    def set(self, page_text: str, options: List[str], value: str):
        key = self._make_key(page_text, options)
        self._data[key] = value
        self._save()
