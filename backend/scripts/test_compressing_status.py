"""Offline checks for the COMPRESSING status.

A CAgent session on the mock database with a scripted summarizer that waits
on a gate, so the status can be read while it runs.

Covers:
  - turn compression (end of a turn), manual compression (POST /compress):
    the stored status is COMPRESSING while the summarizer runs, then back
  - STATUS events "compressing" / "compressing_done" reach subscribers
  - while COMPRESSING the session is busy: POST /messages queues behind it,
    changing the model / working dir -> 409
  - the framework reads COMPRESSING as RUNNING (not IDLE)
  - a stale COMPRESSING is reset on rehydrate (restart)

Run from backend/:  python scripts/test_compressing_status.py
"""
import asyncio
import sys
import tempfile

import _common  # noqa: F401 — sets sys.path + logging

from fastapi import HTTPException

import app.adapters.cagent_adapter as cagent_adapter_module
from app.adapters.cagent_store import AugentiaStore
from app.adapters.registry import AdapterRegistry
from app.core.session_runtime import build_runner, get_or_rehydrate
from app.db.mock import MockMemoryDatabase
from app.models.domain import AgentTemplate, AgentType, SessionStatus
from app.routers.sessions import (
    CompressRequest, UpdateSessionModelRequest, compress_session, update_session_model,
)

from _common import fail, ok, section

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        ok(label)
    else:
        fail(label)
        FAILURES.append(label)


def last_user(messages) -> str:
    users = [m for m in messages if m["role"] == "user"]
    content = users[-1].get("content") if users else ""
    return content if isinstance(content, str) else ""


async def wait_for(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def main() -> int:
    db = MockMemoryDatabase()
    gate = asyncio.Event()
    in_summary = asyncio.Event()

    async def model(*, messages, tools, tool_choice=None):
        text = last_user(messages)
        if text.startswith("SUM") or text.startswith("Summarize"):
            in_summary.set()
            await asyncio.wait_for(gate.wait(), 5)
            in_summary.clear()
            return {"content": "SUMMARY"}
        if messages[-1]["role"] == "tool":
            return {"content": "done"}
        if text == "use tool":
            return {"tool_calls": [{"id": "c1", "name": "read", "arguments": {"file_path": "x.txt"}}]}
        return {"content": f"re:{text}"}

    original = cagent_adapter_module._make_model
    cagent_adapter_module._make_model = lambda client, name: model
    registry = AdapterRegistry()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            (__import__("pathlib").Path(tmp) / "x.txt").write_text("hi", encoding="utf-8")
            agent = await db.create_agent(AgentTemplate(
                name="comp", description="", agent_type=AgentType.TOOL_USE,
                system_prompt="You are a test agent.", tool_names=["read"],
                config={"tool_approvals": {"read": False},
                        "turn_compression_tool_call_threshold": 1,
                        "turn_compression_instruction": "SUM this turn",
                        "compression_instruction": "SUM all"},
                openai_model="test-model",
            ))
            s = await db.create_session(agent.id, working_dir=tmp, session_id="s")
            runner = await build_runner(s, agent, db, registry)

            events: list[str] = []

            async def listen():
                async with runner.subscribe() as stream:
                    async for e in stream:
                        if e.type.value == "status":
                            events.append(e.data)
            listener = asyncio.create_task(listen())
            await asyncio.sleep(0.05)

            async def status():
                return (await db.get_session("s")).status

            section("turn compression at the end of a turn")
            await runner.submit("use tool")
            check(await wait_for(lambda: _set(in_summary)), "the summarizer was called")
            check(await status() == SessionStatus.COMPRESSING,
                  f"stored status is COMPRESSING while it runs ({(await status()).value})")
            store = AugentiaStore(db, "s")
            check(await store.get_status("s") == "RUNNING", "the framework reads COMPRESSING as RUNNING")
            try:
                await update_session_model("s", UpdateSessionModelRequest(model="x"), db=db, registry=registry)
            except HTTPException as e:
                check(e.status_code == 409, "changing the model while compressing -> 409")
            else:
                check(False, "changing the model while compressing was accepted")
            gate.set()
            check(await wait_for(lambda: _status_is(db, SessionStatus.WAITING_USER)),
                  "WAITING_USER once the turn is done")
            check("compressing" in events and "compressing_done" in events
                  and events.index("compressing") < events.index("compressing_done"),
                  f"subscribers got compressing / compressing_done ({events})")

            section("manual compression")
            gate.clear()
            events.clear()
            await runner.submit("second")
            await wait_for(lambda: _idle(runner))
            task = asyncio.create_task(compress_session("s", CompressRequest(), db=db, registry=registry))
            check(await wait_for(lambda: _set(in_summary)), "manual compression called the summarizer")
            check(await status() == SessionStatus.COMPRESSING, "COMPRESSING during POST /compress")
            gate.set()
            result = await task
            check(result.get("status") == "compressed" and await status() == SessionStatus.WAITING_USER,
                  f"compressed, then WAITING_USER ({result.get('status')}, {(await status()).value})")
            await asyncio.sleep(0.05)
            check("compressing" in events and "compressing_done" in events,
                  f"subscribers got the events outside a turn too ({events})")

            section("stale COMPRESSING after a restart")
            await registry.remove("s")
            await db.update_session_status("s", SessionStatus.COMPRESSING)
            await get_or_rehydrate(await db.get_session("s"), db, registry)
            check(await status() == SessionStatus.WAITING_USER, "reset to WAITING_USER on rehydrate")
            listener.cancel()
    finally:
        cagent_adapter_module._make_model = original
        for sid in list(getattr(registry, "_runners", {})):
            await registry.remove(sid)
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        return 1
    print("\nALL COMPRESSING STATUS CHECKS PASSED")
    return 0


async def _set(event):
    return event.is_set()


async def _idle(runner):
    await asyncio.sleep(0.02)
    return not runner.is_busy


async def _status_is(db, value):
    return (await db.get_session("s")).status == value


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
