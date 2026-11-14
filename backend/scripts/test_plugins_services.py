"""Plugin services, events, UI state, actions and the live stream (P4).

Throwaway plugins under a temp AUGENTIA_HOME; the app runs in-process on a
temp SQLite DB (TestClient for HTTP + WebSocket), sessions use the stock agent
with a scripted model patched into the adapter. Restart timing constants are
shrunk so backoff tests run in well under a second.

Run from backend/:  python scripts/test_plugins_services.py
"""
import json
import sys
import textwrap
import time
from pathlib import Path

import _common  # noqa: F401 — sets sys.path + logging

from _common import fail, ok, section, use_isolated_state

ROOT = use_isolated_state("augentia-plugins-services-")
HOME = ROOT / "home"
PLUGINS = HOME / "plugins"

from fastapi.testclient import TestClient  # noqa: E402

import app.plugins.services as services_module  # noqa: E402

services_module.BACKOFF_START = 0.05
services_module.BACKOFF_MAX = 0.2
services_module.RESTART_LIMIT = 3
services_module.STOP_GRACE = 0.5

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
    base = {"id": plugin_id, "name": plugin_id.title(), "version": "1", "api_version": 1, "entry": "plugin:P"}
    base.update(manifest or {})
    (d / "plugin.json").write_text(json.dumps(base), encoding="utf-8")
    (d / "plugin.py").write_text(textwrap.dedent(code), encoding="utf-8")


# A bridge-like plugin: a global ticker, a per-session "bridge" that relays
# messages, events, UI and actions.
write_plugin("bridge", """
import asyncio
from app.plugins.sdk import Plugin, service, action, hook

EVENTS = []
STOPPED = []

class P(Plugin):
    async def on_load(self, ctx):
        self.unsub = ctx.events.subscribe("message.created", self.on_message)
        ctx.events.subscribe("session.created", self.on_session)
        ctx.events.subscribe("session.deleted", self.on_session)

    async def on_message(self, event):
        EVENTS.append(("message.created", event.data.get("role"), event.data.get("session_id"), event.source))

    async def on_session(self, event):
        EVENTS.append((event.type, event.data.get("id") or event.data.get("session_id")))

    @service("ticker")
    async def ticker(self, ctx):
        ctx.log.info("ticker up", interval=0.05)
        try:
            while True:
                await asyncio.sleep(0.05)
        finally:
            STOPPED.append("ticker")

    @service("relay", scope="session", autostart=True)
    async def relay(self, ctx):
        ctx.log.info("relay up")
        await ctx.ui.update({"status": "ok", "message": f"connected {ctx.session_id[:4]}",
                             "actions": [{"id": "send", "label": "Send hello"},
                                         {"id": "disconnect", "label": "Disconnect", "style": "danger"}]})
        ctx.state["relay"] = True
        try:
            await asyncio.Event().wait()
        finally:
            ctx.state.pop("relay", None)
            STOPPED.append(("relay", ctx.session_id))

    @service("login", scope="session", autostart=False)
    async def login(self, ctx):
        ctx.ui.update_nowait({"status": "waiting", "message": "scan me", "qr": "https://example/qr"})
        await asyncio.Event().wait()

    @action("send")
    async def send(self, ctx, data):
        await ctx.sessions.send(ctx.session_id, data.get("text", "hello from plugin"), label="tester")
        return {"sent": True, "relay": ctx.state.get("relay", False)}

    @action("connect")
    async def connect(self, ctx, data):
        await ctx.services.get("login").start()
        return "starting"

    @action("disconnect")
    async def disconnect(self, ctx, data):
        await ctx.services.get("login").stop()
        await ctx.ui.update({"status": "idle", "message": "disconnected"})

    @action("boom")
    async def boom(self, ctx, data):
        raise ValueError("cannot do that")

    @action("who")
    async def who(self, ctx, data):
        info = await ctx.session()
        return {"id": info.id, "status": info.status}

    @hook("turn.end")
    async def end(self, ctx, summary):
        EVENTS.append(("turn.end", ctx.session_id, summary.final_text))
""", {"config": {"global": {"type": "object", "properties": {"label": {"type": "string", "default": "a"}}}}})

# Crashes on start: restart policy.
write_plugin("crashy", """
from app.plugins.sdk import Plugin, service
COUNT = [0]
class P(Plugin):
    @service("flaky")
    async def flaky(self, ctx):
        COUNT[0] += 1
        raise RuntimeError(f"crash #{COUNT[0]}")

    @service("once", restart="never")
    async def once(self, ctx):
        return None

    @service("forever", restart="always", autostart=False)
    async def forever(self, ctx):
        return None
""")

