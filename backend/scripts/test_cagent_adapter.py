"""Offline integration test for the CAgent-backed tool-use engine.

Drives CAgentAdapter both directly and through the SessionRunner against the
in-memory mock database, with a scripted model — no network, no live LLM.
Covers: the gated-tool confirm flow (approve + deny), cancel-during-confirm
history pairing, status transitions, multi-turn continuity, and repair of
legacy/dangling persisted history.

Run from backend/:  python scripts/test_cagent_adapter.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import _common  # noqa: F401 — sets sys.path + logging

from app.adapters.base import StreamEventType
from app.adapters.cagent_adapter import CAgentAdapter
from app.adapters.cagent_store import AugentiaStore
from app.adapters.registry import AdapterRegistry
from app.core.session_runtime import build_runner
from app.db.mock import MockMemoryDatabase
from app.models.domain import AgentTemplate, AgentType, MessageRole, SessionStatus

from _common import ok, fail, section

from cagent import Context

TOOL_NAMES = ["write", "read"]


def summary_instruction(messages) -> str:
    """CAgent sends the compression instruction as the final user message."""
    last = messages[-1] if messages else {}
    return last.get("content", "") if last.get("role") == "user" and isinstance(
        last.get("content"), str) else ""


def scripted(*outputs):
    """Model callable returning canned outputs in order."""
    iterator = iter(outputs)

    async def model(*, messages, tools, tool_choice=None):
        return next(iterator)

    return model


def call_model(step1_calls, final_text):
    """Model that calls tools once, then answers from the tool results."""
    async def model(*, messages, tools, tool_choice=None):
        if any(m["role"] == "tool" for m in messages):
            return {"content": final_text, "usage": {"prompt_tokens": 120, "completion_tokens": 30}}
        return {"tool_calls": step1_calls, "content": "let me use a tool"}
    return model


async def collect_until_done(events_iter, on_confirm=None, limit=100):
    collected = []
    async for event in events_iter:
        collected.append(event)
        if on_confirm is not None and event.type == StreamEventType.TOOL_CONFIRM:
            await on_confirm(event)
        if event.type == StreamEventType.DONE:
            break
        if len(collected) >= limit:
            break
    return collected


async def test_direct_adapter_approve(db, wd) -> bool:
    section("CAgent adapter: gated approve flow (direct adapter)")
    session = await db.create_session("agent-direct", working_dir=str(wd), session_id="s-direct")
    model = call_model(
        [{"id": "call-1", "name": "write",
          "arguments": {"file_path": "out.txt", "content": "hello cagent"}}],
        "file written",
    )
    adapter = CAgentAdapter(
        db=db, tool_names=TOOL_NAMES, session_id=session.id,
        working_dir=str(wd), config={}, model_callable=model,
    )
    await adapter.start("You are a test agent.")
    await adapter.send("write out.txt please")
    events = []
    async for event in adapter.stream():
        events.append(event)
        if event.type == StreamEventType.TOOL_CONFIRM:
            payload = json.loads(event.data)
            if payload.get("call_id") != "call-1":
                fail(f"unexpected confirm call_id: {payload}")
                return False
            await adapter.resolve_decision("call-1", True)
    types = [e.type for e in events]

    def has(t):
        return t in types

    if not has(StreamEventType.TOOL_CONFIRM):
        fail(f"no TOOL_CONFIRM in {types}")
        return False
    if not has(StreamEventType.ASSISTANT_MESSAGE) or not has(StreamEventType.TOOL_RESULT):
        fail(f"missing assistant/tool_result events: {types}")
        return False
    if not has(StreamEventType.USAGE) or not has(StreamEventType.DONE):
        fail(f"missing usage/done events: {types}")
        return False
    # Confirm must be preceded by the assistant message (UI ordering parity).
    if types.index(StreamEventType.ASSISTANT_MESSAGE) > types.index(StreamEventType.TOOL_CONFIRM):
        fail(f"assistant message announced after confirm: {types}")
        return False
    result_event = next(e for e in events if e.type == StreamEventType.TOOL_RESULT)
    if "[Created]" not in result_event.data and "[Updated]" not in result_event.data:
        fail(f"tool result does not look like a write: {result_event.data[:200]}")
        return False
    if (wd / "out.txt").read_text(encoding="utf-8") != "hello cagent":
        fail("file content mismatch")
        return False
    usage_event = next(e for e in events if e.type == StreamEventType.USAGE)
    if json.loads(usage_event.data).get("context_tokens") != 150:
        fail(f"usage not reported: {usage_event.data}")
        return False

    rows = await db.get_messages(session.id)
    roles = [r.role for r in rows]
    if roles != [MessageRole.USER, MessageRole.AGENT, MessageRole.TOOL,
                 MessageRole.AGENT, MessageRole.MARK]:
        fail(f"unexpected persisted roles: {roles}")
        return False
    # The assistant row must hold OpenAI-native JSON (legacy-compatible format).
    assistant_row = json.loads(rows[1].content)
    if assistant_row.get("role") != "assistant" or not assistant_row.get("tool_calls"):
        fail(f"assistant row is not native JSON: {rows[1].content[:200]}")
        return False
    tool_row = json.loads(rows[2].content)
    if tool_row.get("tool_call_id") != "call-1" or tool_row.get("role") != "tool":
        fail(f"tool row malformed: {rows[2].content[:200]}")
        return False
    mark_row = json.loads(rows[4].content)
    if mark_row.get("name") != "turn_end" or mark_row["metadata"].get("stop_reason") != "completed":
        fail(f"turn_end mark malformed: {rows[4].content[:200]}")
        return False

    # The framework must be able to reload this history cleanly.
    store = AugentiaStore(db, session.id)
    records = await store.list_messages(session.id)
    Context(session.id, messages=records, system_prompt="x").model_messages()
    await adapter.stop()
    ok("approve flow: events, file, persisted rows, reloadable history")
    return True


async def test_direct_adapter_deny(db, wd) -> bool:
    section("CAgent adapter: deny flow (direct adapter)")
    session = await db.create_session("agent-direct", working_dir=str(wd), session_id="s-deny")
    model = call_model(
        [{"id": "call-2", "name": "write",
          "arguments": {"file_path": "denied.txt", "content": "nope"}}],
        "okay, skipped the write",
    )
    adapter = CAgentAdapter(
        db=db, tool_names=TOOL_NAMES, session_id=session.id,
        working_dir=str(wd), config={}, model_callable=model,
    )
    await adapter.start("You are a test agent.")
    await adapter.send("write denied.txt")
    events = []
    async for event in adapter.stream():
        events.append(event)
        if event.type == StreamEventType.TOOL_CONFIRM:
            await adapter.resolve_decision("call-2", False)
    result_event = next(e for e in events if e.type == StreamEventType.TOOL_RESULT)
    if "未获批准" not in result_event.data:
        fail(f"denial result missing: {result_event.data[:200]}")
        return False
    if (wd / "denied.txt").exists():
        fail("denied file was written anyway")
        return False
    if not any(e.type == StreamEventType.DONE for e in events):
        fail("no DONE after denial")
        return False
    rows = await db.get_messages(session.id)
    if not any(r.role == MessageRole.TOOL for r in rows):
        fail("denial was not persisted as a paired tool row")
        return False
    await adapter.stop()
    ok("deny flow: paired denial result, no file, turn completed")
    return True


async def _make_runner_session(db, registry, wd, session_id, model, seed=None):
    """Create a cagent-engine agent + session (+ optional seed rows), build a runner.

    The scripted model is injected by patching the adapter module's model
    factory for the duration of build_adapter (the runner path has no direct
    model_callable parameter).
    """
    import app.adapters.cagent_adapter as cagent_adapter_module
    original_make_model = cagent_adapter_module._make_model
    cagent_adapter_module._make_model = lambda client, name: model
    try:
        agent = await db.create_agent(AgentTemplate(
            name=f"cagent-test-{session_id}",
            description="cagent engine test agent",
            agent_type=AgentType.TOOL_USE,
            system_prompt="You are a test agent.",
            tool_names=TOOL_NAMES,
            config={"engine": "cagent"},
            openai_model="test-model",
        ))
        session = await db.create_session(agent.id, working_dir=str(wd), session_id=session_id)
        if seed is not None:
            await seed(session)
        runner = await build_runner(session, agent, db, registry)
    finally:
        cagent_adapter_module._make_model = original_make_model
    return session, runner


async def test_runner_flow(db, wd) -> bool:
    section("CAgent engine through SessionRunner: approve + status transitions")
    registry = AdapterRegistry()
    model = call_model(
        [{"id": "rc-1", "name": "write",
          "arguments": {"file_path": "runner.txt", "content": "via runner"}}],
        "written via runner",
    )
    session, runner = await _make_runner_session(db, registry, wd, "s-runner", model)
    try:
        # Snapshot the enum, not the Session object: the mock DB returns live
        # references, so a stored object would mutate as the turn progresses.
        statuses = []

        async def on_confirm(event):
            # Resolve inline: the turn is blocked on this decision.
            statuses.append((await db.get_session(session.id)).status)
            await runner.resolve_decision("rc-1", True)

        async def consume():
            async with runner.subscribe() as events:
                return await collect_until_done(events, on_confirm=on_confirm)

        await runner.submit("write runner.txt")
        collected = await asyncio.wait_for(consume(), 15)
        types = [e.type for e in collected]
        if StreamEventType.USER not in types or StreamEventType.TOOL_CONFIRM not in types:
            fail(f"runner dropped user/confirm events: {types}")
            return False
        if not statuses or statuses[0] != SessionStatus.WAITING_CONFIRM:
            fail(f"session not WAITING_CONFIRM during gate: {statuses}")
            return False
        final = await db.get_session(session.id)
        if final.status != SessionStatus.WAITING_USER:
            fail(f"session not WAITING_USER after turn: {final.status}")
            return False
        rows = await db.get_messages(session.id)
        user_rows = [r for r in rows if r.role == MessageRole.USER]
        if len(user_rows) != 1:
            fail(f"expected exactly one persisted user row, got {len(user_rows)}")
            return False
        if not (wd / "runner.txt").exists():
            fail("runner-flow file missing")
            return False
        ok("runner flow: confirm status flip, single user row, file written")
        return True
    finally:
        await registry.remove(session.id)


async def test_cancel_during_confirm(db, wd) -> bool:
    section("CAgent engine: cancel while waiting for human confirm")
    registry = AdapterRegistry()
    model = call_model(
        [{"id": "cc-1", "name": "write",
          "arguments": {"file_path": "cancel.txt", "content": "should not exist"}}],
        "unreachable",
    )
    session, runner = await _make_runner_session(db, registry, wd, "s-cancel", model)
    try:
        await runner.submit("write cancel.txt")
        state = {"cancelled": False}

        async def consume():
            async with runner.subscribe() as events:
                async for event in events:
                    if event.type == StreamEventType.TOOL_CONFIRM and not state["cancelled"]:
                        state["cancelled"] = True
                        await runner.cancel_turn()
                    if event.type == StreamEventType.DONE:
                        return event

        done_event = await asyncio.wait_for(consume(), 15)
        if done_event.data != "cancelled":
            fail(f"expected DONE(cancelled), got {done_event.data!r}")
            return False
        final = await db.get_session(session.id)
        if final.status != SessionStatus.WAITING_USER:
            fail(f"session not WAITING_USER after cancel: {final.status}")
            return False
        if (wd / "cancel.txt").exists():
            fail("cancelled tool still executed")
            return False
        # The interrupted turn must leave paired tool history (cooperative stop).
        store = AugentiaStore(db, session.id)
        records = await store.list_messages(session.id)
        try:
            Context(session.id, messages=records, system_prompt="x").model_messages()
        except ValueError as exc:
            fail(f"history needs recovery after cancel: {exc}")
            return False
        tool_records = [r for r in records if r["role"] == "tool"]
        if not tool_records or "未执行" not in tool_records[-1]["content"]:
            fail(f"missing paired not-executed result: {[r['content'][:60] for r in tool_records]}")
            return False

        # A follow-up turn on the same session must work (history is valid).
        runner._adapter._model = scripted({"content": "still alive"})
        await runner.submit("are you alive")
        async with runner.subscribe() as events:
            collected = await collect_until_done(events)
        if not any(e.type == StreamEventType.DONE for e in collected):
            fail("follow-up turn did not finish")
            return False
        texts = [e.data for e in collected if e.type == StreamEventType.ASSISTANT_MESSAGE]
        if not any("still alive" in t for t in texts):
            fail(f"follow-up answer missing: {texts}")
            return False
        ok("cancel flow: paired history, session reusable on next turn")
        return True
    finally:
        await registry.remove(session.id)


async def test_legacy_history_repair(db, wd) -> bool:
    section("CAgent engine: legacy + dangling persisted history is repaired on read")

    async def seed(session):
        # Legacy split rows (pre-native format) …
        await db.add_message(session.id, MessageRole.USER, "seeded user")
        await db.add_message(session.id, MessageRole.TOOL_CALL,
                             json.dumps({"name": "bash", "args": {"command": "ls"}}))
        await db.add_message(session.id, MessageRole.TOOL,
                             json.dumps({"name": "bash", "result": "a.txt b.txt"}))
        # … plus a dangling native assistant tool_call with no result row.
        await db.add_message(session.id, MessageRole.AGENT, json.dumps({
            "role": "assistant", "content": "working on it",
            "tool_calls": [{"id": "legacy-dangling", "type": "function",
                            "function": {"name": "write",
                                         "arguments": json.dumps({"file_path": "x", "content": "y"})}}],
        }))

    registry = AdapterRegistry()
    seen_messages = []

    async def model(*, messages, tools, tool_choice=None):
        seen_messages.append(messages)
        return {"content": "history loaded fine"}

    session, runner = await _make_runner_session(
        db, registry, wd, "s-legacy", model, seed=seed)
    try:
        await runner.submit("continue")
        async with runner.subscribe() as events:
            await collect_until_done(events)
        if not seen_messages:
            fail("model was never called")
            return False
        tool_inputs = [m for m in seen_messages[0] if m["role"] == "tool"]
        results = [m["content"] for m in tool_inputs]
        if "a.txt b.txt" not in results:
            fail(f"legacy tool result not surfaced: {results}")
            return False
        if not any("[result not recorded]" in r for r in results):
            fail(f"dangling call not synthesized: {results}")
            return False
        ok("legacy repair: split rows re-paired, dangling call synthesized")
        return True
    finally:
        await registry.remove(session.id)


async def test_turn_compression(db, wd) -> bool:
    section("CAgent engine: turn_compression_tool_call_threshold summarizes a tool-heavy turn")
    import app.adapters.cagent_adapter as cagent_adapter_module

    seen = []

    async def model(*, messages, tools, tool_choice=None):
        seen.append(messages)
        if "Summarize part of a conversation" in summary_instruction(messages):
            return {"content": "User asked to read notes.txt; it says hello."}
        if any(m["role"] == "tool" for m in messages):
            return {"content": "notes.txt says hello"}
        if any(m["content"].startswith("Summary of an earlier turn") for m in messages
               if m["role"] == "user" and isinstance(m["content"], str)):
            return {"content": "follow-up answered from summary"}
        return {"tool_calls": [{"id": "read-1", "name": "read",
                                "arguments": {"path": "notes.txt"}}]}

    original_make_model = cagent_adapter_module._make_model
    cagent_adapter_module._make_model = lambda client, name: model
    registry = AdapterRegistry()
    try:
        agent = await db.create_agent(AgentTemplate(
            name="cagent-test-turn-compression", description="turn compression test agent",
            agent_type=AgentType.TOOL_USE, system_prompt="You are a test agent.",
            tool_names=TOOL_NAMES,
            config={"engine": "cagent", "turn_compression_tool_call_threshold": 1},
            openai_model="test-model",
        ))
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "notes.txt").write_text("hello " * 400, encoding="utf-8")
        session = await db.create_session(agent.id, working_dir=str(wd), session_id="s-turn-comp")
        runner = await build_runner(session, agent, db, registry)
    finally:
        cagent_adapter_module._make_model = original_make_model
    async def approve(event):
        # The workspace permission policy may gate read; approve inline.
        data = json.loads(event.data) if isinstance(event.data, str) else event.data
        await runner.resolve_decision(data["call_id"], True)

    async def run(text):
        async def consume():
            async with runner.subscribe() as events:
                return await collect_until_done(events, on_confirm=approve)
        await runner.submit(text)
        return await asyncio.wait_for(consume(), 15)

    try:
        await run("read notes.txt")
        store = AugentiaStore(db, session.id)
        records = await store.list_messages(session.id)
        summaries = [r for r in records if r["role"] == "user" and r["name"] == "turn_summary"]
        if len(summaries) != 1 or not summaries[0]["metadata"].get("replaced_message_ids"):
            fail(f"turn summary row missing or unnamed: {[(r['role'], r['name']) for r in records]}")
            return False
        projected = Context(session.id, messages=records).model_messages()
        if [m["role"] for m in projected] != ["user"] or "hello" not in projected[0]["content"]:
            fail(f"reloaded history not compacted: {projected}")
            return False
        mark = json.loads((await db.get_messages(session.id))[-1].content)
        if not mark["metadata"].get("turn_compression_applied"):
            fail(f"turn_end does not record compression: {mark}")
            return False

        seen.clear()
        await run("and then?")
        sent = seen[0]
        if any(m["role"] == "tool" for m in sent) or not any(
                isinstance(m["content"], str) and m["content"].startswith("Summary of an earlier turn")
                for m in sent):
            fail(f"next turn did not see the summary instead of tool history: {sent}")
            return False
        ok("turn compression: summary persisted, reload + next turn see only the summary")
        return True
    finally:
        await registry.remove(session.id)


async def test_manual_compression_instruction(db, wd) -> bool:
    section("CAgent engine: config.compression_instruction drives manual compression")
    import app.adapters.cagent_adapter as cagent_adapter_module

    custom = "CUSTOM COMPRESSION INSTRUCTION: summarize in one line."
    systems = []

    async def model(*, messages, tools, tool_choice=None):
        instruction = summary_instruction(messages)
        systems.append(instruction)
        if instruction == custom:
            return {"content": "short summary"}
        return {"content": "a fairly long answer " * 60}

    original_make_model = cagent_adapter_module._make_model
    cagent_adapter_module._make_model = lambda client, name: model
    registry = AdapterRegistry()
    try:
        agent = await db.create_agent(AgentTemplate(
            name="cagent-test-manual-compression", description="manual compression test agent",
            agent_type=AgentType.TOOL_USE, system_prompt="You are a test agent.",
            tool_names=TOOL_NAMES,
            config={"engine": "cagent", "compression_instruction": custom},
            openai_model="test-model",
        ))
        wd.mkdir(parents=True, exist_ok=True)
        session = await db.create_session(agent.id, working_dir=str(wd), session_id="s-manual-comp")
        runner = await build_runner(session, agent, db, registry)
    finally:
        cagent_adapter_module._make_model = original_make_model

    try:
        async def consume():
            async with runner.subscribe() as events:
                return await collect_until_done(events)
        await runner.submit("tell me something long")
        await asyncio.wait_for(consume(), 15)

        result = await asyncio.wait_for(runner.compress(), 15)
        if result.get("status") != "compressed":
            fail(f"manual compression did not apply: {result}")
            return False
        if systems[-1] != custom:
            fail(f"summarizer did not receive the configured instruction: {systems[-1][:200]!r}")
            return False
        names = [json.loads(m.content).get("name") for m in await db.get_messages(session.id)
                 if m.role == MessageRole.MARK]
        if "compression_completed" not in names:
            fail(f"no compression_completed mark persisted: {names}")
            return False
        status = (await db.get_session(session.id)).status
        if status != SessionStatus.WAITING_USER:
            fail(f"status after manual compression={status}")
            return False
        ok("manual compression: configured instruction used, mark saved, status restored")
        return True
    finally:
        await registry.remove(session.id)


async def test_compression_sends_tools_with_tool_choice_none(db, wd) -> bool:
    section("CAgent engine: summarizer gets the agent's tools + tool_choice='none'")
    import app.adapters.cagent_adapter as cagent_adapter_module

    seen = {}

    def make_model(client, name):
        async def model(*, messages, tools, tool_choice=None):
            if summary_instruction(messages).startswith("Summarize the conversation"):
                seen["summary"] = ([t["name"] for t in tools], tool_choice)
                return {"content": "short summary"}
            seen.setdefault("step", ([t["name"] for t in tools], tool_choice))
            return {"content": "a fairly long answer " * 60}
        return model

    async def run_case(label, config):
        seen.clear()
        original_make_model = cagent_adapter_module._make_model
        cagent_adapter_module._make_model = make_model
        registry = AdapterRegistry()
        try:
            agent = await db.create_agent(AgentTemplate(
                name=f"cagent-test-send-tools-{label}", description="x",
                agent_type=AgentType.TOOL_USE, system_prompt="You are a test agent.",
                tool_names=TOOL_NAMES, config={"engine": "cagent", **config},
                openai_model="test-model",
            ))
            (wd / label).mkdir(parents=True, exist_ok=True)
            session = await db.create_session(agent.id, working_dir=str(wd / label),
                                              session_id=f"s-send-tools-{label}")
            runner = await build_runner(session, agent, db, registry)
        finally:
            cagent_adapter_module._make_model = original_make_model
        try:
            async def consume():
                async with runner.subscribe() as events:
                    return await collect_until_done(events)
            await runner.submit("tell me something long")
            await asyncio.wait_for(consume(), 15)
            result = await asyncio.wait_for(runner.compress(), 15)
            if result.get("status") != "compressed":
                fail(f"[{label}] manual compression did not apply: {result}")
                return None
            return seen.get("summary"), seen.get("step")
        finally:
            await registry.remove(session.id)

    got = await run_case("default", {})
    if got is None:
        return False
    summary, step = got
    if summary != (step[0], "none") or not summary[0]:
        fail(f"default: summarizer got tools={summary}, step tools={step}")
        return False
    got = await run_case("off", {"compression_send_tools": False})
    if got is None:
        return False
    if got[0] != ([], None):
        fail(f"compression_send_tools=false: summarizer got {got[0]}")
        return False
    ok("summarizer: same tools as a normal step + tool_choice='none'; config can turn it off")
    return True


async def test_compression_model(db, wd) -> bool:
    section("CAgent engine: config.compression_model selects the summarizer model")
    import app.adapters.cagent_adapter as cagent_adapter_module

    calls = []  # (model_name, is_summary)

    def make_model(client, name):
        async def model(*, messages, tools, tool_choice=None):
            is_summary = summary_instruction(messages).startswith("Summarize the conversation")
            calls.append((name, is_summary))
            if is_summary:
                return {"content": "short summary"}
            return {"content": "a fairly long answer " * 60}
        return model

    async def run_case(label, config, expect_summarizer):
        original_make_model = cagent_adapter_module._make_model
        cagent_adapter_module._make_model = make_model
        registry = AdapterRegistry()
        try:
            agent = await db.create_agent(AgentTemplate(
                name=f"cagent-test-compression-model-{label}", description="x",
                agent_type=AgentType.TOOL_USE, system_prompt="You are a test agent.",
                tool_names=TOOL_NAMES, config={"engine": "cagent", **config},
                openai_model="main-model",
            ))
            (wd / label).mkdir(parents=True, exist_ok=True)
            session = await db.create_session(
                agent.id, working_dir=str(wd / label), session_id=f"s-comp-model-{label}")
            runner = await build_runner(session, agent, db, registry)
        finally:
            cagent_adapter_module._make_model = original_make_model
        try:
            async def turn(text):
                async def consume():
                    async with runner.subscribe() as events:
                        return await collect_until_done(events)
                await runner.submit(text)
                await asyncio.wait_for(consume(), 15)

            calls.clear()
            await turn("first")
            if config.get("compression_turn_threshold"):
                # Threshold-triggered: compression ran at the end of the turn.
                pass
            else:
                result = await asyncio.wait_for(runner.compress(), 15)
                if result.get("status") != "compressed" or result.get("model") != expect_summarizer:
                    fail(f"[{label}] manual compression result: {result}")
                    return False
            summarizers = {name for name, is_summary in calls if is_summary}
            workers = {name for name, is_summary in calls if not is_summary}
            if summarizers != {expect_summarizer} or workers != {"main-model"}:
                fail(f"[{label}] summarizer={summarizers} turn model={workers}")
                return False
            return True
        finally:
            await registry.remove(session.id)

    if not await run_case("default", {}, "main-model"):
        return False
    if not await run_case("manual", {"compression_model": "small-model"}, "small-model"):
        return False
    if not await run_case("auto", {"compression_model": "small-model",
                                   "compression_turn_threshold": 1}, "small-model"):
        return False
    ok("compression_model: unset -> agent model; set -> used by manual + threshold compression")
    return True


async def test_model_error_path(db, wd) -> bool:
    section("CAgent adapter: model failure emits ERROR then DONE (no hang)")
    session = await db.create_session("agent-direct", working_dir=str(wd), session_id="s-error")

    async def broken_model(*, messages, tools, tool_choice=None):
        raise RuntimeError("model gateway down")

    adapter = CAgentAdapter(
        db=db, tool_names=TOOL_NAMES, session_id=session.id,
        working_dir=str(wd), config={}, model_callable=broken_model,
    )
    await adapter.start("You are a test agent.")
    await adapter.send("hello?")
    types = []
    async for event in adapter.stream():
        types.append(event.type)
    if StreamEventType.ERROR not in types or StreamEventType.DONE not in types:
        fail(f"expected ERROR+DONE, got {types}")
        return False
    if types.index(StreamEventType.ERROR) > types.index(StreamEventType.DONE):
        fail(f"ERROR came after DONE: {types}")
        return False
    await adapter.stop()
    ok("error path: ERROR followed by DONE, stream terminates")
    return True


async def main() -> int:
    db = MockMemoryDatabase()
    # The mock database validates that a session's agent exists.
    await db.create_agent(AgentTemplate(
        id="agent-direct",
        name="cagent-direct",
        description="direct-drive test agent",
        agent_type=AgentType.TOOL_USE,
        system_prompt="You are a test agent.",
        tool_names=TOOL_NAMES,
        config={},
        openai_model="test-model",
    ))
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        results = [
            await test_direct_adapter_approve(db, wd / "approve"),
            await test_direct_adapter_deny(db, wd / "deny"),
            await test_runner_flow(db, wd / "runner"),
            await test_cancel_during_confirm(db, wd / "cancel"),
            await test_legacy_history_repair(db, wd / "legacy"),
            await test_turn_compression(db, wd / "turn-compression"),
            await test_manual_compression_instruction(db, wd / "manual-compression"),
            await test_compression_model(db, wd / "compression-model"),
            await test_compression_sends_tools_with_tool_choice_none(db, wd / "send-tools"),
            await test_model_error_path(db, wd / "error"),
        ]
        if all(results):
            ok("ALL CAGENT ADAPTER TESTS PASSED")
            return 0
        fail("SOME CAGENT ADAPTER TESTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
