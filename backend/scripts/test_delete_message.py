"""Offline test for DELETE /sessions/{id}/messages/{message_id}.

Runs a CAgent session against the in-memory mock database with a scripted
model, then deletes through the router function and checks what the next turn
sends to the model:
  - deleting user input removes only that row;
  - deleting a model output removes its text, tool calls and tool results;
  - compression soft-deletes the replaced rows (compressed_by), which the
    transcript hides and .../compressed returns;
  - diverge copies a summary as a plain user message;
  - deleting a summary removes it together with what it replaced;
  - deleting is rejected while the session is running.

Run from backend/:  python scripts/test_delete_message.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import _common  # noqa: F401 — sets sys.path + logging

from fastapi import HTTPException

import app.adapters.cagent_adapter as cagent_adapter_module
from app.adapters.registry import AdapterRegistry
from app.core.session_runtime import build_runner
from app.db.mock import MockMemoryDatabase
from app.models.domain import AgentTemplate, AgentType, MessageRole, SessionStatus
from app.core.compressed import is_summary
from app.routers.sessions import (
    _create_derived_session, compressed_messages, delete_message, transcript_messages,
)

from _common import ok, fail, section
from test_cagent_adapter import collect_until_done, summary_instruction


def wire_text(messages) -> list[str]:
    """Flatten what the model received into comparable strings."""
    out = []
    for m in messages:
        if m["role"] == "system":
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        calls = [c["id"] for c in m.get("tool_calls") or []]
        out.append(f"{m['role']}:{content or ''}{':' + ','.join(calls) if calls else ''}")
    return out


async def main() -> int:
    section("delete message: user / model output / summary")
    db = MockMemoryDatabase()
    seen: list[list[dict]] = []

    async def model(*, messages, tools, tool_choice=None):
        if summary_instruction(messages).startswith("SUM"):
            return {"content": "SUMMARY"}
        seen.append(messages)
        last = messages[-1]
        if last["role"] == "user" and "use tool" in str(last.get("content")):
            return {"content": "reading", "tool_calls": [
                {"id": f"call-{len(seen)}", "name": "read", "arguments": {"path": "a.txt"}}]}
        return {"content": f"answer {len(seen)}"}

    original = cagent_adapter_module._make_model
    cagent_adapter_module._make_model = lambda client, name: model
    registry = AdapterRegistry()
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        (wd / "a.txt").write_text("file body", encoding="utf-8")
        try:
            agent = await db.create_agent(AgentTemplate(
                name="delete-test", description="delete test agent",
                agent_type=AgentType.TOOL_USE, system_prompt="You are a test agent.",
                tool_names=["read"], config={"engine": "cagent"}, openai_model="test-model",
            ))
            session = await db.create_session(agent.id, working_dir=str(wd), session_id="s-delete")
            runner = await build_runner(session, agent, db, registry)
        finally:
            cagent_adapter_module._make_model = original

        async def turn(text):
            current = registry.get(session.id)

            async def approve(event):
                # The workspace permission policy may gate read; approve inline.
                await current.resolve_decision(json.loads(event.data)["call_id"], True)

            async def consume():
                async with current.subscribe() as events:
                    return await collect_until_done(events, on_confirm=approve)
            await current.submit(text)
            await asyncio.wait_for(consume(), 15)
            # DONE is broadcast just before the runner clears its turn task.
            for _ in range(100):
                if not current.is_busy:
                    break
                await asyncio.sleep(0.01)

        async def delete(message_id):
            cagent_adapter_module._make_model = lambda client, name: model
            try:
                return await delete_message(session.id, message_id, db=db, registry=registry)
            finally:
                cagent_adapter_module._make_model = original

        passed = True
        try:
            await turn("hello one")
            await turn("please use tool")
            await turn("hello three")
            rows = await db.get_messages(session.id)

            # 1. Rejected while running.
            await db.update_session_status(session.id, SessionStatus.RUNNING)
            try:
                await delete(rows[0].id)
                fail("delete while RUNNING was accepted")
                passed = False
            except HTTPException as e:
                if e.status_code != 409:
                    fail(f"delete while RUNNING: {e.status_code}")
                    passed = False
            await db.update_session_status(session.id, SessionStatus.WAITING_USER)

            # 2. Model output with tool call: assistant row + its tool rows.
            tool_assistant = next(m for m in rows if m.role == MessageRole.AGENT and "tool_calls" in m.content
                                  and json.loads(m.content).get("tool_calls"))
            call_id = json.loads(tool_assistant.content)["tool_calls"][0]["id"]
            tool_row = next(m for m in rows if m.role == MessageRole.TOOL and call_id in m.content)
            result = await delete(tool_assistant.id)
            if set(result["deleted_ids"]) != {tool_assistant.id, tool_row.id}:
                fail(f"model-output delete group: {result}")
                passed = False

            # 3. User input: just that row.
            user_one = next(m for m in rows if m.role == MessageRole.USER and m.content == "hello one")
            result = await delete(user_one.id)
            if result["deleted_ids"] != [user_one.id]:
                fail(f"user delete group: {result}")
                passed = False

            seen.clear()
            await turn("after deletes")
            text = wire_text(seen[0])
            if any("hello one" in t for t in text) or any(call_id in t for t in text) \
                    or any(t.startswith("tool:") for t in text):
                fail(f"deleted rows still reach the model: {text}")
                passed = False
            elif not any("please use tool" in t for t in text) or not any("hello three" in t for t in text):
                fail(f"surviving rows missing from the model input: {text}")
                passed = False
            else:
                ok("user / model-output deletes are gone from the next turn's context")

            # 4. Compression soft-deletes the originals under the summary.
            compressed = await asyncio.wait_for(registry.get(session.id).compress(instruction="SUM"), 15)
            if compressed.get("status") != "compressed":
                fail(f"compression: {compressed}")
                return 1
            rows = await db.get_messages(session.id)
            mark = next(m for m in rows if m.role == MessageRole.MARK
                        and json.loads(m.content).get("name") == "compression_completed")
            transcript = transcript_messages(rows, include_compression=True)
            originals = await compressed_messages(db, session.id, mark.id)
            texts = [m.content for m in originals]
            if [m.id for m in transcript] != [mark.id]:
                fail(f"transcript after compression should be just the summary: {transcript}")
                passed = False
            elif not any("please use tool" in t for t in texts) or not any("hello three" in t for t in texts)                     or any(m.role == MessageRole.MARK for m in originals):
                fail(f"compressed originals: {texts}")
                passed = False
            elif not all(m.deleted_at and m.metadata.get("compressed_by") == mark.id for m in originals):
                fail("originals not soft-deleted with compressed_by")
                passed = False
            else:
                ok("compression soft-deletes originals with compressed_by; transcript shows the summary")

            # Live client is told to reload after a turn that compresses.
            seen.clear()
            await turn("after compression")
            if not any(t == "user:SUMMARY" for t in wire_text(seen[0])):
                fail(f"summary not in context: {wire_text(seen[0])}")
                passed = False

            # 5. Diverge after compression: the summary is copied as plain text.
            branch = await _create_derived_session(
                await db.get_session(session.id), db, registry, title="branch")
            branch_rows = await db.get_messages(branch.id)
            if any(is_summary(m) for m in branch_rows) or not any(
                    m.role == MessageRole.USER and m.content == "SUMMARY" for m in branch_rows):
                fail(f"branch rows: {[(m.role, m.content[:30]) for m in branch_rows]}")
                passed = False
            else:
                ok("diverge copies the summary as a plain user message")
            await registry.remove(branch.id)

            # 6. Deleting the summary removes the whole compressed span.
            result = await delete(mark.id)
            started = json.loads(mark.content)["metadata"]["compression_id"]
            if set(result["deleted_ids"]) != {mark.id, started}:
                fail(f"summary delete group: {result}")
                passed = False
            seen.clear()
            await turn("after summary delete")
            text = wire_text(seen[0])
            if any("SUMMARY" in t or "hello three" in t or "please use tool" in t for t in text):
                fail(f"summary delete left compressed content in context: {text}")
                passed = False
            elif not any("after compression" in t for t in text):
                fail(f"rows after the summary missing: {text}")
                passed = False
            else:
                ok("deleting a summary removes it and its originals from the model's view")

            # 5. Internal marks (turn_end) are not deletable.
            rows = await db.get_messages(session.id)
            turn_end = next(m for m in rows if m.role == MessageRole.MARK)
            try:
                await delete(turn_end.id)
                fail("turn_end mark delete was accepted")
                passed = False
            except HTTPException as e:
                if e.status_code != 400:
                    fail(f"turn_end delete: {e.status_code}")
                    passed = False
        finally:
            await registry.remove(session.id)

    if passed:
        ok("ALL DELETE MESSAGE TESTS PASSED")
        return 0
    fail("SOME DELETE MESSAGE TESTS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
