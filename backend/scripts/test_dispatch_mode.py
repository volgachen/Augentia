"""Offline checks for dispatch mode (PUT /sessions/{id}/dispatching).

CAgent sessions on the mock database with a scripted model, through the
router functions.

Covers:
  - turning it on needs an idle session; the switch is stored
    (dispatch_mode) while the stored status is untouched; GET reports
    DISPATCHING
  - each POST /messages opens a new branch (returned), runs there, and its
    result is joined back; messages run concurrently; later messages borrow
    the results already joined
  - in dispatch mode the session runs no turns of its own: retry and
    compression -> 409; fork-dispatch adds branches
  - turning it off: back to ordinary turns once pending results are in
  - a branch deleted after its join stays expandable
  - the mode survives a rebuilt runner (status writes can't clear it)

Run from backend/:  python scripts/test_dispatch_mode.py
"""
import asyncio
import json
import sys
import tempfile

import _common  # noqa: F401 — sets sys.path + logging

from fastapi import HTTPException

import app.adapters.cagent_adapter as cagent_adapter_module
from app.adapters.registry import AdapterRegistry
from app.core.branch_join import JOINED_FROM, is_branch_summary
from app.core.session_runtime import build_runner, get_or_rehydrate
from app.db.mock import MockMemoryDatabase
from app.models.domain import AgentTemplate, AgentType, MessageRole, SessionStatus
from app.routers.sessions import (
    CompressRequest, DispatchModeRequest, ForkDispatchRequest, PostMessageRequest,
    RetryMessageRequest, compress_session, compressed_messages, delete_session, fork_dispatch,
    get_session, post_message, retry_from_message, set_dispatch_mode,
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
    return content if isinstance(content, str) else json.dumps(content)


async def wait_for(predicate, timeout=15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def expect_409(label, coro):
    try:
        await coro
    except HTTPException as e:
        check(e.status_code == 409, f"{label} -> 409 ({e.detail[:70]})")
        return
    check(False, f"{label} was accepted")


async def main() -> int:
    db = MockMemoryDatabase()
    gate = asyncio.Event()
    gate.set()
    active = {"now": 0, "max": 0}
    seen: dict[str, list[dict]] = {}

    async def model(*, messages, tools, tool_choice=None):
        text = last_user(messages)
        seen[text] = messages
        if text.startswith("msg "):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            try:
                await asyncio.wait_for(gate.wait(), 5)
            except asyncio.TimeoutError:
                pass
            active["now"] -= 1
        return {"content": f"result of {text}"}

    original = cagent_adapter_module._make_model
    cagent_adapter_module._make_model = lambda client, name: model
    registry = AdapterRegistry()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            agent = await db.create_agent(AgentTemplate(
                name="mode-test", description="", agent_type=AgentType.TOOL_USE,
                system_prompt="You are a test agent.", tool_names=[],
                config={"branch_join_template": "{result}"}, openai_model="test-model",
            ))
            src = await db.create_session(agent.id, working_dir=tmp, session_id="src")
            await build_runner(src, agent, db, registry)
            await db.update_session_status("src", SessionStatus.WAITING_USER)

            async def post(text):
                return await post_message("src", PostMessageRequest(content=text), db=db, registry=registry)

            async def joined_count():
                return sum(is_branch_summary(m) for m in await db.get_own_messages("src"))

            # A normal turn first, so there is history to borrow.
            await post("hello")
            await wait_for(lambda: _idle(registry, "src"))

            section("turning dispatch mode on")
            runner = registry.get("src")
            gate.clear()
            await runner.submit("msg own-turn")
            await asyncio.sleep(0.1)
            await expect_409("turning it on during a turn", set_dispatch_mode(
                "src", DispatchModeRequest(enabled=True), db=db, registry=registry))
            gate.set()
            await wait_for(lambda: _idle(registry, "src"))
            shown = await set_dispatch_mode("src", DispatchModeRequest(enabled=True), db=db, registry=registry)
            stored = await db.get_session("src")
            check(shown.status.value == "DISPATCHING" and stored.dispatch_mode
                  and stored.status == SessionStatus.WAITING_USER,
                  f"reported DISPATCHING, stored dispatch_mode on, stored status untouched ({stored.status.value})")

            section("each message runs in its own branch, concurrently")
            gate.clear()
            active["max"] = 0
            r1 = await post("msg one")
            r2 = await post("msg two")
            check(r1["status"] == "dispatched" and r1["branch_session_id"] != r2["branch_session_id"],
                  "POST /messages returns a new branch per message")
            b1 = await db.get_session(r1["branch_session_id"])
            check(b1.join_into_session_id == "src" and b1.branched_from_session_id == "src",
                  "the branch borrows from and joins into the session")
            await wait_for(lambda: _gte(active, 2), timeout=3)
            check(active["max"] == 2, f"the two messages run at once (max {active['max']})")
            shown = await get_session("src", db=db)
            check(shown.status.value == "DISPATCHING" and shown.pending_joins == 2,
                  f"DISPATCHING with 2 pending ({shown.pending_joins})")
            r3 = await post("msg three")
            check(r3["status"] == "dispatched", "more messages are accepted while others run")
            gate.set()
            check(await wait_for(lambda: _gte_async(joined_count, 3)), "all three results joined back")
            rows = await db.get_own_messages("src")
            joined = [m for m in rows if is_branch_summary(m)]
            check(all(m.content.startswith("result of msg ") for m in joined),
                  "each result is joined (template {result})")
            check(not [m for m in rows if m.role == MessageRole.USER and m.content.startswith("msg ")
                       and not is_branch_summary(m) and m.content != "msg own-turn"],
                  "the messages themselves ran in the branches, not in the session")

            section("later messages see results already joined")
            r4 = await post("msg four")
            await wait_for(lambda: _gte_async(joined_count, 4))
            sent = json.dumps(seen["msg four"], ensure_ascii=False)
            check("result of msg one" in sent and "result of msg three" in sent,
                  "the next branch borrows the joined results")

            section("the session runs no turns of its own")
            user = next(m for m in await db.get_own_messages("src") if m.content == "hello")
            await expect_409("retry", retry_from_message(
                "src", user.id, RetryMessageRequest(content="x"), db=db, registry=registry))
            await expect_409("compress", compress_session(
                "src", CompressRequest(), db=db, registry=registry))
            r = await fork_dispatch("src", ForkDispatchRequest(messages=["msg five", "msg six"]),
                                    db=db, registry=registry)
            check(len(r["branches"]) == 2, "fork-dispatch adds a batch in dispatch mode")
            await wait_for(lambda: _gte_async(joined_count, 6))

            section("the mode survives a rebuilt runner")
            await registry.remove("src")
            await get_or_rehydrate(await db.get_session("src"), db, registry)
            check((await get_session("src", db=db)).status.value == "DISPATCHING",
                  "still DISPATCHING after the runner is rebuilt")

            section("a deleted branch's joined result stays expandable")
            first = next(m for m in await db.get_own_messages("src") if is_branch_summary(m)
                         and m.metadata[JOINED_FROM]["session_id"] == b1.id)
            await delete_session(b1.id, db=db, registry=registry)
            rows_a = await compressed_messages(db, "src", first.id)
            check([m.content for m in rows_a if m.role == MessageRole.USER] == ["msg one"],
                  f"expanding after deleting the branch ({len(rows_a)} rows)")

            section("turning it off")
            gate.clear()
            pending = await post("msg seven")
            await asyncio.sleep(0.1)
            shown = await set_dispatch_mode("src", DispatchModeRequest(enabled=False), db=db, registry=registry)
            check(shown.status.value == "WAITING_USER" and shown.pending_joins == 1 and not shown.dispatch_mode,
                  "off: status back to WAITING_USER, one result still pending")
            await expect_409("a message while a result is pending", post("too early"))
            gate.set()
            await wait_for(lambda: _joined(db, pending["branch_session_id"]))
            r = await post("normal again")
            check(r["status"] == "queued", "once all results are in, messages run in the session again")
            await wait_for(lambda: _idle(registry, "src"))
            check(any(m.content == "normal again" and m.role == MessageRole.USER
                      for m in await db.get_own_messages("src")), "the turn ran in the session itself")
    finally:
        cagent_adapter_module._make_model = original
        for sid in list(getattr(registry, "_runners", {})):
            await registry.remove(sid)
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        return 1
    print("\nALL DISPATCH MODE CHECKS PASSED")
    return 0


async def _idle(registry, sid):
    await asyncio.sleep(0.02)
    return not registry.get(sid).is_busy


async def _gte(active, n):
    return active["max"] >= n


async def _gte_async(count_fn, n):
    return await count_fn() >= n


async def _joined(db, sid):
    return (await db.get_session(sid, include_deleted=True)).joined_message_id is not None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
