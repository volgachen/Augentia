"""Offline checks for POST /compress with until (summarize a past span).

A CAgent session on the mock database with turns on "yesterday" and
"this morning" (row times set directly), then a range compression with
since/until.

Covers:
  - only turns that ended before `until` are summarized; later turns stay live
  - the range_summary row is placed right after the turns it covers, so the
    later turns follow it (in the transcript and in the model's context)
  - last_message_at isn't moved back by the summary
  - until without since -> 422; until <= since -> 422

Run from backend/:  python scripts/test_compress_until.py
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
from app.routers.sessions import CompressRequest, compress_session

from _common import fail, ok, section

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        ok(label)
    else:
        fail(label)
        FAILURES.append(label)


def mark(turn_id: str) -> str:
    return json.dumps({"name": "turn_end", "metadata": {
        "turn_id": turn_id, "stop_reason": "completed", "turn_compression_applied": False,
        "num_tokens": None, "token_count_is_estimate": True, "token_count_error": None,
        "usage": []}, "content": ""})


async def main() -> int:
    db = MockMemoryDatabase()
    summarized: list[list[dict]] = []
    seen: list[list[dict]] = []

    async def model(*, messages, tools, tool_choice=None):
        last = messages[-1].get("content") if messages else ""
        if isinstance(last, str) and last.startswith("SUM"):
            summarized.append(messages)
            return {"content": "YESTERDAY SUMMARY"}
        seen.append(messages)
        return {"content": "ok"}

    original = cagent_adapter_module._make_model
    cagent_adapter_module._make_model = lambda client, name: model
    registry = AdapterRegistry()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            agent = await db.create_agent(AgentTemplate(
                name="until", description="", agent_type=AgentType.TOOL_USE,
                system_prompt="You are a test agent.", tool_names=[], config={},
                openai_model="test-model"))
            s = await db.create_session(agent.id, working_dir=tmp, session_id="s")
            day = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)   # 10:00 +08

            async def turn(text, at):
                u = await db.add_message("s", MessageRole.USER, text, created_at=at)
                a = await db.add_message("s", MessageRole.AGENT, json.dumps(
                    {"role": "assistant", "content": f"re {text}"}), created_at=at + timedelta(seconds=1))
                await db.add_message("s", MessageRole.MARK, mark(f"t-{text}"), created_at=at + timedelta(seconds=2))
                return u, a

            yesterday = [await turn(f"y{i}", day + timedelta(hours=i * 3)) for i in range(5)]  # 10:00 .. 22:00
            morning_at = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)                  # 07:00 +08 next day
            morning = [await turn(f"m{i}", morning_at + timedelta(hours=i)) for i in range(2)]
            await build_runner(await db.get_session("s"), agent, db, registry)
            await db.update_session_status("s", SessionStatus.WAITING_USER)
            last_before = (await db.get_session("s")).last_message_at

            section("validation")
            for label, body in (
                ("until without since", CompressRequest(until=morning_at)),
                ("until <= since", CompressRequest(since=morning_at, until=day)),
            ):
                try:
                    await compress_session("s", body, db=db, registry=registry)
                except HTTPException as e:
                    check(e.status_code == 422, f"{label} -> 422")
                else:
                    check(False, f"{label} accepted")

            section("compress yesterday only")
            until = datetime(2026, 11, 16, 18, 30, tzinfo=timezone.utc)   # 02:30 +08
            r = await compress_session("s", CompressRequest(
                since=day - timedelta(hours=1), until=until, instruction="SUM yesterday"),
                db=db, registry=registry)
            check(r.get("status") == "compressed" and r.get("kind") == "range",
                  f"compressed as a range ({r.get('status')}: {r.get('reason')})")
            rows = await db.get_messages("s")
            live_users = [m.content for m in rows if m.role == MessageRole.USER]
            check(live_users == ["YESTERDAY SUMMARY", "m0", "m1"],
                  f"yesterday's turns replaced, this morning's kept, in order ({live_users})")
            summary = next(m for m in rows if m.content == "YESTERDAY SUMMARY")
            last_y = yesterday[-1][0].created_at + timedelta(seconds=2)
            check(last_y < summary.created_at < morning_at,
                  f"summary placed right after yesterday's last row ({summary.created_at.isoformat()})")
            sent = json.dumps(summarized[-1], ensure_ascii=False)
            check("y0" in sent and "y4" in sent and '"m0"' not in sent,
                  "the summarizer saw yesterday, not this morning")
            check((await db.get_session("s")).last_message_at == last_before,
                  "last_message_at unchanged (the summary was placed in the past)")

            section("the next turn")
            runner = registry.get("s")
            await runner.submit("next")
            for _ in range(200):
                await asyncio.sleep(0.02)
                if not runner.is_busy:
                    break
            order = [m.get("content") for m in seen[-1] if m["role"] == "user"]
            check(order[:3] == ["YESTERDAY SUMMARY", "m0", "m1"],
                  f"the model sees the summary, then this morning ({order})")
    finally:
        cagent_adapter_module._make_model = original
        for sid in list(getattr(registry, "_runners", {})):
            await registry.remove(sid)
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        return 1
    print("\nALL COMPRESS UNTIL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
