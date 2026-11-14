"""Plugin hooks (P3): every hook point, chaining, Reject, timeouts, on_error.

Throwaway plugins under a temp AUGENTIA_HOME; sessions run on the real CAgent
engine (mock DB) with a scripted model. A small "probe" plugin records what
its hooks saw; behavior is switched per test through the session config, so
each case is set up by writing plugins.<id> into the session's config.json.

Run from backend/:  python scripts/test_plugins_hooks.py
"""
import asyncio
import json
import sys
import textwrap
from pathlib import Path

import _common  # noqa: F401 — sets sys.path + logging

from _common import fail, ok, section, use_isolated_state

ROOT = use_isolated_state("augentia-plugins-hooks-")
HOME = ROOT / "home"
PLUGINS = HOME / "plugins"

import app.adapters.tools  # noqa: E402,F401
from app.adapters.base import StreamEventType  # noqa: E402
from app.adapters.cagent_adapter import CAgentAdapter  # noqa: E402
from app.db.mock import MockMemoryDatabase  # noqa: E402
from app.models.domain import MessageRole  # noqa: E402

failures = 0


def check(cond: bool, msg: str) -> None:
    global failures
    if cond:
        ok(msg)
    else:
        failures += 1
        fail(msg)


def write_plugin(plugin_id: str, code: str, manifest: dict | None = None) -> None:
    d = PLUGINS / plugin_id
    d.mkdir(parents=True, exist_ok=True)
    base = {"id": plugin_id, "name": plugin_id, "version": "1", "api_version": 1, "entry": "plugin:P",
            "config": {"session": {"type": "object"}}}
    base.update(manifest or {})
    (d / "plugin.json").write_text(json.dumps(base), encoding="utf-8")
    (d / "plugin.py").write_text(textwrap.dedent(code), encoding="utf-8")


# One plugin covers every hook; ctx.config["mode"] picks the behavior per test.
write_plugin("probe", """
import asyncio
from dataclasses import replace
from app.plugins.sdk import Plugin, hook, Reject, ToolImage

LOG = []

class P(Plugin):
    @hook("message.received", priority=10)
    async def received(self, ctx, msg):
        LOG.append(("received", msg.content, msg.source, msg.images))
        mode = ctx.config.get("mode")
        if mode == "reject_message":
            raise Reject("no swearing")
        if mode == "rewrite_message":
            return replace(msg, content=msg.content.upper())
        if mode == "inject_image":
            return replace(msg, images=[{"ref": "att:" + "0" * 64}])
        if mode == "drop_images":
            return replace(msg, images=None)

    @hook("model.before")
    async def before(self, ctx, req):
        LOG.append(("model.before", req.step, len(req.messages), [t["name"] for t in req.tools]))
        mode = ctx.config.get("mode")
        if mode == "inject_context":
            msgs = list(req.messages)
            msgs.insert(1, {"role": "user", "content": "CONTEXT FROM PLUGIN"})
            return replace(req, messages=msgs)
        if mode == "hide_tools":
            return replace(req, tools=[])
        if mode == "add_tool":
            return replace(req, tools=req.tools + [{"name": "evil", "description": "", "parameters": {}}])

    @hook("message.generated")
    async def generated(self, ctx, reply):
        LOG.append(("generated", reply.step, reply.content, [c["name"] for c in reply.tool_calls], reply.final))
        mode = ctx.config.get("mode")
        if mode == "redact" and reply.final:
            return replace(reply, content=reply.content.replace("secret", "[redacted]"))
        if mode == "drop_calls":
            return replace(reply, tool_calls=[], content="calls dropped")
        if mode == "add_call":
            return replace(reply, tool_calls=reply.tool_calls + [{"id": "x", "name": "read", "arguments": {}}])
        if mode == "reject_reply" and reply.final:
            raise Reject("reply not allowed")

    @hook("tool.before")
    async def tool_before(self, ctx, call):
        LOG.append(("tool.before", call.name, dict(call.arguments)))
        mode = ctx.config.get("mode")
        if mode == "block_tool":
            raise Reject("not today")
        if mode == "rewrite_args":
            return replace(call, arguments={**call.arguments, "file_path": "safe.txt"})
        if mode == "bad_args":
            return replace(call, arguments={"nope": 1})
        if mode == "rename":
            return replace(call, name="write")

    @hook("tool.after")
    async def tool_after(self, ctx, result, call):
        LOG.append(("tool.after", call.name, result.text[:40], len(result.images)))
        mode = ctx.config.get("mode")
        if mode == "annotate":
            return replace(result, text=result.text + " [checked]")
        if mode == "after_reject":
            raise Reject("too late")

    @hook("turn.end")
    async def turn_end(self, ctx, summary):
        if ctx.config.get("mode") == "slow_end":
            await asyncio.sleep(0.2)
        LOG.append(("turn.end", summary.stop_reason, summary.final_text, summary.steps, summary.tool_calls))
""")

