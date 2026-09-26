"""Model providers. Each turns a provider-neutral chat into one API call and returns (raw_text, usage).

A message is ``{"role": "user" | "assistant", "text": str, "images": list[Path]}``. For structured calls
the raw text is the JSON produced by the model (tool input re-serialised for tool-use providers).
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Protocol

import httpx

from rfq_agent.config import LLMSettings

Message = dict[str, Any]
Usage = dict[str, int]

OLLAMA_DEFAULT_MODEL = "gemma4:12b"
TOOL_NAME = "emit_result"

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class ProviderError(RuntimeError):
    """Transport / API failure (not a validation failure)."""


def strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks some local models emit even when thinking is off."""
    return _THINK_RE.sub("", text or "").strip()


def _b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


def _mime(path: Path) -> str:
    return "image/jpeg" if Path(path).suffix.lower() in (".jpg", ".jpeg") else "image/png"


def require_all(schema: dict) -> dict:
    """Copy of a JSON schema with every object property required (nullable fields stay nullable).

    Ollama's grammar only forces `required` keys; small models then silently skip optional fields
    (e.g. the sender's name) or drift into repeating list items. Requiring every key makes the model
    decide each field explicitly, typically emitting null for absent ones.
    """

    def walk(node):
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items()}
            props = out.get("properties")
            if out.get("type") == "object" and isinstance(props, dict) and props:
                out["required"] = list(props)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


class Provider(Protocol):
    name: str

    def model_for(self, has_images: bool) -> str: ...

    def available(self, model: str | None = None) -> bool: ...

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        schema: dict | None,
        max_tokens: int,
    ) -> tuple[str, Usage]: ...


class OllamaProvider:
    """Native Ollama /api/chat: `format` = JSON schema, base64 `images`, thinking disabled."""

    name = "ollama"

    def __init__(self, s: LLMSettings):
        self.s = s
        self.base = s.ollama_base_url.rstrip("/")
        self.text_model = s.model or OLLAMA_DEFAULT_MODEL
        self.vision_model = s.vision_model or self.text_model

    def model_for(self, has_images: bool) -> str:
        return self.vision_model if has_images else self.text_model

    def installed_models(self) -> list[str]:
        r = httpx.get(f"{self.base}/api/tags", timeout=5, trust_env=False)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def available(self, model: str | None = None) -> bool:
        try:
            names = self.installed_models()
        except (httpx.HTTPError, OSError):
            return False
        want = model or self.text_model
        return any(n == want or n == f"{want}:latest" for n in names)

    def complete(self, *, model, system, messages, schema, max_tokens):
        msgs: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            d: dict[str, Any] = {"role": m["role"], "content": m["text"]}
            if m.get("images"):
                d["images"] = [_b64(p) for p in m["images"]]
            msgs.append(d)
        body: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "stream": False,
            "think": False,
            "options": {
                "temperature": 0,
                "seed": 0,
                "num_ctx": self.s.ollama_num_ctx,
                "num_predict": max_tokens,
            },
        }
        if schema is not None:
            body["format"] = require_all(schema)
        # trust_env=False: a system HTTP proxy must not intercept localhost calls
        with httpx.Client(timeout=self.s.timeout_s, trust_env=False) as c:
            r = c.post(f"{self.base}/api/chat", json=body)
            if r.status_code == 400 and "think" in r.text.lower():
                body.pop("think")  # model without a thinking switch
                r = c.post(f"{self.base}/api/chat", json=body)
        if r.status_code != 200:
            raise ProviderError(f"ollama {r.status_code}: {r.text[:500]}")
        data = r.json()
        text = strip_thinking(data.get("message", {}).get("content", ""))
        usage = {
            "input_tokens": int(data.get("prompt_eval_count") or 0),
            "output_tokens": int(data.get("eval_count") or 0),
        }
        return text, usage


