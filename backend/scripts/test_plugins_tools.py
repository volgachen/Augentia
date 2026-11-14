"""Plugin tools (P1): registry, agent panel API, session execution.

Builds throwaway plugins under a temp AUGENTIA_HOME, loads them through the
plugin host, and runs real CAgent sessions with a scripted model that calls
the plugin tools. No network, no live model.

Run from backend/:  python scripts/test_plugins_tools.py
"""
import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import _common  # noqa: F401 — sets sys.path + logging

from _common import fail, ok, section, use_isolated_state

ROOT = use_isolated_state("augentia-plugins-tools-")
HOME = ROOT / "home"
PLUGINS = HOME / "plugins"

import app.adapters.tools  # noqa: E402,F401 — built-in tool registration
from fastapi.testclient import TestClient  # noqa: E402

from app.adapters.base import StreamEventType  # noqa: E402
from app.adapters.cagent_adapter import CAgentAdapter  # noqa: E402
from app.adapters.tools.base import ToolOutput  # noqa: E402
from app.adapters.tools.registry import get_entry, list_entries  # noqa: E402
from app.db.mock import MockMemoryDatabase  # noqa: E402
from app.plugins.host import PluginHost  # noqa: E402

failures = 0


def check(cond: bool, msg: str) -> None:
    global failures
    if cond:
        ok(msg)
    else:
        failures += 1
        fail(msg)


def write_plugin(plugin_id: str, code: str, manifest: dict | None = None) -> Path:
    d = PLUGINS / plugin_id
    d.mkdir(parents=True, exist_ok=True)
    base = {"id": plugin_id, "name": plugin_id.title(), "version": "1.0.0", "api_version": 1,
            "entry": "plugin:P"}
    base.update(manifest or {})
    (d / "plugin.json").write_text(json.dumps(base), encoding="utf-8")
    (d / "plugin.py").write_text(textwrap.dedent(code), encoding="utf-8")
    return d


write_plugin("kit", """
from app.plugins.sdk import Plugin, tool, ToolOutput, ToolImage

S = {"type": "object", "properties": {"text": {"type": "string"}, "times": {"type": "integer", "minimum": 1}},
     "required": ["text"], "additionalProperties": False}

class P(Plugin):
    @tool("echo", description="Repeat text.", parameters=S, requires_approval=False)
    async def echo(self, ctx, text, times=1):
        ctx.state["calls"] = ctx.state.get("calls", 0) + 1
        (ctx.session_data_dir / "last.txt").write_text(text)
        return " ".join([text] * times) + (
            f" | session={ctx.session_id} wd={ctx.working_dir} call={ctx.call.tool_call_id}"
            f" name={ctx.call.name} prefix={ctx.config.get('prefix')} n={ctx.state['calls']}"
        )

    @tool("boom", description="Always fails.", parameters={"type": "object", "properties": {}},
          requires_approval=False)
    async def boom(self, ctx):
        raise RuntimeError("kaboom")

    @tool("guarded", description="Needs approval.", parameters={"type": "object", "properties": {}})
    async def guarded(self, ctx):
        return "guarded ran"

    @tool("pic", description="Returns an image.", parameters={"type": "object", "properties": {}},
          requires_approval=False)
    async def pic(self, ctx):
        return ToolOutput("see image", images=[ToolImage(PNG)])

    @tool("weird", description="Returns a dict.", parameters={"type": "object", "properties": {}},
          requires_approval=False)
    async def weird(self, ctx):
        return {"a": 1}

import base64
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
""", {"config": {"session": {"type": "object", "properties": {"prefix": {"type": "string", "default": ">"}}}},
      "description": "Test kit"})

write_plugin("clash", """
from app.plugins.sdk import Plugin, tool
class P(Plugin):
    @tool("x", description="x", parameters={"type": "object", "properties": {}})
    async def x(self, ctx):
        return "x"
""")

write_plugin("bad__id", "")


