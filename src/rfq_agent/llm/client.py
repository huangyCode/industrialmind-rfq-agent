"""LLMClient: structured / text calls with schema validation, feedback retries, record/replay and call logging."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from rfq_agent.config import FAILURE_DIR, LLM_CACHE_DIR, PROMPT_DIR, LLMSettings
from rfq_agent.llm.cache import MODES, LLMCache, cache_key
from rfq_agent.llm.providers import Provider, make_provider

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)


class LLMError(RuntimeError):
    pass


class CacheMissError(LLMError):
    """Replay requested but no recording exists for this exact input."""


class ExtractionError(LLMError):
    """Model output never validated against the schema; raw output is kept as evidence."""

    def __init__(self, message: str, raw_output: str = "", failure_path: Path | None = None):
        super().__init__(message)
        self.raw_output = raw_output
        self.failure_path = failure_path


def load_prompt(name: str, version: str = "v1") -> str:
    return (PROMPT_DIR / f"{name}.{version}.md").read_text(encoding="utf-8")


def parse_json(raw: str) -> Any:
    """Parse model output as JSON, tolerating code fences and prose around one object."""
    text = _FENCE_RE.sub("", raw.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            return json.loads(text[i : j + 1])
        raise


def _feedback(errors: str) -> str:
    return (
        "Your previous answer did not validate against the required JSON schema.\n"
        f"Errors:\n{errors}\n\n"
        "Return the corrected JSON object only. Keep every value you read correctly; fix only the errors. "
        "Use null for anything not visible — do not invent values."
    )


class LLMClient:
    def __init__(
        self,
        provider: Provider,
        *,
        mode: str = "auto",
        cache: LLMCache | None = None,
        call_repo: Any = None,
        failure_dir: Path = FAILURE_DIR,
        max_tokens: int = 8192,
    ):
        if mode not in MODES:
            raise LLMError(f"LLM_MODE must be one of {MODES}, got {mode!r}")
        self.provider = provider
        self.mode = mode
        self.cache = cache or LLMCache(LLM_CACHE_DIR)
        self.call_repo = call_repo  # LLMCallRepo or None
        self.failure_dir = Path(failure_dir)
        self.max_tokens = max_tokens
        self.stats = {"llm_calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0}
        # one record per structured/text call (cache hits report the latency measured when recorded)
        self.trace: list[dict] = []

    # ---------- public API ----------

    def structured(
        self,
        *,
        task: str,
        prompt_version: str,
        system: str,
        user_text: str,
        images: list[Path] = [],  # noqa: B006 - contract signature; never mutated
        schema: type[T],
        max_retries: int = 2,
        rfq_id: str | None = None,
        max_tokens: int | None = None,
    ) -> T:
        js = schema.model_json_schema()
        result = self._run(
            task=task,
            prompt_version=prompt_version,
            system=system,
            user_text=user_text,
            images=list(images),
            json_schema=js,
            validate=lambda raw: schema.model_validate(parse_json(raw)),
            max_retries=max_retries,
            rfq_id=rfq_id,
            max_tokens=max_tokens,
        )
        return result if isinstance(result, schema) else schema.model_validate(result)

    def text(
        self,
        *,
        task: str,
        prompt_version: str,
        system: str,
        user_text: str,
        rfq_id: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return self._run(
            task=task,
            prompt_version=prompt_version,
            system=system,
            user_text=user_text,
            images=[],
            json_schema=None,
            validate=lambda raw: raw.strip(),
            max_retries=0,
            rfq_id=rfq_id,
            max_tokens=max_tokens,
        )

    # ---------- internals ----------

    def _run(
        self,
        *,
        task,
        prompt_version,
        system,
        user_text,
        images,
        json_schema,
        validate,
        max_retries,
        rfq_id,
        max_tokens,
    ):
        model = self.provider.model_for(bool(images))
        key = cache_key(
            provider=self.provider.name,
            model=model,
            task=task,
            prompt_version=prompt_version,
            system=system,
            user_text=user_text,
            images=images,
            schema=json_schema,
        )
        log = {
            "rfq_id": rfq_id,
            "node": task,
            "provider": self.provider.name,
            "model": model,
            "prompt_version": prompt_version,
        }

        if self.mode in ("replay", "auto"):
            entry = self.cache.get(task, key)
            if entry is not None:
                self.stats["cache_hits"] += 1
                usage = entry.get("usage", {})
                meta = entry.get("request_meta", {})
                self._log(**log, cache_hit=1, attempts=0, ok=1, latency_ms=0, **usage)
                self.trace.append(
                    {
                        **log,
                        "cache_hit": True,
                        "cache_path": str(self.cache.path(task, key)),
                        "latency_ms": meta.get("latency_ms"),
                        "attempts": meta.get("attempts"),
                        "recorded_at": entry.get("recorded_at"),
                        **usage,
                    }
                )
                parsed = entry["parsed"]
                return validate(json.dumps(parsed) if json_schema is not None else parsed)
            if self.mode == "replay":
                raise CacheMissError(
                    f"No recorded LLM result for task '{task}' (key {key[:16]}, {self.provider.name}/{model}). "
                    "Run with LLM_MODE=auto or live and a reachable model to record it."
                )
            if not self.provider.available(model):
                raise CacheMissError(
                    f"No recorded LLM result for task '{task}' (key {key[:16]}) and {self.provider.name} "
                    f"model '{model}' is not reachable. Start the model server / set credentials, "
                    "or use inputs that have recordings."
                )

        t0 = time.perf_counter()
        raw, value, usage, attempts = self._live(
            task=task,
            key=key,
            model=model,
            system=system,
            user_text=user_text,
            images=images,
            json_schema=json_schema,
            validate=validate,
            max_retries=max_retries,
            max_tokens=max_tokens or self.max_tokens,
            log=log,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        self.trace.append(
            {
                **log,
                "cache_hit": False,
                "cache_path": str(self.cache.path(task, key)) if self.mode in ("record", "auto") else None,
                "latency_ms": latency_ms,
                "attempts": attempts,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                **usage,
            }
        )
        if self.mode in ("record", "auto"):
            self.cache.put(
                task,
                key,
                request_meta={
                    **log,
                    "user_text_chars": len(user_text),
                    "images": [Path(p).name for p in images],
                    "attempts": attempts,
                    "latency_ms": latency_ms,
                },
                raw_response=raw,
                parsed=value.model_dump(mode="json") if isinstance(value, BaseModel) else value,
                usage=usage,
            )
        return value

    def _live(
        self,
        *,
        task,
        key,
        model,
        system,
        user_text,
        images,
        json_schema,
        validate,
        max_retries,
        max_tokens,
        log,
    ):
        messages: list[dict] = [{"role": "user", "text": user_text, "images": images}]
        usage = {"input_tokens": 0, "output_tokens": 0}
        history: list[dict] = []
        t0 = time.perf_counter()
        raw = ""
        for attempt in range(1, max_retries + 2):
            self.stats["llm_calls"] += 1
            try:
                raw, u = self.provider.complete(
                    model=model, system=system, messages=messages, schema=json_schema, max_tokens=max_tokens
                )
            except Exception as e:
                self._log(
                    **log,
                    cache_hit=0,
                    attempts=attempt,
                    ok=0,
                    error=f"{type(e).__name__}: {e}"[:500],
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                    **usage,
                )
                raise
            for k in usage:
                usage[k] += int(u.get(k, 0))
            try:
                value = validate(raw)
            except (ValidationError, ValueError) as e:  # JSONDecodeError is a ValueError
                err = str(e)[:4000]
                history.append({"attempt": attempt, "raw_output": raw, "error": err})
                messages += [{"role": "assistant", "text": raw}, {"role": "user", "text": _feedback(err)}]
                continue
            self._account(usage)
            self._log(
                **log,
                cache_hit=0,
                attempts=attempt,
                ok=1,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                **usage,
            )
            return raw, value, usage, attempt

        self._account(usage)
        path = self._save_failure(task, key, log, user_text, images, history)
        msg = f"'{task}' output failed schema validation after {len(history)} attempts; evidence: {path}"
        self._log(
            **log,
            cache_hit=0,
            attempts=len(history),
            ok=0,
            error=history[-1]["error"][:500],
            latency_ms=int((time.perf_counter() - t0) * 1000),
            **usage,
        )
        raise ExtractionError(msg, raw_output=raw, failure_path=path)

    def _account(self, usage: dict) -> None:
        self.stats["input_tokens"] += usage["input_tokens"]
        self.stats["output_tokens"] += usage["output_tokens"]

    def _save_failure(self, task, key, log, user_text, images, history) -> Path:
        self.failure_dir.mkdir(parents=True, exist_ok=True)
        path = self.failure_dir / f"{datetime.now():%Y%m%dT%H%M%S}_{task}_{key[:8]}.json"
        path.write_text(
            json.dumps(
                {
                    **log,
                    "key": key,
                    "user_text": user_text,
                    "images": [str(p) for p in images],
                    "attempts": history,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def _log(self, **kw) -> None:
        if self.call_repo is None:
            return
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "rfq_id": kw.get("rfq_id"),
            "node": kw.get("node"),
            "provider": kw.get("provider"),
            "model": kw.get("model"),
            "prompt_version": kw.get("prompt_version"),
            "input_tokens": kw.get("input_tokens", 0),
            "output_tokens": kw.get("output_tokens", 0),
            "latency_ms": kw.get("latency_ms", 0),
            "cache_hit": kw.get("cache_hit", 0),
            "attempts": kw.get("attempts", 0),
            "ok": kw.get("ok", 1),
            "error": kw.get("error"),
        }
        try:
            self.call_repo.add(**row)
        except Exception:  # logging must never break extraction
            pass


def get_client(settings: LLMSettings | None = None, *, call_repo: Any = "auto") -> LLMClient:
    """Build a client from the environment. call_repo='auto' logs to the SQLite DB when it exists."""
    s = settings or LLMSettings.from_env()
    if call_repo == "auto":
        call_repo = None
        from rfq_agent.config import DB_PATH

        if Path(DB_PATH).exists():
            from rfq_agent.data.repositories import LLMCallRepo

            call_repo = LLMCallRepo()
    cache = LLMCache(Path(s.cache_dir) if s.cache_dir else LLM_CACHE_DIR)
    return LLMClient(make_provider(s), mode=s.mode, cache=cache, call_repo=call_repo, max_tokens=s.max_tokens)
