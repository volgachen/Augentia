"""Offline checks for POST /messages with reply=false (inject context).

CAgent sessions on the mock database with a scripted model, through the
router functions.

Covers:
  - the message is added as a user row, no turn runs, nothing replies
  - the next turn's model sees it right before that turn's input
  - turn compression of the next turn keeps it (it isn't part of that turn)
  - added while a turn runs, it lands after that turn, not inside it
  - from_session_id is kept and shown to the model as for a sent message
  - in dispatch mode it goes into the session itself (no branch); branches
    made later borrow it

Run from backend/:  python scripts/test_inject_context.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import _common  # noqa: F401 — sets sys.path + logging

import app.adapters.cagent_adapter as cagent_adapter_module
from app.adapters.registry import AdapterRegistry
from app.core.session_runtime import build_runner
from app.db.mock import MockMemoryDatabase
from app.models.domain import AgentTemplate, AgentType, MessageRole, SessionStatus
from app.routers.sessions import (
    DispatchModeRequest, PostMessageRequest, post_message, set_dispatch_mode,
)

from _common import fail, ok, section

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        ok(label)
    else:
        fail(label)
        FAILURES.append(label)


def text_of(m) -> str:
    content = m.get("content")
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


async def wait_idle(runner, db, sid, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.03)
        if not runner.is_busy and (await db.get_session(sid)).status == SessionStatus.WAITING_USER:
            return True
    return False


async def main() -> int:
    db = MockMemoryDatabase()
    calls: list[list[dict]] = []
    gate = asyncio.Event()
    gate.set()

    async def model(*, messages, tools, tool_choice=None):
        last = messages[-1]
        if text_of(last).startswith("SUM"):
            return {"content": "TURN SUMMARY"}
        if last["role"] == "tool":
            return {"content": "done with tool"}
        calls.append(messages)
        if text_of(last) == "slow":
            await asyncio.wait_for(gate.wait(), 5)
        if text_of(last) == "use tool":
            return {"tool_calls": [{"id": f"c{len(calls)}", "name": "read",
                                    "arguments": {"file_path": "x.txt"}}]}
        return {"content": f"re:{text_of(last)}"}

    original = cagent_adapter_module._make_model
    cagent_adapter_module._make_model = lambda client, name: model
    registry = AdapterRegistry()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "x.txt").write_text("hi", encoding="utf-8")
            agent = await db.create_agent(AgentTemplate(
                name="inject", description="", agent_type=AgentType.TOOL_USE,
                system_prompt="You are a test agent.", tool_names=["read"], openai_model="m",
                config={"tool_approvals": {"read": False},
                        "turn_compression_tool_call_threshold": 1,
                        "turn_compression_instruction": "SUM this turn",
                        "branch_join_template": "{result}"},
            ))
            s = await db.create_session(agent.id, working_dir=tmp, session_id="s")
            runner = await build_runner(s, agent, db, registry)
            await db.update_session_status("s", SessionStatus.WAITING_USER)

            async def post(content, reply=True, **kw):
                return await post_message("s", PostMessageRequest(content=content, reply=reply, **kw),
                                          db=db, registry=registry)

            section("added without a reply")
            r = await post("现在是 20:30，外面在下雨。", reply=False)
            check(r["status"] == "added" and r["message_id"], f"reply=false -> added ({r})")
            await asyncio.sleep(0.2)
            rows = await db.get_own_messages("s")
            check([m.role for m in rows] == [MessageRole.USER] and rows[0].content.startswith("现在是"),
                  "one user row, no reply, no turn_end")
            check(calls == [] and (await db.get_session("s")).status == SessionStatus.WAITING_USER,
                  "no model call; session stays WAITING_USER")

            section("the next turn sees it, turn compression keeps it")
            await post("use tool")
            check(await wait_idle(runner, db, "s"), "the next turn ran")
            users = [text_of(m) for m in calls[0] if m["role"] == "user"]
            check(users == ["现在是 20:30，外面在下雨。", "use tool"],
                  f"the model saw the context right before the input ({users})")
            rows = await db.get_own_messages("s")
            summary = next((m for m in rows if (m.metadata or {}).get("cagent_name") == "turn_summary"), None)
            context_row = next(m for m in rows if m.content.startswith("现在是"))
            check(summary is not None and context_row.deleted_at is None
                  and context_row.id not in (summary.metadata.get("replaced_message_ids") or []),
                  "after turn compression the context row is still there, as written")
            check(rows.index(context_row) < rows.index(summary), "it sits before the turn summary")

            section("added while a turn runs: lands after that turn")
            gate.clear()
            await post("slow")
            await asyncio.sleep(0.1)
            task = asyncio.create_task(post("中途发生的事", reply=False))
            await asyncio.sleep(0.1)
            check(not task.done(), "waits for the running turn")
            gate.set()
            r = await task
            await wait_idle(runner, db, "s")
            rows = await db.get_own_messages("s")
            names = []
            for m in rows:
                if m.role == MessageRole.MARK:
                    names.append(json.loads(m.content)["name"])
                elif m.role == MessageRole.USER:
                    names.append("ctx" if m.content == "中途发生的事" else "user")
                else:
                    names.append(m.role.value)
            i = names.index("ctx")
            check(names[i - 1] == "turn_end", f"right after the running turn's turn_end ({names[i - 2:i + 1]})")

            section("from_session_id")
            await post("来自另一个会话的消息", reply=False, from_session_id="other-session")
            await post("hello")
            await wait_idle(runner, db, "s")
            users = [text_of(m) for m in calls[-1] if m["role"] == "user"]
            check("[from-session:other-session] 来自另一个会话的消息" in users,
                  "the model sees the sender prefix like for a sent message")
            row = next(m for m in await db.get_own_messages("s") if "另一个会话" in m.content)
            check(row.from_session_id == "other-session", "from_session_id stored")

            section("dispatch mode")
            await set_dispatch_mode("s", DispatchModeRequest(enabled=True), db=db, registry=registry)
            before = len(await db.list_sessions())
            r = await post("派发模式下的背景", reply=False)
            check(r["status"] == "added" and len(await db.list_sessions()) == before,
                  "added to the session itself, no branch")
            r = await post("ask in a branch")
            branch_id = r["branch_session_id"]
            for _ in range(200):
                await asyncio.sleep(0.03)
                if (await db.get_session(branch_id)).joined_message_id:
                    break
            users = [text_of(m) for m in calls[-1] if m["role"] == "user"]
            check("派发模式下的背景" in users, "a branch made later borrows the context")
    finally:
        cagent_adapter_module._make_model = original
        for sid in list(getattr(registry, "_runners", {})):
            await registry.remove(sid)
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        return 1
    print("\nALL INJECT CONTEXT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