def calls_model(calls):
    """Model that issues the given tool calls once, then answers with the results."""
    async def model(*, messages, tools, tool_choice=None):
        model.seen_tools = [t["name"] for t in tools]
        if messages[-1]["role"] == "tool":
            model.results = [m for m in messages if m["role"] == "tool"][-len(calls):]
            return {"content": "done"}
        # A new user turn: call the tools again, with ids unique per turn.
        model.turn += 1
        return {"tool_calls": [dict(c, id=f"{c['id']}-{model.turn}") for c in calls], "content": ""}
    model.seen_tools = []
    model.results = []
    model.turn = 0
    return model


async def run_turn(adapter, text="go", approve=None):
    await adapter.send(text)
    events = []
    async for event in adapter.stream():
        events.append(event)
        if event.type == StreamEventType.TOOL_CONFIRM and approve is not None:
            await adapter.resolve_decision(json.loads(event.data)["call_id"], approve)
    return events


async def main() -> None:
    import app.plugins.host as host_module

    host = PluginHost()
    host_module._host = host  # registry.unavailable_reason uses get_plugin_host()
    await host.load_all()

    section("registry")
    names = [e.name for e in list_entries()]
    check(all(n in names for n in ("kit__echo", "kit__boom", "kit__guarded")), "plugin tools registered")
    check(names.index("read") < names.index("kit__echo"), "built-in tools listed before plugin tools")
    entry = get_entry("kit__guarded")
    check(entry is not None and entry.source == "plugin" and entry.plugin_id == "kit"
          and entry.plugin_name == "Kit" and entry.requires_approval, "entry metadata")
    check(host.get("bad__id").state == "error" and "__" in (host.get("bad__id").error or ""),
          "plugin id containing '__' rejected")

    db = MockMemoryDatabase()
    wd = ROOT / "wd"
    wd.mkdir()

    section("tool call in a session")
    session = await db.create_session("main-agent", working_dir=str(wd), session_id="s1")
    cfg_dir = HOME / "sessions" / "s1"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"plugins": {"kit": {"prefix": "#"}}}), encoding="utf-8")
    model = calls_model([{"id": "c1", "name": "kit__echo", "arguments": {"text": "hi", "times": 2}}])
    adapter = CAgentAdapter(db=db, tool_names=["read", "kit__echo"], session_id=session.id,
                            working_dir=str(wd), config={}, model_callable=model)
    await adapter.start("test")
    events = await run_turn(adapter)
    result = next((e for e in events if e.type == StreamEventType.TOOL_RESULT), None)
    data = json.loads(result.data) if result else {}
    check(model.seen_tools == ["read", "kit__echo"], f"model offered plugin tool: {model.seen_tools}")
    check(not any(e.type == StreamEventType.TOOL_CONFIRM for e in events), "requires_approval=False skips confirm")
    text = data.get("result", "")
    check(text.startswith("hi hi | session=s1"), f"tool ran with arguments: {text[:80]}")
    check(f"wd={wd}" in text and "call=c1-1" in text and "name=kit__echo" in text,
          "ToolContext carries working dir and call info")
    check("prefix=#" in text, "ctx.config reads the session's plugins.<id> section")
    check((HOME / "plugin_data" / "kit" / "sessions" / "s1" / "last.txt").read_text() == "hi",
          "session_data_dir")
    await adapter.stop()
    # Another session: its own state. Its scripted model calls the tool on
    # every turn (history has a tool result -> answer; new user turn -> the
    # model sees the older result and answers, so drive two turns).
    await db.create_session("main-agent", working_dir=str(wd), session_id="s1-again")
    model = calls_model([{"id": "c2", "name": "kit__echo", "arguments": {"text": "again"}}])
    adapter = CAgentAdapter(db=db, tool_names=["kit__echo"], session_id="s1-again",
                            working_dir=str(wd), config={}, model_callable=model)
    await adapter.start("test")
    await run_turn(adapter)
    check(model.results and "n=1" in model.results[0]["content"], "ctx.state is separate per session")
    await run_turn(adapter)
    check(host.session_state("kit", "s1-again")["calls"] == 2 and host.session_state("kit", "s1")["calls"] == 1,
          "ctx.state persists across calls within a session")
    await adapter.stop()

    section("failures become tool results")
    session2 = await db.create_session("main-agent", working_dir=str(wd), session_id="s2")
    model = calls_model([
        {"id": "b1", "name": "kit__boom", "arguments": {}},
        {"id": "b2", "name": "kit__echo", "arguments": {"text": 5}},
        {"id": "b3", "name": "kit__echo", "arguments": {"text": "x", "extra": 1}},
        {"id": "b4", "name": "kit__weird", "arguments": {}},
    ])
    adapter = CAgentAdapter(db=db, tool_names=["kit__boom", "kit__echo", "kit__weird"], session_id=session2.id,
                            working_dir=str(wd), config={}, model_callable=model)
    await adapter.start("test")
    events = await run_turn(adapter)
    results = {json.loads(e.data)["call_id"].rsplit("-", 1)[0]: json.loads(e.data)["result"]
               for e in events if e.type == StreamEventType.TOOL_RESULT}
    check(any(e.type == StreamEventType.DONE for e in events) and not any(e.type == StreamEventType.ERROR for e in events),
          "turn completes despite failing plugin tools")
    check("RuntimeError: kaboom" in results.get("b1", ""), f"exception -> tool result: {results.get('b1')}")
    check("参数不符合 schema" in results.get("b2", "") and "text" in results.get("b2", ""),
          f"schema violation reported: {results.get('b2')}")
    check("参数不符合 schema" in results.get("b3", ""), f"additionalProperties enforced: {results.get('b3')}")
    check(results.get("b4") == "{'a': 1}", f"non-str result converted: {results.get('b4')}")
    await adapter.stop()

    section("approval")
    session3 = await db.create_session("main-agent", working_dir=str(wd), session_id="s3")
    model = calls_model([{"id": "g1", "name": "kit__guarded", "arguments": {}}])
    adapter = CAgentAdapter(db=db, tool_names=["kit__guarded"], session_id=session3.id,
                            working_dir=str(wd), config={}, model_callable=model)
    await adapter.start("test")
    events = await run_turn(adapter, approve=False)
    check(any(e.type == StreamEventType.TOOL_CONFIRM for e in events), "requires_approval=True asks")
    check("未获批准" in model.results[0]["content"] if model.results else False, "denied call not executed")
    await adapter.stop()
    session3b = await db.create_session("main-agent", working_dir=str(wd), session_id="s3b")
    model = calls_model([{"id": "g2", "name": "kit__guarded", "arguments": {}}])
    adapter = CAgentAdapter(db=db, tool_names=["kit__guarded"], session_id=session3b.id, working_dir=str(wd),
                            config={"tool_approvals": {"kit__guarded": False}}, model_callable=model)
    await adapter.start("test")
    events = await run_turn(adapter)
    check(not any(e.type == StreamEventType.TOOL_CONFIRM for e in events)
          and model.results and model.results[0]["content"] == "guarded ran",
          "agent tool_approvals overrides a plugin tool's default")
    await adapter.stop()

    section("image output")
    session4 = await db.create_session("main-agent", working_dir=str(wd), session_id="s4")
    model = calls_model([{"id": "p1", "name": "kit__pic", "arguments": {}}])
    adapter = CAgentAdapter(db=db, tool_names=["kit__pic"], session_id=session4.id, working_dir=str(wd),
                            config={"vision": True}, model_callable=model)
    await adapter.start("test")
    events = await run_turn(adapter)
    result = next(e for e in events if e.type == StreamEventType.TOOL_RESULT)
    check(bool(result.images), "ToolOutput images stored and attached to the result")
    await adapter.stop()

    section("unavailable tools")
    await host.set_enabled("kit", False)
    check(get_entry("kit__echo") is None, "disabling a plugin unregisters its tools")
    check(host.session_state("kit", "s1") == {}, "disabling drops the plugin's session state")
    session5 = await db.create_session("main-agent", working_dir=str(wd), session_id="s5")
    model = calls_model([{"id": "u1", "name": "kit__echo", "arguments": {"text": "x"}}])
    adapter = CAgentAdapter(db=db, tool_names=["read", "kit__echo", "nope__tool"], session_id=session5.id,
                            working_dir=str(wd), config={}, model_callable=model)
    await adapter.start("test")
    events = await run_turn(adapter)
    statuses = [e.data for e in events if e.type == StreamEventType.STATUS]
    check(model.seen_tools == ["read"], f"session starts without unavailable tools: {model.seen_tools}")
    check(any("kit__echo" in s and "plugin 'kit' is disabled" in s for s in statuses)
          and any("nope__tool" in s and "not installed" in s for s in statuses),
          f"unavailable tools reported as status lines: {statuses}")
    events = await run_turn(adapter)
    check(not any(e.type == StreamEventType.STATUS and "kit__echo" in e.data for e in events),
          "warning shown once, not every turn")
    await adapter.stop()

    section("plugin disabled mid-session")
    await host.set_enabled("kit", True)
    session6 = await db.create_session("main-agent", working_dir=str(wd), session_id="s6")
    model = calls_model([{"id": "m1", "name": "kit__echo", "arguments": {"text": "x"}}])
    adapter = CAgentAdapter(db=db, tool_names=["kit__echo"], session_id=session6.id, working_dir=str(wd),
                            config={}, model_callable=model)
    await adapter.start("test")
    await host.set_enabled("kit", False)
    await run_turn(adapter)
    check(model.results and "kit__echo 不可用" in model.results[0]["content"]
          and "disabled" in model.results[0]["content"],
          f"call after disable returns an error result: {model.results[0]['content'] if model.results else None}")
    await host.set_enabled("kit", True)
    model.results = []
    await run_turn(adapter)
    check(model.results and model.results[0]["content"].startswith("x |"),
          "re-enabled plugin serves the live session again")
    await adapter.stop()

    section("name clash")
    import app.adapters.tools.registry as reg
    from app.adapters.tools.registry import ToolEntry
    reg._REGISTRY["clash__x"] = ToolEntry("clash__x", "builtin squatter", True, lambda: None)  # type: ignore
    await host.set_enabled("clash", False)
    reg._REGISTRY["clash__x"] = ToolEntry("clash__x", "builtin squatter", True, lambda: None)  # type: ignore
    record = await host.set_enabled("clash", True)
    check(record.state == "error" and "already registered" in (record.error or ""),
          f"tool name clash puts the plugin in error: {record.error}")
    check(get_entry("clash__x").description == "builtin squatter", "existing tool not replaced")
    del reg._REGISTRY["clash__x"]

    await host.shutdown()


