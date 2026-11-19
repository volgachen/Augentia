"""Offline checks for multi-select: batch delete and compressing a message span.

A CAgent session on the mock database with five completed turns (one with a
tool call), a failed turn, then:

  POST /sessions/{id}/messages/delete
    - deletes several entries in one request (model output takes its tool
      results along), rebuilds the runner once
    - an unknown id fails the whole request, nothing is deleted
    - empty list -> 422; refused while the session is running

  POST /compress {from_message_id, through_message_id}
    - exactly the selected whole turns become one range summary, placed where
      they were (later turns follow it); the summarizer sees only them
    - a span that does not start at a turn's first message or end at a turn's
      last message is refused (skipped with a reason), never widened
    - a span ending at a failed / cancelled turn is refused; one with such a
      turn inside (ending at a completed turn) is compressed, like CAgent's
      since-range
    - an earlier range summary can be part of a new span
    - only one of the ids, or mixing with since/hours/until -> 422

Run from backend/:  python scripts/test_multi_select.py
"""
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import _common  # noqa: F401 — sets sys.path + logging

from fastapi import HTTPException

import app.adapters.cagent_adapter as cagent_adapter_module
from app.adapters.registry import AdapterRegistry
from app.core.session_runtime import build_runner
from app.db.mock import MockMemoryDatabase
from app.models.domain import AgentTemplate, AgentType, MessageRole, SessionStatus
from app.routers.sessions import (
    CompressRequest, DeleteMessagesRequest, compress_session, delete_messages,
)

from _common import fail, ok, section

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        ok(label)
    else:
        fail(label)
        FAILURES.append(label)


def mark(turn_id: str, stop_reason: str = "completed") -> str:
    return json.dumps({"name": "turn_end", "metadata": {
        "turn_id": turn_id, "stop_reason": stop_reason, "turn_compression_applied": False,
        "num_tokens": None, "token_count_is_estimate": True, "token_count_error": None,
        "usage": []}, "content": ""})


async def expect_status(coro, status: int, label: str) -> None:
    try:
        await coro
    except HTTPException as e:
        check(e.status_code == status, f"{label} -> {status} ({e.status_code}: {e.detail})")
    else:
        check(False, f"{label} accepted")