# Chain partner: runs after probe (priority 50 < 100) on message.received.
write_plugin("second", """
from dataclasses import replace
from app.plugins.sdk import Plugin, hook

class P(Plugin):
    @hook("message.received", priority=50)
    async def received(self, ctx, msg):
        if ctx.config.get("mode") == "suffix":
            return replace(msg, content=msg.content + " +second")
""")

# Misbehaving hooks with on_error skip / fail.
write_plugin("flaky", """
import asyncio
from app.plugins.sdk import Plugin, hook

class P(Plugin):
    @hook("model.before", timeout=0.2)
    async def slow(self, ctx, req):
        if ctx.config.get("mode") == "slow":
            await asyncio.sleep(5)
        if ctx.config.get("mode") == "crash":
            raise RuntimeError("flaky crashed")
        if ctx.config.get("mode") == "wrong_type":
            return "not a request"

    @hook("tool.before", on_error="fail")
    async def strict(self, ctx, call):
        if ctx.config.get("mode") == "strict_crash":
            raise RuntimeError("guard is broken")
""")

wd = ROOT / "wd"
wd.mkdir()
(wd / "notes.txt").write_text("hello secret world", encoding="utf-8")


def set_plugins(session_id: str, plugins: dict) -> None:
    d = HOME / "sessions" / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps({"plugins": plugins}), encoding="utf-8")