def api_tests() -> None:
    section("HTTP API")
    from app.main import app

    with TestClient(app) as client:
        tools = client.get("/api/v1/tools").json()
        by_name = {t["name"]: t for t in tools}
        check("read" in by_name and by_name["read"]["source"] == "builtin" and by_name["read"]["plugin_id"] is None,
              "GET /tools: built-in entry")
        echo = by_name.get("kit__echo", {})
        check(echo.get("source") == "plugin" and echo.get("plugin_id") == "kit" and echo.get("plugin_name") == "Kit"
              and echo.get("requires_approval") is False and echo.get("description") == "Repeat text.",
              "GET /tools: plugin entry")
        base = {"name": "a", "description": "", "agent_type": "tool_use", "system_prompt": "",
                "openai_model": "m", "config": {}}
        r = client.post("/api/v1/agents", json={**base, "tool_names": ["read", "kit__echo", "gone__tool"]})
        check(r.status_code == 201, f"agent may list plugin tools, even of missing plugins ({r.status_code})")
        r = client.post("/api/v1/agents", json={**base, "tool_names": ["no_such_builtin"]})
        check(r.status_code == 400, "unknown non-plugin tool still rejected")
        r = client.post("/api/v1/agents", json={**base, "tool_names": ["kit__echo"],
                                               "config": {"tool_approvals": {"kit__echo": True}}})
        check(r.status_code == 201, "tool_approvals accepts plugin tools")


asyncio.run(main())
api_tests()

print()
if failures:
    fail(f"{failures} check(s) failed")
    sys.exit(1)
ok("all plugin tool checks passed")