async def main() -> int:
    db = MockMemoryDatabase()
    summarized: list[list[dict]] = []

    async def model(*, messages, tools, tool_choice=None):
        last = messages[-1].get("content") if messages else ""
        if isinstance(last, str) and last.startswith("SUM"):
            summarized.append(messages)
            return {"content": f"SUMMARY {len(summarized)}"}
        return {"content": "ok"}

    original = cagent_adapter_module._make_model
    cagent_adapter_module._make_model = lambda client, name: model
    registry = AdapterRegistry()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            agent = await db.create_agent(AgentTemplate(
                name="multi", description="", agent_type=AgentType.TOOL_USE,
                system_prompt="You are a test agent.", tool_names=[], config={},
                openai_model="test-model"))
            await db.create_session(agent.id, working_dir=tmp, session_id="s")
            t0 = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
            turns: dict[str, dict] = {}

            async def turn(text, at, *, tool=False, stop="completed"):
                rows = {"user": await db.add_message("s", MessageRole.USER, text, created_at=at)}
                if tool:
                    rows["call"] = await db.add_message("s", MessageRole.AGENT, json.dumps({
                        "role": "assistant", "content": "",
                        "tool_calls": [{"id": f"c-{text}", "type": "function",
                                        "function": {"name": "read", "arguments": "{}"}}]}),
                        created_at=at + timedelta(seconds=1))
                    rows["result"] = await db.add_message("s", MessageRole.TOOL, json.dumps(
                        {"role": "tool", "tool_call_id": f"c-{text}", "content": f"out {text}"}),
                        created_at=at + timedelta(seconds=2))
                rows["reply"] = await db.add_message("s", MessageRole.AGENT, json.dumps(
                    {"role": "assistant", "content": f"re {text}"}), created_at=at + timedelta(seconds=3))
                await db.add_message("s", MessageRole.MARK, mark(f"t-{text}", stop),
                                     created_at=at + timedelta(seconds=4))
                turns[text] = rows

            for i in range(5):
                await turn(f"q{i}", t0 + timedelta(minutes=i), tool=(i == 1))
            await turn("bad", t0 + timedelta(minutes=5), stop="failed")
            await turn("q6", t0 + timedelta(minutes=6))
            await turn("q7", t0 + timedelta(minutes=7))
            await build_runner(await db.get_session("s"), agent, db, registry)
            await db.update_session_status("s", SessionStatus.WAITING_USER)
            users = lambda rows: [m.content for m in rows if m.role == MessageRole.USER]

            async def compress(first, last, label=""):
                return await compress_session("s", CompressRequest(
                    from_message_id=first.id, through_message_id=last.id,
                    instruction=f"SUM {label}"), db=db, registry=registry)

            section("compress: validation")
            q = turns
            await expect_status(compress_session("s", CompressRequest(
                from_message_id=q["q0"]["user"].id), db=db, registry=registry),
                422, "only from_message_id")
            await expect_status(compress_session("s", CompressRequest(
                from_message_id=q["q0"]["user"].id, through_message_id=q["q0"]["reply"].id,
                hours=1), db=db, registry=registry), 422, "span with hours")

            section("compress: refused, never widened")
            before = await db.get_messages("s")
            for label, first, last in (
                ("starts mid-turn", q["q1"]["call"], q["q2"]["reply"]),
                ("ends mid-turn", q["q1"]["user"], q["q1"]["call"]),
                ("ends at a turn's first row", q["q0"]["user"], q["q1"]["user"]),
                ("ends at a failed turn", q["q4"]["user"], q["bad"]["reply"]),
                ("unknown id", q["q0"]["user"], type("X", (), {"id": "nope"})()),
            ):
                r = await compress(first, last)
                check(r.get("status") == "skipped" and r.get("reason"),
                      f"{label}: skipped ({r.get('status')}: {r.get('reason')})")
            check([m.id for m in await db.get_messages("s")] == [m.id for m in before],
                  "refused spans changed nothing")
            check(not summarized, "the summarizer was never called for refused spans")

            section("compress: q1..q2 (one with a tool call)")
            r = await compress(q["q1"]["user"], q["q2"]["reply"], "a")
            check(r.get("status") == "compressed" and r.get("kind") == "range",
                  f"compressed as a range ({r.get('status')}: {r.get('reason')})")
            rows = await db.get_messages("s")
            check(users(rows) == ["q0", "SUMMARY 1", "q3", "q4", "bad", "q6", "q7"],
                  f"summary sits where q1..q2 were ({users(rows)})")
            sent = json.dumps(summarized[-1], ensure_ascii=False)
            check("out q1" in sent and "re q2" in sent and "re q3" not in sent,
                  "the summarizer saw q1..q2 (and earlier context), not later turns")
            summary1 = next(m for m in rows if m.content == "SUMMARY 1")

            section("compress: span starting at an earlier range summary")
            r = await compress(summary1, q["q3"]["reply"], "b")
            check(r.get("status") == "compressed", f"summary + q3 compressed ({r.get('reason')})")
            rows = await db.get_messages("s")
            check(users(rows) == ["q0", "SUMMARY 2", "q4", "bad", "q6", "q7"],
                  f"nested: one summary replaces summary 1 and q3 ({users(rows)})")

            section("compress: a failed turn inside the span")
            r = await compress(q["bad"]["user"], q["q6"]["reply"], "inside")
            check(r.get("status") == "compressed",
                  f"failed turn inside, completed last turn: compressed ({r.get('reason')})")
            rows = await db.get_messages("s")
            check(users(rows) == ["q0", "SUMMARY 2", "q4", "SUMMARY 3", "q7"],
                  f"bad..q6 replaced by one summary ({users(rows)})")

            section("compress: a single turn, the last one")
            r = await compress(q["q7"]["user"], q["q7"]["reply"], "c")
            check(r.get("status") == "compressed", f"last turn compressed ({r.get('reason')})")
            check(users(await db.get_messages("s"))[-1] == "SUMMARY 4", "summary is last")

            section("compress: the newest turn was cancelled")
            # CAgent's own ranges end at the newest turn and give up when it
            # didn't complete; a span ends where it ends, so it still works.
            await turn("q9", t0 + timedelta(minutes=20))
            await turn("stopped", t0 + timedelta(minutes=21), stop="cancelled")
            r = await compress(q["q9"]["user"], q["q9"]["reply"], "before-cancelled")
            check(r.get("status") == "compressed",
                  f"span before a cancelled newest turn: compressed ({r.get('reason')})")
            order = users(await db.get_messages("s"))
            # (SUMMARY 4 replaced the then-newest turn, so it was stamped with
            # the real current time and sorts after these back-dated turns.)
            check("q9" not in order and order.index("SUMMARY 5") + 1 == order.index("stopped"),
                  f"q9 replaced; its summary sits right before the cancelled turn ({order})")
            check((await db.get_session("s")).status == SessionStatus.WAITING_USER,
                  "status back to idle after compressing")
            r = await compress(q["stopped"]["user"], q["stopped"]["reply"], "cancelled")
            check(r.get("status") == "skipped" and "did not complete" in (r.get("reason") or ""),
                  f"the cancelled turn itself is refused ({r.get('reason')})")

            section("batch delete")
            await expect_status(delete_messages("s", DeleteMessagesRequest(message_ids=[]),
                                                db=db, registry=registry), 422, "empty list")
            count = len(await db.get_messages("s"))
            await expect_status(delete_messages("s", DeleteMessagesRequest(
                message_ids=[q["q0"]["user"].id, "nope"]), db=db, registry=registry),
                404, "one unknown id")
            check(len(await db.get_messages("s")) == count, "nothing deleted when one id fails")

            runner_before = registry.get("s")
            r = await delete_messages("s", DeleteMessagesRequest(
                message_ids=[q["q0"]["user"].id, q["q4"]["user"].id, q["q4"]["reply"].id]),
                db=db, registry=registry)
            check(set(r["deleted_ids"]) == {q["q0"]["user"].id, q["q4"]["user"].id, q["q4"]["reply"].id},
                  "deleted exactly the selected rows")
            rows = await db.get_messages("s")
            check(sorted(users(rows)) == ["SUMMARY 2", "SUMMARY 3", "SUMMARY 4", "SUMMARY 5", "stopped"],
                  f"rows gone ({users(rows)})")
            check(registry.get("s") is not runner_before, "runner rebuilt")

            # A model output with tool calls takes its results along.
            await turn("q8", t0 + timedelta(minutes=30), tool=True)
            r = await delete_messages("s", DeleteMessagesRequest(message_ids=[q["q8"]["call"].id]),
                                      db=db, registry=registry)
            check(q["q8"]["result"].id in r["deleted_ids"], "tool result deleted with its call")

            await db.update_session_status("s", SessionStatus.RUNNING)
            await expect_status(delete_messages("s", DeleteMessagesRequest(
                message_ids=[q["q8"]["user"].id]), db=db, registry=registry), 409, "while running")
            await db.update_session_status("s", SessionStatus.WAITING_USER)
    finally:
        cagent_adapter_module._make_model = original
        for sid in list(getattr(registry, "_runners", {})):
            await registry.remove(sid)
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        return 1
    print("\nALL MULTI-SELECT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
