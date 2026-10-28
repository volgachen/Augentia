"""Offline test for config.model_params / compression_model_params.

Drives both engines with a fake AsyncOpenAI client that records the
chat.completions.create kwargs, so no network is needed. Checks that:
- model_params is merged into every request of the CAgent and legacy engines;
- reserved keys (model, messages, tools, tool_choice, ...) are rejected at start;
- the summarizer uses compression_model_params when set, model_params otherwise,
  and still sends tool_choice="none" (params cannot override it).

Run from backend/:  python scripts/test_model_params.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from openai.types.chat import ChatCompletion

import _common  # noqa: F401 — sets sys.path + logging

from _common import fail, ok, section, use_isolated_state

use_isolated_state("augentia-model-params-")

import app.adapters.cagent_adapter as cagent_adapter_module  # noqa: E402
import app.adapters.openai_tool_use as legacy_module  # noqa: E402
from app.adapters.model_params import validate_model_params  # noqa: E402
from app.adapters.registry import AdapterRegistry  # noqa: E402
from app.core.session_runtime import build_runner  # noqa: E402
from app.db.mock import MockMemoryDatabase  # noqa: E402
from app.models.domain import AgentTemplate, AgentType  # noqa: E402

TOOL_NAMES = ["read"]
LONG = "a fairly long answer " * 60


def _response(content: str):
    """A real ChatCompletion, so both engines can parse/model_dump it."""
    return ChatCompletion.model_validate({
        "id": "fake", "object": "chat.completion", "created": 0, "model": "main-model",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


class FakeClient:
    """Stands in for AsyncOpenAI; records every chat.completions.create call."""

    calls: list[dict] = []

    def __init__(self, **kwargs):
        completions = SimpleNamespace(create=self._create)
        self.chat = SimpleNamespace(completions=completions)

    async def _create(self, **kwargs):
        FakeClient.calls.append(kwargs)
        is_summary = kwargs.get("tool_choice") == "none"
        return _response("short summary" if is_summary else LONG)


async def collect_until_done(events_iter, limit=200):
    seen = []
    async for event in events_iter:
        seen.append(event.type.value if hasattr(event.type, "value") else event.type)
        if seen[-1] in ("done", "error") or len(seen) >= limit:
            break
    return seen


async def run_cagent(db, wd, label, config, compress=False):
    FakeClient.calls = []
    registry = AdapterRegistry()
    agent = await db.create_agent(AgentTemplate(
        name=f"params-{label}", description="x", agent_type=AgentType.TOOL_USE,
        system_prompt="You are a test agent.", tool_names=TOOL_NAMES,
        config={"engine": "cagent", **config}, openai_model="main-model"))
    (wd / label).mkdir(parents=True, exist_ok=True)
    session = await db.create_session(agent.id, working_dir=str(wd / label),
                                      session_id=f"s-params-{label}")
    runner = await build_runner(session, agent, db, registry)
    try:
        async def consume():
            async with runner.subscribe() as events:
                return await collect_until_done(events)
        await runner.submit("tell me something long")
        await asyncio.wait_for(consume(), 15)
        result = await asyncio.wait_for(runner.compress(), 15) if compress else None
        return FakeClient.calls, result
    finally:
        await registry.remove(session.id)


async def test_validation() -> bool:
    section("model_params validation")
    if validate_model_params(None) != {}:
        fail("None should mean no params")
        return False
    for bad, needle in (([1], "object"), ({"tool_choice": "auto"}, "tool_choice"),
                        ({"model": "x"}, "model"), ({"extra_body": "x"}, "extra_body")):
        try:
            validate_model_params(bad)
        except ValueError as exc:
            if needle not in str(exc):
                fail(f"{bad!r}: unexpected message {exc}")
                return False
        else:
            fail(f"{bad!r} was accepted")
            return False
    ok("non-object, reserved keys and non-object extra_body are rejected")
    return True


async def test_cagent_params(db, wd) -> bool:
    section("CAgent engine: model_params reach every request; summarizer params")
    params = {"temperature": 0.3, "reasoning_effort": "high",
              "extra_body": {"top_k": 5}}
    calls, result = await run_cagent(db, wd, "merged", {"model_params": params}, compress=True)
    if result is None or result.get("status") != "compressed":
        fail(f"manual compression did not apply: {result}")
        return False
    step = [c for c in calls if c.get("tool_choice") != "none"]
    summary = [c for c in calls if c.get("tool_choice") == "none"]
    if not step or not summary:
        fail(f"expected step and summarizer calls, got {calls}")
        return False
    for call in calls:
        for key, value in params.items():
            if call.get(key) != value:
                fail(f"{key} missing from request: {sorted(call)}")
                return False
        if call["model"] != "main-model":
            fail(f"model overridden: {call['model']}")
            return False
    if not summary[0].get("tools"):
        fail("summarizer request lost its tools")
        return False
    ok("step + summarizer requests carry model_params; summarizer keeps tools + tool_choice='none'")

    calls, result = await run_cagent(db, wd, "split", {
        "model_params": {"temperature": 0.3},
        "compression_model_params": {"temperature": 0.0, "max_tokens": 2048},
    }, compress=True)
    step = [c for c in calls if c.get("tool_choice") != "none"]
    summary = [c for c in calls if c.get("tool_choice") == "none"]
    if result.get("status") != "compressed" or not step or not summary:
        fail(f"split case: result={result} calls={calls}")
        return False
    if step[0].get("temperature") != 0.3 or "max_tokens" in step[0]:
        fail(f"step params wrong: {step[0]}")
        return False
    if summary[0].get("temperature") != 0.0 or summary[0].get("max_tokens") != 2048:
        fail(f"summarizer params wrong: {summary[0]}")
        return False
    ok("compression_model_params replaces model_params for the summarizer only")

    calls, _ = await run_cagent(db, wd, "none", {})
    if any(k in calls[0] for k in ("temperature", "reasoning_effort", "extra_body")):
        fail(f"unexpected params without config: {sorted(calls[0])}")
        return False
    ok("no model_params -> request unchanged")

    try:
        await run_cagent(db, wd, "reserved", {"model_params": {"tool_choice": "auto"}})
    except ValueError as exc:
        if "tool_choice" not in str(exc):
            fail(f"unexpected error: {exc}")
            return False
    else:
        fail("reserved tool_choice in model_params was accepted")
        return False
    ok("reserved key in model_params fails at session start")
    return True


async def test_legacy_params(db, wd) -> bool:
    section("legacy engine: model_params reach every request")
    FakeClient.calls = []
    registry = AdapterRegistry()
    agent = await db.create_agent(AgentTemplate(
        name="params-legacy", description="x", agent_type=AgentType.TOOL_USE,
        system_prompt="You are a test agent.", tool_names=TOOL_NAMES,
        config={"engine": "legacy", "model_params": {"temperature": 0.9, "top_p": 0.5}},
        openai_model="main-model"))
    (wd / "legacy").mkdir(parents=True, exist_ok=True)
    session = await db.create_session(agent.id, working_dir=str(wd / "legacy"),
                                      session_id="s-params-legacy")
    runner = await build_runner(session, agent, db, registry)
    try:
        async def consume():
            async with runner.subscribe() as events:
                return await collect_until_done(events)
        await runner.submit("hello")
        await asyncio.wait_for(consume(), 15)
    finally:
        await registry.remove(session.id)
    if not FakeClient.calls:
        fail("legacy engine made no model call")
        return False
    call = FakeClient.calls[0]
    if call.get("temperature") != 0.9 or call.get("top_p") != 0.5 or call["model"] != "main-model":
        fail(f"legacy request params wrong: {sorted(call.items())}")
        return False
    ok("legacy request carries model_params")
    return True


def test_agent_api_validation() -> bool:
    section("REST: agents API rejects bad model_params with 400")
    from fastapi.testclient import TestClient
    from app.main import app

    base = {"name": "params-api", "agent_type": "tool_use", "system_prompt": "t",
            "tool_names": []}
    with TestClient(app) as c:
        good = c.post("/api/v1/agents", json={**base, "config": {
            "model_params": {"temperature": 0.5}}})
        if good.status_code != 201:
            fail(f"valid model_params -> {good.status_code} {good.text}")
            return False
        agent_id = good.json()["id"]
        for config in ({"model_params": {"tool_choice": "auto"}},
                       {"model_params": "hot"},
                       {"compression_model_params": {"messages": []}}):
            r = c.post("/api/v1/agents", json={**base, "config": config})
            if r.status_code != 400:
                fail(f"{config} -> {r.status_code}, expected 400")
                return False
        r = c.put(f"/api/v1/agents/{agent_id}", json={"config": {"model_params": {"model": "x"}}})
        if r.status_code != 400:
            fail(f"update with reserved key -> {r.status_code}, expected 400")
            return False
    ok("create/update with reserved or non-object params -> 400; valid params -> 201")
    return True


async def main() -> int:
    originals = (cagent_adapter_module.AsyncOpenAI, legacy_module.AsyncOpenAI)
    cagent_adapter_module.AsyncOpenAI = FakeClient
    legacy_module.AsyncOpenAI = FakeClient
    try:
        db = MockMemoryDatabase()
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            results = [
                await test_validation(),
                await test_cagent_params(db, wd / "cagent"),
                await test_legacy_params(db, wd),
            ]
        results.append(test_agent_api_validation())
    finally:
        cagent_adapter_module.AsyncOpenAI, legacy_module.AsyncOpenAI = originals
    if all(results):
        ok("ALL MODEL PARAMS TESTS PASSED")
        return 0
    fail("SOME MODEL PARAMS TESTS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
