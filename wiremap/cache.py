"""Per-file extraction cache (ROADMAP 2.1 — incremental scans).

Stores the serializable per-file parse results of both extractors in
`.wiremap/cache.json`, keyed by relative path and validated by a sha256
content hash. On re-scan only changed files are re-parsed; the matcher and
risk engine always re-run. Bump CACHE_VERSION whenever an extractor's
per-file output shape changes — stale-format entries are then discarded
wholesale instead of being misread.
"""
from __future__ import annotations

import hashlib
import json
import os

CACHE_VERSION = 2   # v2: backend pydantic_models + response_model, frontend expected_fields


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FileCache:
    """Content-hash-keyed store of per-file extraction results.

    Sections ("backend", "frontend") keep the two extractors' entries
    apart so a file visible to both walks cannot collide.
    """

    def __init__(self, path: str | None = None):
        self.path = path
        self._sections: dict[str, dict] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict) and raw.get("version") == CACHE_VERSION:
                    self._sections = raw.get("sections", {})
            except (json.JSONDecodeError, OSError):
                pass  # unreadable cache is equivalent to no cache

    def get(self, section: str, rel: str, sha: str):
        """Return the cached parse result, or None if absent or stale."""
        entry = self._sections.get(section, {}).get(rel)
        if entry and entry.get("sha") == sha:
            return entry["data"]
        return None

    def put(self, section: str, rel: str, sha: str, data) -> None:
        self._sections.setdefault(section, {})[rel] = {"sha": sha, "data": data}

    def prune(self, section: str, keep: set[str]) -> None:
        """Drop entries for files that no longer exist on disk."""
        sec = self._sections.get(section, {})
        for rel in list(sec):
            if rel not in keep:
                del sec[rel]

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"version": CACHE_VERSION, "sections": self._sections}, f)
