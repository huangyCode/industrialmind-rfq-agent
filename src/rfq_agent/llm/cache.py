"""Record / replay cache for LLM calls (DESIGN §7.3). Replay data must be real recorded model output."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

MODES = ("live", "record", "replay", "auto")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cache_key(
    *,
    provider: str,
    model: str,
    task: str,
    prompt_version: str,
    system: str,
    user_text: str,
    images: list[Path],
    schema: dict | None,
) -> str:
    """sha256 over everything that can change the model's answer."""
    payload = {
        "provider": provider,
        "model": model,
        "task": task,
        "prompt_version": prompt_version,
        "system": system,
        "user_text": user_text,
        "images": [file_sha256(p) for p in images],
        "schema": schema,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


class LLMCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, task: str, key: str) -> Path:
        return self.root / task / f"{key[:16]}.json"

    def get(self, task: str, key: str) -> dict | None:
        p = self.path(task, key)
        if not p.exists():
            return None
        entry = json.loads(p.read_text(encoding="utf-8"))
        # key[:16] collisions are practically impossible, but verify when the full key is stored
        if entry.get("request_meta", {}).get("key", key) != key:
            return None
        return entry

    def put(
        self,
        task: str,
        key: str,
        *,
        request_meta: dict,
        raw_response: str,
        parsed: dict | str | None,
        usage: dict,
    ) -> Path:
        p = self.path(task, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "request_meta": {**request_meta, "key": key},
            "raw_response": raw_response,
            "parsed": parsed,
            "usage": usage,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }
        p.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
        return p