class OpenAICompatProvider:
    """Any OpenAI-compatible endpoint: json_schema response_format, falls back to a forced tool call."""

    name = "openai_compat"

    def __init__(self, s: LLMSettings):
        from openai import OpenAI

        if not s.model:
            raise ProviderError("openai_compat needs LLM_MODEL")
        self.s = s
        self.text_model = s.model
        self.vision_model = s.vision_model or s.model
        self.client = OpenAI(
            api_key=s.openai_api_key or "none", base_url=s.openai_base_url or None, timeout=s.timeout_s
        )
        self._use_tools = False

    def model_for(self, has_images: bool) -> str:
        return self.vision_model if has_images else self.text_model

    def available(self, model: str | None = None) -> bool:
        return bool(self.s.openai_api_key or self.s.openai_base_url)

    @staticmethod
    def _messages(system: str, messages: list[Message]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m.get("images"):
                parts: list[dict] = [{"type": "text", "text": m["text"]}]
                parts += [
                    {"type": "image_url", "image_url": {"url": f"data:{_mime(p)};base64,{_b64(p)}"}}
                    for p in m["images"]
                ]
                out.append({"role": m["role"], "content": parts})
            else:
                out.append({"role": m["role"], "content": m["text"]})
        return out

    def complete(self, *, model, system, messages, schema, max_tokens):
        from openai import BadRequestError

        kw: dict[str, Any] = {
            "model": model,
            "messages": self._messages(system, messages),
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if schema is not None and not self._use_tools:
            try:
                resp = self.client.chat.completions.create(
                    **kw,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": TOOL_NAME, "schema": schema, "strict": False},
                    },
                )
                return strip_thinking(resp.choices[0].message.content or ""), self._usage(resp)
            except BadRequestError:
                self._use_tools = True  # endpoint lacks json_schema support; remember and fall back
        if schema is not None:
            resp = self.client.chat.completions.create(
                **kw,
                tools=[{"type": "function", "function": {"name": TOOL_NAME, "parameters": schema}}],
                tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            )
            calls = resp.choices[0].message.tool_calls or []
            raw = calls[0].function.arguments if calls else (resp.choices[0].message.content or "")
            return raw, self._usage(resp)
        resp = self.client.chat.completions.create(**kw)
        return strip_thinking(resp.choices[0].message.content or ""), self._usage(resp)

    @staticmethod
    def _usage(resp) -> Usage:
        u = getattr(resp, "usage", None)
        return {
            "input_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(u, "completion_tokens", 0) or 0),
        }


class AnthropicProvider:
    """Anthropic Messages API; structured output via a forced tool call. Model id comes from env only."""

    name = "anthropic"

    def __init__(self, s: LLMSettings):
        import anthropic

        model = s.model or os.getenv("ANTHROPIC_MODEL", "")
        if not model:
            raise ProviderError("anthropic provider needs LLM_MODEL (or ANTHROPIC_MODEL) to be set")
        self.s = s
        self.text_model = model
        self.vision_model = s.vision_model or model
        self.client = anthropic.Anthropic(api_key=s.anthropic_api_key or None, timeout=s.timeout_s)

    def model_for(self, has_images: bool) -> str:
        return self.vision_model if has_images else self.text_model

    def available(self, model: str | None = None) -> bool:
        return bool(self.s.anthropic_api_key)

    def complete(self, *, model, system, messages, schema, max_tokens):
        msgs = []
        for m in messages:
            content: list[dict] = [
                {"type": "image", "source": {"type": "base64", "media_type": _mime(p), "data": _b64(p)}}
                for p in m.get("images") or []
            ]
            content.append({"type": "text", "text": m["text"]})
            msgs.append({"role": m["role"], "content": content})
        kw: dict[str, Any] = {"model": model, "system": system, "messages": msgs, "max_tokens": max_tokens}
        if schema is not None:
            kw["tools"] = [
                {"name": TOOL_NAME, "description": "Return the extracted data.", "input_schema": schema}
            ]
            kw["tool_choice"] = {"type": "tool", "name": TOOL_NAME}
        else:
            kw["temperature"] = 0
        resp = self.client.messages.create(**kw)
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        for block in resp.content:
            if block.type == "tool_use":
                return json.dumps(block.input, ensure_ascii=False), usage
        return "".join(b.text for b in resp.content if b.type == "text"), usage


def make_provider(s: LLMSettings) -> Provider:
    match s.provider:
        case "ollama":
            return OllamaProvider(s)
        case "openai_compat":
            return OpenAICompatProvider(s)
        case "anthropic":
            return AnthropicProvider(s)
    raise ProviderError(f"unknown LLM_PROVIDER {s.provider!r} (ollama | openai_compat | anthropic)")
