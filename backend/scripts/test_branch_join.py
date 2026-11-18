"""Offline checks for fork-dispatch: run messages in parallel branches, join back.

CAgent sessions on the mock database with a scripted model, driven through
the router functions.

Covers:
  - fork-dispatch makes one branch per message (borrowing the history), each
    runs its message; the branches run concurrently
  - each finished branch turn is appended to the source as a branch_summary
    row (+ turn_end), with joined_from; the result is the branch's
    turn_summary when turn compression ran, else its last reply; a failed
    turn joins a note
  - the source's next turn sends the joined results to its model
  - a join waits for a running source turn (lands after it, never inside it)
  - expanding a branch_summary returns the branch's turn rows
  - joins are idempotent and redone for pending branches (restart)
  - while results are pending the source reports pending_joins and refuses
    new messages / another dispatch (409); deleting
    a pending branch joins what it did and releases the source

Run from backend/:  python scripts/test_branch_join.py
"""
import asyncio
import json
import sys
import tempfile

import _common  # noqa: F401 — sets sys.path + logging

import app.adapters.cagent_adapter as cagent_adapter_module
from app.adapters.registry import AdapterRegistry
from app.core.branch_join import BRANCH_SUMMARY, JOINED_FROM, is_branch_summary, write_join
from app.core.session_runtime import build_runner, join_pending_branches
from app.db.mock import MockMemoryDatabase
from app.models.domain import AgentTemplate, AgentType, MessageRole
from fastapi import HTTPException
from app.routers.sessions import (
    ForkDispatchRequest, PostMessageRequest, compressed_messages, delete_session, fork_dispatch,
    get_session, list_sessions, post_message,
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


async def main() -> int:
    db = MockMemoryDatabase()
    calls: list[str] = []
    # Concurrency probe: how many branch turns are inside the model at once.
    active = {"now": 0, "max": 0}
    gate = asyncio.Event()
    source_seen: list[list[dict]] = []

    async def model(*, messages, tools, tool_choice=None):
        text = last_user(messages)
        if text.startswith("Summarize") or text.startswith("请总结"):
            # turn-compression summarizer
            task = next((m["content"] for m in messages if isinstance(m.get("content"), str)
                         and m["content"].startswith("task ")), "?")
            return {"content": f"SUMMARY of {task}"}
        calls.append(text)
        if messages[-1]["role"] == "tool":
            return {"content": "tool done"}
        if text.startswith("task "):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            try:
                await asyncio.wait_for(gate.wait(), 5)
            except asyncio.TimeoutError:
                pass
            active["now"] -= 1
            if text == "task fail":
                raise RuntimeError("boom")
            if text == "task tool":
                return {"tool_calls": [{"id": f"c-{len(calls)}", "name": "read",
                                        "arguments": {"file_path": "x.txt"}}]}
            return {"content": f"done: {text}"}
        source_seen.append(messages)
        if text == "slow source turn":
            await asyncio.sleep(0.5)
        return {"content": f"source: {text}"}

    original = cagent_adapter_module._make_model
    cagent_adapter_module._make_model = lambda client, name: model
    registry = AdapterRegistry()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            (__import__("pathlib").Path(tmp) / "x.txt").write_text("hi", encoding="utf-8")
            agent = await db.create_agent(AgentTemplate(
                name="join-test", description="", agent_type=AgentType.TOOL_USE,
                system_prompt="You are a test agent.", tool_names=["read"],
                config={"tool_approvals": {"read": False},
                        # turn compression after 1 tool call: "task tool" gets a turn_summary
                        "turn_compression_tool_call_threshold": 1,
                        "turn_compression_instruction": "请总结这一轮"},
                openai_model="test-model",
            ))
            source = await db.create_session(agent.id, working_dir=tmp, session_id="src")
            await build_runner(source, agent, db, registry)

            async def turn(sid, text):
                runner = registry.get(sid)
                await runner.submit(text)
                await asyncio.sleep(0.05)
                await wait_for(lambda: _idle(runner))

            async def _idle(runner):
                return not runner.is_busy

            gate.set()
            await turn("src", "hello")
            gate.clear()

            section("fork-dispatch")
            r = await fork_dispatch("src", ForkDispatchRequest(
                messages=["task a", "task tool", "task fail"]), db=db, registry=registry)
            ids = [b["session_id"] for b in r["branches"]]
            check(len(ids) == 3, "three branches")
            branches = [await db.get_session(i) for i in ids]
            check(all(b.join_into_session_id == "src" and b.branched_from_session_id == "src"
                      for b in branches), "each branch borrows from and joins into the source")
            await wait_for(lambda: _gte(active, 3), timeout=3)
            check(active["max"] == 3, f"branch turns run concurrently (max {active['max']} at once)")

            section("while results are pending (dispatch mode off)")
            shown = await get_session("src", db=db)
            check(shown.status.value == "WAITING_USER" and shown.pending_joins == 3,
                  f"GET reports 3 pending joins; status stays WAITING_USER ({shown.status.value}, {shown.pending_joins})")
            listed = {s.id: s for s in await list_sessions(db=db)}
            check(listed["src"].pending_joins == 3, "the session list reports them too")
            for label, call in (
                ("POST /messages", lambda: post_message(
                    "src", PostMessageRequest(content="too early"), db=db, registry=registry)),
                ("fork-dispatch", lambda: fork_dispatch(
                    "src", ForkDispatchRequest(messages=["x"]), db=db, registry=registry)),
            ):
                try:
                    await call()
                except HTTPException as e:
                    check(e.status_code == 409 and "joined back" in e.detail, f"{label} -> 409 while results are pending")
                else:
                    check(False, f"{label} accepted while results are pending")
            gate.set()

            async def all_joined():
                rows = await db.get_own_messages("src")
                return sum(is_branch_summary(m) for m in rows) == 3
            check(await wait_for(all_joined), "all three results joined into the source")

            rows = await db.get_own_messages("src")
            joined = [m for m in rows if is_branch_summary(m)]
            by_task = {m.metadata[JOINED_FROM]["task"]: m for m in joined}
            check(set(by_task) == {"task a", "task tool", "task fail"}, f"tasks recorded ({list(by_task)})")
            check("done: task a" in by_task["task a"].content and "task a" in by_task["task a"].content,
                  "no tool calls: the last reply is the result (template includes the task)")
            check("SUMMARY" in by_task["task tool"].content
                  and by_task["task tool"].metadata[JOINED_FROM]["summary_id"],
                  "turn compression ran: its turn_summary is the result")
            check("没有完成" in by_task["task fail"].content
                  and by_task["task fail"].metadata[JOINED_FROM]["ok"] is False,
                  "a failed turn joins a note")
            ends = [m for m in rows if m.role == MessageRole.MARK
                    and JOINED_FROM in json.loads(m.content).get("metadata", {})]
            check(len(ends) == 3, "each join adds a turn_end mark")
            for b in ids:
                s = await db.get_session(b)
                check(s.joined_message_id in {m.id for m in joined}, f"branch {b[:8]} records its joined row")

            shown = await get_session("src", db=db)
            check(shown.status.value == "WAITING_USER" and shown.pending_joins == 0,
                  f"after the last join: WAITING_USER again ({shown.status.value})")

            section("the source's next turn sees the results")
            await turn("src", "next")
            sent = json.dumps(source_seen[-1], ensure_ascii=False)
            check("done: task a" in sent and "SUMMARY" in sent and "没有完成" in sent,
                  "joined results are in the source model's context")

            section("expanding a branch_summary")
            rows_a = await compressed_messages(db, "src", by_task["task a"].id)
            check([m.content for m in rows_a if m.role == MessageRole.USER] == ["task a"]
                  and all(m.session_id == ids[0] for m in rows_a),
                  "plain branch turn: its rows, read from the branch")
            rows_t = await compressed_messages(db, "src", by_task["task tool"].id)
            roles = [m.role for m in rows_t]
            check(MessageRole.TOOL in roles and MessageRole.USER in roles,
                  f"compressed branch turn: the originals the turn_summary replaced ({[r.value for r in roles]})")

            section("share page: locate a branch's message")
            from app.core.session_share import write_share
            from app.routers.share import locate_shared_message
            write_share("src", True)
            tool_row = next(m for m in rows_t if m.role == MessageRole.TOOL)
            loc = await locate_shared_message("src", tool_row.id, db=db)
            check(loc["expand"] == [by_task["task tool"].id] and loc["role"] == MessageRole.TOOL,
                  f"a row of a joined branch turn: expand that result ({loc})")
            plain_user = next(m for m in rows_a if m.role == MessageRole.USER)
            loc = await locate_shared_message("src", plain_user.id, db=db)
            check(loc["expand"] == [by_task["task a"].id], "also for a branch turn without turn compression")
            own = next(m for m in await db.get_own_messages("src") if m.content == "hello")
            loc = await locate_shared_message("src", own.id, db=db)
            check(loc["expand"] == [], "the session's own rows locate as before")
            # A row of the branch that its joined result doesn't show: 404.
            later = await db.add_message(ids[0], MessageRole.USER, "after the join")
            for mid, label in ((later.id, "a branch row after the joined turn"),
                               ("no-such-id", "an unknown id")):
                try:
                    await locate_shared_message("src", mid, db=db)
                except HTTPException as e:
                    check(e.status_code == 404, f"{label} -> 404")
                else:
                    check(False, f"{label} was located")
            write_share("src", False)
            try:
                await locate_shared_message("src", tool_row.id, db=db)
            except HTTPException as e:
                check(e.status_code == 404, "not shared -> the share locate is 404")
            from app.routers.sessions import locate_session_message
            loc = await locate_session_message("src", tool_row.id, db=db)
            check(loc["expand"] == [by_task["task tool"].id],
                  "the console's locate (owner, no share needed) finds it the same way")

            section("a join waits for a running source turn")
            gate.set()
            runner = registry.get("src")
            await runner.submit("slow source turn")
            await asyncio.sleep(0.1)
            try:
                await fork_dispatch("src", ForkDispatchRequest(messages=["x"]), db=db, registry=registry)
            except HTTPException as e:
                check(e.status_code == 409, "fork-dispatch over a running turn -> 409")
            else:
                check(False, "fork-dispatch over a running turn accepted")
            # A branch made just before the source started a turn (here: its
            # join is queued while that turn runs) still lands after it.
            from app.routers.sessions import _create_derived_session
            from datetime import datetime, timezone
            late_branch = await _create_derived_session(
                await db.get_session("src"), db, registry, title="late",
                cutoff=datetime.now(timezone.utc), join_into=True)
            late = late_branch.id
            await registry.get(late).submit("task late")

            async def late_joined():
                return (await db.get_session(late)).joined_message_id is not None
            check(await wait_for(late_joined), "late branch joined")
            rows = await db.get_own_messages("src")
            order = [("slow" if m.role == MessageRole.USER and m.content == "slow source turn"
                      else "join" if is_branch_summary(m) and m.metadata[JOINED_FROM]["session_id"] == late
                      else "end" if m.role == MessageRole.MARK and json.loads(m.content).get("name") == "turn_end"
                      else None) for m in rows]
            order = [o for o in order if o]
            slow_i = order.index("slow")
            check(order[slow_i + 1] == "end" and order.index("join") > slow_i + 1,
                  f"the join landed after the running turn's turn_end ({order[slow_i:]})")

            section("deleting a pending branch releases the source")
            gate.clear()
            r = await fork_dispatch("src", ForkDispatchRequest(messages=["task stuck"]), db=db, registry=registry)
            stuck = r["branches"][0]["session_id"]
            await asyncio.sleep(0.1)
            check((await get_session("src", db=db)).pending_joins == 1, "one join pending on a stuck branch")
            await delete_session(stuck, db=db, registry=registry)
            shown = await get_session("src", db=db)
            check(shown.status.value == "WAITING_USER" and shown.pending_joins == 0,
                  f"deleting the branch releases the source ({shown.status.value})")
            rows = await db.get_own_messages("src")
            stuck_row = next((m for m in rows if is_branch_summary(m)
                              and m.metadata[JOINED_FROM]["session_id"] == stuck), None)
            check(stuck_row is not None and stuck_row.metadata[JOINED_FROM]["ok"] is False,
                  "the cancelled turn joined a note")
            gate.set()

            section("idempotent; pending joins redone at startup")
            b = await db.get_session(ids[0])
            before = sum(is_branch_summary(m) for m in await db.get_own_messages("src"))
            again = await write_join(db, b)
            after = sum(is_branch_summary(m) for m in await db.get_own_messages("src"))
            check(again.id == b.joined_message_id and after == before, "writing the same join twice adds nothing")
            # Simulate a restart between turn end and join.
            r = await fork_dispatch("src", ForkDispatchRequest(messages=["task restart"]), db=db, registry=registry)
            rid = r["branches"][0]["session_id"]
            registry.get(rid).on_turn_finished = None   # the join never ran
            await wait_for(lambda: _turn_done(db, rid))
            check((await db.get_session(rid)).joined_message_id is None, "join pending after the 'crash'")
            await join_pending_branches(db, registry)
            check(await wait_for(lambda: _joined(db, rid)), "join_pending_branches appended it")
    finally:
        cagent_adapter_module._make_model = original
        for sid in list(getattr(registry, "_runners", {})):
            await registry.remove(sid)
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        return 1
    print("\nALL BRANCH JOIN CHECKS PASSED")
    return 0


async def _gte(active, n):
    return active["max"] >= n


async def _turn_done(db, sid):
    rows = await db.get_own_messages(sid)
    return any(m.role == MessageRole.MARK and json.loads(m.content).get("name") == "turn_end" for m in rows)


async def _joined(db, sid):
    return (await db.get_session(sid)).joined_message_id is not None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