def tool_then_answer(calls, answer="final answer"):
    async def model(*, messages, tools, tool_choice=None):
        model.requests.append({"messages": messages, "tools": [t["name"] for t in tools]})
        if messages[-1]["role"] == "tool":
            return {"content": answer, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return {"content": "using tools", "tool_calls": [dict(c) for c in calls]}
    model.requests = []
    return model


def answer_only(text="plain answer"):
    async def model(*, messages, tools, tool_choice=None):
        model.requests.append({"messages": messages, "tools": [t["name"] for t in tools]})
        return {"content": text}
    model.requests = []
    return model


counter = 0


async def session(db, plugins: dict, model, tools=("read", "write")):
    global counter
    counter += 1
    sid = f"s{counter}"
    await db.create_session("main-agent", working_dir=str(wd), session_id=sid)
    set_plugins(sid, plugins)
    adapter = CAgentAdapter(db=db, tool_names=list(tools), session_id=sid, working_dir=str(wd),
                            config={"tool_approvals": {"write": False}}, model_callable=model)
    await adapter.start("system")
    return sid, adapter


async def turn(adapter, text="hello", images=None, approve=None):
    await adapter.send(text, images=images)
    events = []
    async for event in adapter.stream():
        events.append(event)
        if event.type == StreamEventType.TOOL_CONFIRM and approve is not None:
            await adapter.resolve_decision(json.loads(event.data)["call_id"], approve)
    return events


def of(events, t):
    return [e for e in events if e.type == t]


async def main() -> None:
    from app.plugins.host import get_plugin_host

    host = get_plugin_host()
    await host.load_all()
    import augentia_plugins.probe.plugin as probe  # noqa: E402 — loaded by the host
    LOG = probe.LOG
    db = MockMemoryDatabase()

    def log(kind):
        return [e for e in LOG if e[0] == kind]

    section("not enabled -> no hooks")
    LOG.clear()
    sid, a = await session(db, {}, answer_only())
    await turn(a)
    check(LOG == [], f"plugins off by default for the session: {LOG}")
    await a.stop()

    section("message.received")
    LOG.clear()
    model = answer_only()
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "rewrite_message"},
                                "second": {"enabled": True, "mode": "suffix"}}, model)
    events = await turn(a, "hello")
    check(log("received")[0][1:3] == ("hello", None), "hook sees content and source=None for user input")
    user_msgs = [m for m in model.requests[0]["messages"] if m["role"] == "user"]
    check(user_msgs[-1]["content"] == "HELLO +second", f"chain: probe(10) then second(50): {user_msgs[-1]}")
    rows = await db.get_messages(sid)
    check(rows[0].content == "HELLO +second", "the rewritten message is what gets saved")
    check(of(events, StreamEventType.USER)[0].data == "HELLO +second", "USER event shows the rewritten text")
    await a.stop()

    LOG.clear()
    model = answer_only()
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "reject_message"}}, model)
    events = await turn(a, "bad words")
    check(model.requests == [], "rejected message never reaches the model")
    check(await db.get_messages(sid) == [], "rejected message is not saved")
    status = [e.data for e in of(events, StreamEventType.STATUS)]
    check(of(events, StreamEventType.USER)[0].data == "bad words" and any("no swearing" in s for s in status)
          and of(events, StreamEventType.DONE), f"user sees their input, the reason, and DONE: {status}")
    check(not log("turn.end"), "no turn.end for a rejected message")
    await a.stop()

    LOG.clear()
    model = answer_only()
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "inject_image"}}, model)
    events = await turn(a, "hi")
    status = [e.data for e in of(events, StreamEventType.STATUS)]
    check(any("may only keep or remove" in s for s in status) and model.requests,
          f"injecting an image is refused (hook skipped, turn continues): {status}")
    await a.stop()

    LOG.clear()
    model = answer_only()
    sid, a = await session(db, {"probe": {"enabled": True}}, model)
    await turn(a, "[from-session:abc] relayed")
    a._pending_from_session = None
    check(log("received")[0][2] == "session:abc", "source is session:<id> for relayed messages")
    await a.stop()

    section("model.before")
    LOG.clear()
    model = answer_only()
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "inject_context"}}, model)
    await turn(a)
    await turn(a, "second turn")
    msgs = model.requests[0]["messages"]
    check(any(m.get("content") == "CONTEXT FROM PLUGIN" for m in msgs), "injected message reaches the model")
    rows = await db.get_messages(sid)
    check(not any("CONTEXT FROM PLUGIN" in r.content for r in rows), "injected message is not saved")
    check([e[1] for e in log("model.before")] == [1, 1], "step counts from 1 each turn")
    await a.stop()

    model = answer_only()
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "hide_tools"}}, model)
    await turn(a)
    check(model.requests[0]["tools"] == [], "tools can be hidden for a request")
    await a.stop()

    model = answer_only()
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "add_tool"}}, model)
    events = await turn(a)
    check(model.requests[0]["tools"] == ["read", "write"]
          and any("cannot add tools" in e.data for e in of(events, StreamEventType.STATUS)),
          "adding a tool is refused and reported")
    await a.stop()

    section("message.generated")
    LOG.clear()
    model = tool_then_answer([{"id": "r1", "name": "read", "arguments": {"file_path": "notes.txt"}}],
                             answer="the secret is out")
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "redact"}}, model)
    events = await turn(a)
    gen = log("generated")
    check([(g[1], g[4]) for g in gen] == [(1, False), (2, True)], f"runs per model call, final flag: {gen}")
    rows = await db.get_messages(sid)
    final_row = json.loads([r for r in rows if r.role == MessageRole.AGENT][-1].content)
    check(final_row["content"] == "the [redacted] is out", f"rewritten reply is what gets saved: {final_row}")
    texts = [json.loads(e.data).get("content") for e in of(events, StreamEventType.ASSISTANT_MESSAGE)]
    check("the [redacted] is out" in texts, "and what the UI receives")
    check(rows[-1].role == MessageRole.MARK, "turn still ends normally")
    await a.stop()

    model = tool_then_answer([{"id": "w1", "name": "write", "arguments": {"file_path": "x.txt", "content": "x"}}])
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "drop_calls"}}, model)
    await turn(a)
    check(not (wd / "x.txt").exists(), "dropped tool call does not run")
    rows = await db.get_messages(sid)
    first = json.loads([r for r in rows if r.role == MessageRole.AGENT][0].content)
    check(first["content"] == "calls dropped" and not first.get("tool_calls"), "reply saved without the calls")
    await a.stop()

    model = tool_then_answer([{"id": "r2", "name": "read", "arguments": {"file_path": "notes.txt"}}])
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "add_call"}}, model)
    events = await turn(a)
    rows = await db.get_messages(sid)
    first = json.loads([r for r in rows if r.role == MessageRole.AGENT][0].content)
    check(len(first["tool_calls"]) == 1 and any("not added" in e.data for e in of(events, StreamEventType.STATUS)),
          "adding a tool call is refused")
    await a.stop()

    LOG.clear()
    model = answer_only("forbidden reply")
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "reject_reply"}}, model)
    events = await turn(a)
    errors = [e.data for e in of(events, StreamEventType.ERROR)]
    check(errors and "reply not allowed" in errors[0], f"Reject at message.generated fails the turn: {errors}")
    rows = await db.get_messages(sid)
    check(not any(r.role == MessageRole.AGENT for r in rows), "rejected reply is not saved")
    await asyncio.sleep(0.05)
    check(log("turn.end") and log("turn.end")[-1][1] == "failed", "turn.end reports failed")
    await a.stop()

    section("tool.before")
    LOG.clear()
    model = tool_then_answer([{"id": "w2", "name": "write", "arguments": {"file_path": "danger.txt", "content": "y"}}])
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "block_tool"}}, model)
    await turn(a)
    check(not (wd / "danger.txt").exists(), "rejected call does not run")
    tool_msg = [m for m in model.requests[-1]["messages"] if m["role"] == "tool"][0]
    check("rejected by plugin 'probe': not today" in tool_msg["content"], f"model sees why: {tool_msg['content']}")
    await a.stop()

    LOG.clear()
    model = tool_then_answer([{"id": "w3", "name": "write", "arguments": {"file_path": "danger.txt", "content": "z"}}])
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "rewrite_args"}}, model)
    a._requires_approval["write"] = True  # make the human confirm the call
    events = await turn(a, approve=True)
    confirm = json.loads(of(events, StreamEventType.TOOL_CONFIRM)[0].data)
    check(confirm["args"]["file_path"] == "safe.txt", "confirm panel shows the rewritten arguments")
    check((wd / "safe.txt").read_text() == "z" and not (wd / "danger.txt").exists(), "call runs with them")
    rows = await db.get_messages(sid)
    tool_row = [r for r in rows if r.role == MessageRole.TOOL][0]
    check(tool_row.metadata.get("arguments_modified_by") == ["probe"]
          and tool_row.metadata.get("arguments", {}).get("file_path") == "safe.txt",
          f"tool row records who changed what: {tool_row.metadata}")
    assistant_row = json.loads([r for r in rows if r.role == MessageRole.AGENT][0].content)
    check("danger.txt" in assistant_row["tool_calls"][0]["function"]["arguments"],
          "assistant row keeps the model's original arguments")
    await a.stop()

    model = tool_then_answer([{"id": "r3", "name": "read", "arguments": {"file_path": "notes.txt"}}])
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "bad_args"}}, model)
    await turn(a)
    tool_msg = [m for m in model.requests[-1]["messages"] if m["role"] == "tool"][0]
    check("参数不匹配" in tool_msg["content"] and "probe" in tool_msg["content"],
          f"invalid rewritten arguments become a tool error: {tool_msg['content'][:120]}")
    await a.stop()

    model = tool_then_answer([{"id": "r4", "name": "read", "arguments": {"file_path": "notes.txt"}}])
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "rename"}}, model)
    events = await turn(a)
    check(any("only arguments can be changed" in e.data for e in of(events, StreamEventType.STATUS)),
          "renaming the call is refused")
    await a.stop()

    section("tool.after")
    LOG.clear()
    model = tool_then_answer([{"id": "r5", "name": "read", "arguments": {"file_path": "notes.txt"}}])
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "annotate"}}, model)
    events = await turn(a)
    tool_msg = [m for m in model.requests[-1]["messages"] if m["role"] == "tool"][0]
    check(tool_msg["content"].endswith("[checked]"), "model sees the changed result")
    check(log("tool.after")[0][1] == "read" and "hello secret world" in log("tool.after")[0][2],
          "hook sees tool name and original text")
    result_ev = json.loads(of(events, StreamEventType.TOOL_RESULT)[0].data)
    check(result_ev["result"].endswith("[checked]"), "and so does the UI")
    await a.stop()

    model = tool_then_answer([{"id": "r6", "name": "read", "arguments": {"file_path": "notes.txt"}}])
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "after_reject"}}, model)
    events = await turn(a)
    check(any("Reject is not supported" in e.data for e in of(events, StreamEventType.STATUS))
          and of(events, StreamEventType.DONE) and not of(events, StreamEventType.ERROR),
          "Reject at tool.after is treated as a skipped hook")
    await a.stop()

    section("turn.end")
    LOG.clear()
    model = tool_then_answer([{"id": "r7", "name": "read", "arguments": {"file_path": "notes.txt"}}],
                             answer="all done")
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "slow_end"}}, model)
    events = await turn(a)
    check(of(events, StreamEventType.DONE) and not log("turn.end"), "turn.end does not hold up the turn")
    await a.stop()  # drains background hooks
    ends = log("turn.end")
    check(ends == [("turn.end", "completed", "all done", 2, 1)], f"summary: {ends}")

    section("skip / fail / timeouts")
    model = answer_only()
    sid, a = await session(db, {"flaky": {"enabled": True, "mode": "slow"}}, model)
    events = await turn(a)
    status = [e.data for e in of(events, StreamEventType.STATUS)]
    check(model.requests and any("timed out after 0.2s" in s for s in status), f"timeout -> skipped: {status}")
    await a.stop()

    for mode, needle in (("crash", "RuntimeError: flaky crashed"), ("wrong_type", "returned str")):
        model = answer_only()
        sid, a = await session(db, {"flaky": {"enabled": True, "mode": mode}}, model)
        events = await turn(a)
        status = [e.data for e in of(events, StreamEventType.STATUS)]
        check(model.requests and any(needle in s for s in status), f"{mode} -> skipped with warning: {status}")
        await a.stop()

    model = tool_then_answer([{"id": "r8", "name": "read", "arguments": {"file_path": "notes.txt"}}])
    sid, a = await session(db, {"flaky": {"enabled": True, "mode": "strict_crash"}}, model)
    events = await turn(a)
    errors = [e.data for e in of(events, StreamEventType.ERROR)]
    check(errors and "guard is broken" in errors[0] and of(events, StreamEventType.DONE),
          f"on_error=fail fails the turn: {errors}")
    rows = await db.get_messages(sid)
    check(not any(r.role == MessageRole.TOOL and "hello secret" in r.content for r in rows),
          "the guarded tool did not run")
    await a.stop()

    section("compression does not run hooks")
    LOG.clear()
    model = answer_only("summary")
    sid, a = await session(db, {"probe": {"enabled": True}}, model)
    await turn(a)
    await turn(a, "again")
    before = len(log("model.before"))
    result = await a.compress()
    check(result["status"] == "compressed" and len(log("model.before")) == before
          and len(log("generated")) == before, f"summarizer call bypasses hooks: {result}")
    await a.stop()

    section("plugin disabled globally")
    LOG.clear()
    await host.set_enabled("probe", False)
    model = answer_only()
    sid, a = await session(db, {"probe": {"enabled": True, "mode": "reject_message"}}, model)
    await turn(a)
    check(model.requests and LOG == [], "globally disabled plugin's hooks never run")
    await a.stop()

    await host.shutdown()


asyncio.run(main())
print()
if failures:
    fail(f"{failures} check(s) failed")
    sys.exit(1)
ok("all plugin hook checks passed")