# Ignores cancellation for a while: stop grace.
write_plugin("stubborn", """
import asyncio
from app.plugins.sdk import Plugin, service
class P(Plugin):
    @service("stuck")
    async def stuck(self, ctx):
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)  # swallows the first cancel, then...
                raise
""")


def wait_for(cond, timeout=3.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


def scripted_model():
    async def model(*, messages, tools, tool_choice=None):
        return {"content": "echo: " + str(messages[-1].get("content"))[:40],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    return model


def main() -> None:
    import app.adapters.cagent_adapter as adapter_module
    original_init = adapter_module.CAgentAdapter.__init__

    def init_with_script(self, *args, **kwargs):
        kwargs["model_callable"] = scripted_model()
        original_init(self, *args, **kwargs)

    adapter_module.CAgentAdapter.__init__ = init_with_script

    from app.main import app
    from app.plugins.host import get_plugin_host

    with TestClient(app) as client:
        host = get_plugin_host()
        import augentia_plugins.bridge.plugin as bridge

        def api(method, path, **kw):
            return getattr(client, method)(f"/api/v1{path}", **kw)

        def svc(plugin_id, name, session_id=None):
            return host.services.get(plugin_id, name, session_id)

        section("global services")
        check(wait_for(lambda: svc("bridge", "ticker") and svc("bridge", "ticker").status == "running"),
              "autostart global service runs after load")
        runs = api("get", "/plugins/bridge/runs?service=ticker").json()
        check(len(runs) == 1 and runs[0]["status"] == "running" and runs[0]["session_id"] is None,
              "a run is recorded")
        check(wait_for(lambda: any(l["line"].startswith("ticker up") for l in
                                   api("get", "/plugins/bridge/logs").json())),
              "ctx.log lines are persisted")
        logs = api("get", "/plugins/bridge/logs").json()
        line = next(l for l in logs if l["line"].startswith("ticker up"))
        check(line["run_id"] == runs[0]["id"] and "interval=0.05" in line["line"],
              "service log lines carry the run id and fields")
        r = api("post", "/plugins/bridge/services/ticker/stop")
        check(r.status_code == 200 and r.json()["status"] == "stopped" and "ticker" in bridge.STOPPED,
              "stop cancels the service and its finally runs")
        check(api("get", "/plugins/bridge/runs?service=ticker").json()[0]["status"] == "exited",
              "stopped run recorded as exited")
        r = api("post", "/plugins/bridge/services/ticker/start")
        check(r.status_code == 200 and wait_for(lambda: svc("bridge", "ticker").status == "running"),
              "manual start")
        check(api("post", "/plugins/bridge/services/nope/start").status_code == 404, "unknown service -> 404")
        check(api("post", "/plugins/bridge/services/relay/start").status_code == 400,
              "session service without session_id -> 400")

        section("restart policy")
        check(wait_for(lambda: svc("crashy", "flaky") and svc("crashy", "flaky").status == "errored", timeout=5),
              "crashing service gives up after the restart limit")
        state = svc("crashy", "flaky")
        check(state.restart_count == 3 and "gave up after 3 restarts" in (state.error or ""),
              f"restart count and reason: {state.restart_count} {state.error}")
        runs = api("get", "/plugins/crashy/runs?service=flaky").json()
        check(len(runs) == 4 and all(r["status"] == "errored" for r in runs)
              and [r["restart_count"] for r in reversed(runs)] == [0, 1, 2, 3],
              f"one run per attempt: {[(r['status'], r['restart_count']) for r in runs]}")
        check(any("crash #1" in l["line"] and l["level"] == "error"
                  for l in api("get", "/plugins/crashy/logs").json()), "crash logged as error")
        check(wait_for(lambda: svc("crashy", "once") and svc("crashy", "once").status == "exited"),
              "restart=never: a clean exit stays exited")
        api("post", "/plugins/crashy/services/forever/start")
        check(wait_for(lambda: svc("crashy", "forever").restart_count >= 2, timeout=3),
              "restart=always restarts even after clean exits")
        api("post", "/plugins/crashy/services/forever/stop")
        check(wait_for(lambda: svc("crashy", "forever").status == "stopped"), "stop during backoff")
        count = svc("crashy", "forever").restart_count
        time.sleep(0.4)
        check(svc("crashy", "forever").restart_count == count, "no restarts after stop")

        section("stop grace")
        check(wait_for(lambda: svc("stubborn", "stuck") and svc("stubborn", "stuck").status == "running"),
              "stubborn service running")
        t0 = time.monotonic()
        api("post", "/plugins/stubborn/services/stuck/stop")
        check(svc("stubborn", "stuck").status == "stopped" and time.monotonic() - t0 < 2,
              "service that delays on cancel still stops")

        section("session services and switches")
        agent_id = api("get", "/agents").json()[0]["id"]
        sid = api("post", "/sessions", json={"agent_id": agent_id}).json()["id"]
        check(svc("bridge", "relay", sid) is None, "session service not started while the plugin is off")
        r = api("put", f"/sessions/{sid}/plugins/bridge", json={"enabled": True})
        check(r.status_code == 200 and wait_for(lambda: svc("bridge", "relay", sid)
                                                and svc("bridge", "relay", sid).status == "running"),
              "enabling the plugin starts its autostart session service")
        check(svc("bridge", "login", sid) is None, "autostart=False session service not started")
        view = next(p for p in api("get", f"/sessions/{sid}/plugins").json() if p["id"] == "bridge")
        check(view["ui"] and view["ui"]["status"] == "ok" and view["ui"]["actions"][0]["id"] == "send",
              f"session view carries the UI card: {view['ui']}")
        check(any(s["service"] == "relay" and s["status"] == "running" for s in view["services"]),
              "session view carries service state")
        check(view["actions"] == ["boom", "connect", "disconnect", "send", "who"], "actions listed")

        # Changing the model rebuilds the runner; the session service must not notice.
        before_run = svc("bridge", "relay", sid).run_id
        api("put", f"/sessions/{sid}/model", json={"model": "other-model"})
        check(svc("bridge", "relay", sid).run_id == before_run and svc("bridge", "relay", sid).status == "running",
              "runner rebuild (model change) does not restart session services")

        section("actions")
        r = api("post", "/plugins/bridge/actions/connect", json={"session_id": sid})
        check(r.status_code == 200 and r.json() == {"result": "starting"}
              and wait_for(lambda: svc("bridge", "login", sid) and svc("bridge", "login", sid).status == "running"),
              "action starts a session service")
        check(wait_for(lambda: (host.services.ui_state("bridge", sid) or None) is not None
                       and host.services.ui_state("bridge", sid).qr == "https://example/qr"),
              "update_nowait from the service replaces the UI card")
        r = api("post", "/plugins/bridge/actions/disconnect", json={"session_id": sid})
        check(r.status_code == 200 and svc("bridge", "login", sid).status == "stopped"
              and host.services.ui_state("bridge", sid).message == "disconnected", "action stops it")
        r = api("post", "/plugins/bridge/actions/boom", json={"session_id": sid})
        check(r.status_code == 422 and "cannot do that" in r.json()["detail"], "failing action -> 422 with message")
        check(api("post", "/plugins/bridge/actions/nope", json={}).status_code == 404, "unknown action -> 404")
        check(api("post", "/plugins/bridge/actions/send", json={"session_id": "missing"}).status_code == 404,
              "unknown session -> 404")
        r = api("post", "/plugins/bridge/actions/who", json={"session_id": sid})
        check(r.json()["result"]["id"] == sid, "ctx.session() snapshot")

        section("ctx.sessions.send, events, turn.end")
        bridge.EVENTS.clear()
        r = api("post", "/plugins/bridge/actions/send", json={"session_id": sid, "data": {"text": "ping"}})
        check(r.json()["result"] == {"sent": True, "relay": True}, "action sees state set by the service")

        def messages():
            return api("get", f"/sessions/{sid}/messages").json()

        check(wait_for(lambda: any(m["role"] == "agent" for m in messages()), timeout=5), "the turn ran")
        user = next(m for m in messages() if m["role"] == "user")
        check(user["from_session_id"] == "plugin:bridge" and user["metadata"].get("plugin_label") == "tester",
              f"message source and label: {user['from_session_id']} {user['metadata']}")
        check(wait_for(lambda: ("message.created", "agent", sid, None) in
                       [(e[0], e[1], e[2], None) for e in bridge.EVENTS if e[0] == "message.created"]),
              "ctx.events receives message.created")
        check(wait_for(lambda: any(e[0] == "turn.end" and e[1] == sid for e in bridge.EVENTS)),
              "turn.end hook ran for the plugin-sent message")

        # A dormant session (no runner after restart) is rehydrated by send().
        from app.adapters.registry import get_registry
        import asyncio as _asyncio  # noqa: F401
        client.portal.call(get_registry().remove, sid)
        count_before = len([m for m in messages() if m["role"] == "agent"])
        api("post", "/plugins/bridge/actions/send", json={"session_id": sid, "data": {"text": "wake up"}})
        check(wait_for(lambda: len([m for m in messages() if m["role"] == "agent"]) > count_before, timeout=5),
              "send() rehydrates a session without a live runner")

        sid2 = api("post", "/sessions", json={"agent_id": agent_id}).json()["id"]
        check(wait_for(lambda: ("session.created", sid2) in bridge.EVENTS), "session.created event")

        section("live stream")
        with client.websocket_connect(f"/api/v1/plugins/stream?session_id={sid}") as ws:
            snap = json.loads(ws.receive_text())
            check(snap["type"] == "snapshot" and any(p["id"] == "bridge" for p in snap["data"]["plugins"])
                  and any(s["service"] == "relay" and s["session_id"] == sid for s in snap["data"]["services"])
                  and any(u["session_id"] == sid for u in snap["data"]["ui"]),
                  "snapshot has plugins, this session's services and UI")
            check(not any(s["session_id"] == sid2 for s in snap["data"]["services"]),
                  "other sessions filtered out")
            api("post", "/plugins/bridge/actions/connect", json={"session_id": sid})
            seen = []
            for _ in range(10):
                frame = json.loads(ws.receive_text())
                seen.append(frame["type"])
                if frame["type"] == "ui" and frame["data"].get("qr"):
                    break
            check("service" in seen and seen[-1] == "ui", f"service and ui frames streamed: {seen}")
            api("post", "/plugins/bridge/actions/disconnect", json={"session_id": sid})

        section("session deletion, disabling, global config")
        api("put", f"/sessions/{sid2}/plugins/bridge", json={"enabled": True})
        check(wait_for(lambda: svc("bridge", "relay", sid2) and svc("bridge", "relay", sid2).status == "running"),
              "second session's relay running")
        api("put", f"/sessions/{sid2}/plugins/bridge", json={"enabled": False})
        check(wait_for(lambda: svc("bridge", "relay", sid2).status == "stopped")
              and ("relay", sid2) in bridge.STOPPED, "switching the plugin off stops the session service")
        api("put", f"/sessions/{sid2}/plugins/bridge", json={"enabled": True})
        check(wait_for(lambda: svc("bridge", "relay", sid2).status == "running"), "and on again restarts it")
        api("delete", f"/sessions/{sid2}")
        check(wait_for(lambda: svc("bridge", "relay", sid2).status == "stopped"), "deleting the session stops it")
        check(wait_for(lambda: ("session.deleted", sid2) in bridge.EVENTS), "session.deleted event")

        run_before = svc("bridge", "ticker").run_id
        r = api("put", "/plugins/bridge/config", json={"config": {"label": "b"}})
        check(r.status_code == 200 and wait_for(lambda: svc("bridge", "ticker").run_id != run_before
                                                and svc("bridge", "ticker").status == "running"),
              "global config change restarts global services (default on_config_changed)")
        check(svc("bridge", "relay", sid).status == "running", "but not session services")

        r = api("put", "/plugins/bridge/enabled", json={"enabled": False})
        check(r.status_code == 200 and not host.services.states("bridge")
              and host.services.ui_state("bridge", sid) is None, "disabling stops and forgets services and UI")
        bridge.EVENTS.clear()
        api("post", "/sessions", json={"agent_id": agent_id})
        time.sleep(0.2)
        check(not bridge.EVENTS, "event subscriptions removed on unload")
        r = api("put", "/plugins/bridge/enabled", json={"enabled": True})
        check(r.status_code == 200 and wait_for(lambda: svc("bridge", "relay", sid)
                                                and svc("bridge", "relay", sid).status == "running"),
              "re-enabling restarts session services of sessions that enable it")

    section("shutdown and restart recovery")
    import sqlite3
    from app.config import get_settings
    conn = sqlite3.connect(get_settings().sqlite_path)
    statuses = [r[0] for r in conn.execute(
        "SELECT status FROM plugin_service_runs WHERE plugin_id = 'stubborn'")]
    check(statuses and all(s in ("exited", "errored") for s in statuses),
          f"shutdown closed every run: {statuses}")
    # Simulate a crash: a run left 'running' in the DB is marked errored on the next start.
    conn.execute("INSERT INTO plugin_service_runs (id, plugin_id, service, status, restart_count, started_at) "
                 "VALUES ('leftover', 'stubborn', 'stuck', 'running', 0, '2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    with TestClient(app) as client:
        runs = client.get("/api/v1/plugins/stubborn/runs").json()
        old = next(r for r in runs if r["id"] == "leftover")
        check(old["status"] == "errored" and "backend restarted" in (old["error"] or ""),
              "a run left running by a crash is marked errored at startup")
        check(any(r["id"] != "leftover" and r["status"] == "running" for r in runs),
              "and the service starts again")

main()
print()
if failures:
    fail(f"{failures} check(s) failed")
    sys.exit(1)
ok("all plugin service checks passed")
