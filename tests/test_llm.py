"""LLM layer tests with a fake provider (no network)."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from rfq_agent.config import LLMSettings
from rfq_agent.llm import providers
from rfq_agent.llm.cache import LLMCache, cache_key
from rfq_agent.llm.client import CacheMissError, ExtractionError, LLMClient, get_client, parse_json


class Part(BaseModel):
    part_number: str
    quantity: int


class FakeProvider:
    name = "fake"

    def __init__(self, responses: list[str], available: bool = True):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self._available = available

    def model_for(self, has_images: bool) -> str:
        return "fake-vl" if has_images else "fake-text"

    def available(self, model=None) -> bool:
        return self._available

    def complete(self, *, model, system, messages, schema, max_tokens):
        self.calls.append({"model": model, "messages": [dict(m) for m in messages], "schema": schema})
        return self.responses.pop(0), {"input_tokens": 10, "output_tokens": 5}


class FakeRepo:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, **kw):
        self.rows.append(kw)


def make_client(tmp_path, responses, mode="live", available=True, repo=None):
    prov = FakeProvider(responses, available)
    client = LLMClient(
        prov,
        mode=mode,
        cache=LLMCache(tmp_path / "cache"),
        call_repo=repo,
        failure_dir=tmp_path / "failures",
    )
    return client, prov


def ask(client, user_text="SH-1 x 5", images=()):
    return client.structured(
        task="t", prompt_version="v1", system="sys", user_text=user_text, images=list(images), schema=Part
    )


def test_retry_with_feedback_succeeds_on_second_attempt(tmp_path):
    repo = FakeRepo()
    bad = '{"part_number": "SH-1", "quantity": "five"}'
    client, prov = make_client(tmp_path, [bad, '{"part_number": "SH-1", "quantity": 5}'], repo=repo)
    out = ask(client)
    assert out == Part(part_number="SH-1", quantity=5)
    assert len(prov.calls) == 2
    second = prov.calls[1]["messages"]
    assert second[1] == {"role": "assistant", "text": bad}
    assert "quantity" in second[2]["text"] and "did not validate" in second[2]["text"]
    assert repo.rows[-1]["attempts"] == 2 and repo.rows[-1]["ok"] == 1
    assert repo.rows[-1]["input_tokens"] == 20
    assert client.stats["llm_calls"] == 2


def test_final_failure_raises_and_writes_evidence(tmp_path):
    repo = FakeRepo()
    client, prov = make_client(tmp_path, ["not json", "{}", '{"part_number": 1}'], repo=repo)
    with pytest.raises(ExtractionError) as ei:
        ask(client)
    assert len(prov.calls) == 3
    path = ei.value.failure_path
    assert path.exists() and path.parent == tmp_path / "failures"
    data = json.loads(path.read_text())
    assert [a["raw_output"] for a in data["attempts"]] == ["not json", "{}", '{"part_number": 1}']
    assert ei.value.raw_output == '{"part_number": 1}'
    assert repo.rows[-1]["ok"] == 0 and repo.rows[-1]["error"]


def test_auto_mode_records_then_hits_cache(tmp_path):
    client, prov = make_client(tmp_path, ['{"part_number": "A", "quantity": 1}'], mode="auto")
    assert ask(client).part_number == "A"
    files = list((tmp_path / "cache" / "t").glob("*.json"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text())
    assert entry["parsed"] == {"part_number": "A", "quantity": 1}
    assert entry["request_meta"]["model"] == "fake-text" and entry["recorded_at"]

    # same input again: served from cache, provider not called
    assert ask(client).quantity == 1
    assert len(prov.calls) == 1 and client.stats["cache_hits"] == 1

    # replay mode over the same cache dir, provider unavailable: still works
    replay, prov2 = make_client(tmp_path, [], mode="replay", available=False)
    assert ask(replay).part_number == "A" and prov2.calls == []


def test_replay_miss_is_a_clear_error(tmp_path):
    client, _ = make_client(tmp_path, [], mode="replay")
    with pytest.raises(CacheMissError, match="No recorded LLM result"):
        ask(client)


def test_auto_miss_with_unreachable_provider_errors(tmp_path):
    client, prov = make_client(tmp_path, [], mode="auto", available=False)
    with pytest.raises(CacheMissError, match="not reachable"):
        ask(client)
    assert prov.calls == []


def test_live_mode_does_not_write_cache(tmp_path):
    client, _ = make_client(tmp_path, ['{"part_number": "A", "quantity": 1}'], mode="live")
    ask(client)
    assert not (tmp_path / "cache").exists()


def test_cache_key_depends_on_image_content(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"one")
    b.write_bytes(b"two")
    kw = dict(provider="p", model="m", task="t", prompt_version="v1", system="s", user_text="u", schema={})
    assert cache_key(images=[a], **kw) != cache_key(images=[b], **kw)
    assert cache_key(images=[a], **kw) == cache_key(images=[a], **kw)
    assert cache_key(images=[a], **{**kw, "prompt_version": "v2"}) != cache_key(images=[a], **kw)


def test_images_route_to_vision_model(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"png")
    client, prov = make_client(tmp_path, ['{"part_number": "A", "quantity": 1}'])
    ask(client, images=[img])
    assert prov.calls[0]["model"] == "fake-vl"
    assert prov.calls[0]["messages"][0]["images"] == [img]


def test_text_call(tmp_path):
    client, _ = make_client(tmp_path, ["  Dear customer ...  "], mode="auto")
    assert client.text(task="cover", prompt_version="v1", system="s", user_text="u") == "Dear customer ..."
    assert client.text(task="cover", prompt_version="v1", system="s", user_text="u") == "Dear customer ..."
    assert client.stats["cache_hits"] == 1


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"a": 2} hope it helps') == {"a": 2}


def test_get_client_defaults_to_ollama(monkeypatch, tmp_path):
    for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_VISION_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_MODE", "replay")
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path))
    c = get_client(call_repo=None)
    assert c.provider.name == "ollama" and c.mode == "replay"
    assert c.provider.model_for(False) == "gemma4:12b" == c.provider.model_for(True)
    assert c.cache.root == tmp_path

    monkeypatch.setenv("LLM_VISION_MODEL", "other-vl")
    c = get_client(call_repo=None)
    assert c.provider.model_for(True) == "other-vl"


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "nope")
    with pytest.raises(providers.ProviderError):
        get_client(call_repo=None)


def test_anthropic_requires_model_from_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    with pytest.raises(providers.ProviderError, match="LLM_MODEL"):
        providers.AnthropicProvider(LLMSettings(provider="anthropic", model=""))


def _patch_httpx(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(
        providers.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )


def test_ollama_request_shape_and_think_fallback(monkeypatch, tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG")
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        if "think" in body:
            return httpx.Response(400, json={"error": "model does not support thinking"})
        return httpx.Response(
            200,
            json={
                "message": {"content": '<think>hmm</think>{"a": 1}'},
                "prompt_eval_count": 7,
                "eval_count": 3,
            },
        )

    _patch_httpx(monkeypatch, handler)
    p = providers.OllamaProvider(LLMSettings(provider="ollama", model="", ollama_num_ctx=4096))
    raw, usage = p.complete(
        model="gemma4:12b",
        system="sys",
        messages=[{"role": "user", "text": "hi", "images": [img]}],
        schema={"type": "object"},
        max_tokens=100,
    )
    assert raw == '{"a": 1}' and usage == {"input_tokens": 7, "output_tokens": 3}
    first, second = seen
    assert first["think"] is False and "think" not in second
    assert first["format"] == {"type": "object"} and first["stream"] is False
    assert first["options"]["temperature"] == 0 and first["options"]["num_ctx"] == 4096
    assert first["messages"][0] == {"role": "system", "content": "sys"}
    assert first["messages"][1]["images"] == ["iVBORw=="]


def test_ollama_available_checks_model(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"models": [{"name": "qwen3:14b"}]})

    monkeypatch.setattr(
        providers.httpx,
        "get",
        lambda url, **kw: httpx.Client(transport=httpx.MockTransport(handler)).get(url),
    )
    p = providers.OllamaProvider(LLMSettings(provider="ollama", model="qwen3:14b"))
    assert p.available() is True
    assert p.available("gemma4:12b") is False
