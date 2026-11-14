"""Session plugin settings (P2): merge order, validation, masking, API.

Builds throwaway plugins under a temp AUGENTIA_HOME and drives the session /
agent APIs through TestClient (temp SQLite). No network, no model calls: the
sessions are created with the stock agent but no message is ever sent.

Run from backend/:  python scripts/test_plugins_session_config.py
"""
import json
import sys
import textwrap
from pathlib import Path

import _common  # noqa: F401 — sets sys.path + logging

from _common import fail, ok, section, use_isolated_state

ROOT = use_isolated_state("augentia-plugins-session-")
HOME = ROOT / "home"
PLUGINS = HOME / "plugins"

from fastapi.testclient import TestClient  # noqa: E402

failures = 0
MASK = "••••"


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


EMPTY = "from app.plugins.sdk import Plugin\nclass P(Plugin):\n    pass\n"

# Session schema with defaults, a secret, a constrained enum and a nested object.
write_plugin("bridge", EMPTY, {
    "description": "Bridge",
    "config": {"session": {
        "type": "object",
        "properties": {
            "prefix": {"type": "string", "default": "!"},
            "mode": {"type": "string", "enum": ["final_only", "every_step"], "default": "final_only"},
            "token": {"type": "string", "x-secret": True},
            "limits": {"type": "object", "properties": {"rate": {"type": "integer", "minimum": 1, "default": 5}}},
        },
        "additionalProperties": False,
    }},
})
# No session schema, but a hook -> still listed, switch only; on by default.
write_plugin("watcher", """
from app.plugins.sdk import Plugin, hook
class P(Plugin):
    @hook("turn.end")
    async def seen(self, ctx, summary):
        pass
""", {"session": {"default_enabled": True}})
# Tools only: nothing to configure per session -> not listed.
write_plugin("toolsonly", """
from app.plugins.sdk import Plugin, tool
class P(Plugin):
    @tool("t", description="t", parameters={"type": "object", "properties": {}})
    async def t(self, ctx):
        return "t"
""")
# Will be disabled: listed only when a session has an entry for it.
write_plugin("sleepy", EMPTY, {"config": {"session": {"type": "object", "properties": {"x": {"type": "integer"}}}}})

from app.plugins import settings as plugin_settings  # noqa: E402

plugin_settings.set_enabled("sleepy", False)


def views(client, sid):
    r = client.get(f"/api/v1/sessions/{sid}/plugins")
    assert r.status_code == 200, r.text
    return {p["id"]: p for p in r.json()}


def stored(sid) -> dict:
    return json.loads((HOME / "sessions" / sid / "config.json").read_text(encoding="utf-8"))


def main() -> None:
    from app.main import app
    from app.plugins.host import get_plugin_host

    with TestClient(app) as client:
        agent = client.get("/api/v1/agents").json()[0]
        host = get_plugin_host()

        def new_session(agent_id=agent["id"]):
            r = client.post("/api/v1/sessions", json={"agent_id": agent_id})
            assert r.status_code == 201, r.text
            return r.json()["id"]

        section("listing and defaults")
        sid = new_session()
        v = views(client, sid)
        check(set(v) == {"bridge", "watcher"}, f"lists plugins with session schema or hooks: {sorted(v)}")
        b = v["bridge"]
        check(b["enabled"] is False and b["default_enabled"] is False, "default_enabled=false -> off")
        check(b["config"] == {"prefix": "!", "mode": "final_only", "limits": {"rate": 5}},
              f"schema defaults applied: {b['config']}")
        check(b["schema"]["properties"]["prefix"]["default"] == "!", "schema exposed under 'schema'")
        check(v["watcher"]["enabled"] is True and v["watcher"]["schema"] is None
              and v["watcher"]["hooks"] == ["turn.end"], "default_enabled=true, hooks listed, no schema")
        check("plugins" not in stored(sid), "nothing written for untouched plugins")
        check(host.session_enabled("watcher", sid) and not host.session_enabled("bridge", sid),
              "host.session_enabled follows the defaults")

        section("per-plugin PUT")
        r = client.put(f"/api/v1/sessions/{sid}/plugins/bridge", json={"enabled": True})
        check(r.status_code == 200 and r.json()["enabled"] is True, "enable via PUT")
        check(stored(sid)["plugins"]["bridge"] == {"enabled": True}, "only the flag is stored")
        r = client.put(f"/api/v1/sessions/{sid}/plugins/bridge",
                       json={"config": {"prefix": "#", "token": "s3cret"}})
        body = r.json()
        check(r.status_code == 200 and body["enabled"] is True, "config PUT keeps the enabled flag")
        check(body["config"]["token"] == MASK and body["config"]["prefix"] == "#", "response masks the secret")
        check(stored(sid)["plugins"]["bridge"] == {"prefix": "#", "token": "s3cret", "enabled": True},
              "stored in clear, without defaults")
        check(host.session_config("bridge", sid) == {"prefix": "#", "token": "s3cret", "mode": "final_only",
                                                     "limits": {"rate": 5}}, "ctx.config sees real values")
        r = client.put(f"/api/v1/sessions/{sid}/plugins/bridge",
                       json={"config": {"prefix": "%", "token": MASK}})
        check(r.status_code == 200 and stored(sid)["plugins"]["bridge"]["token"] == "s3cret",
              "masked secret sent back keeps the stored one")
        r = client.put(f"/api/v1/sessions/{sid}/plugins/bridge",
                       json={"config": {"mode": "sometimes", "limits": {"rate": 0}, "bogus": 1}})
        errs = r.json().get("detail", {}).get("errors", []) if r.status_code == 400 else []
        check(r.status_code == 400 and any("mode" in e for e in errs) and any("limits.rate" in e for e in errs)
              and any("bogus" in e for e in errs), f"invalid config -> 400 listing every error: {errs}")
        check(stored(sid)["plugins"]["bridge"]["prefix"] == "%", "rejected write leaves the file unchanged")
        check(client.put(f"/api/v1/sessions/{sid}/plugins/nope", json={"enabled": True}).status_code == 404,
              "unknown plugin -> 404")
        r = client.put(f"/api/v1/sessions/{sid}/plugins/watcher", json={"config": {"x": 1}})
        check(r.status_code == 400, "plugin without a session schema rejects config keys")
        r = client.put(f"/api/v1/sessions/{sid}/plugins/watcher", json={"enabled": False})
        check(r.status_code == 200 and r.json()["enabled"] is False and not host.session_enabled("watcher", sid),
              "default-on plugin can be switched off")

        section("whole-config PUT (tool permissions panel)")
        cfg = client.get(f"/api/v1/sessions/{sid}/config").json()
        check(cfg["plugins"]["bridge"]["token"] == MASK, "GET /config masks plugin secrets")
        cfg["tool_permissions"]["default"] = "deny"
        r = client.put(f"/api/v1/sessions/{sid}/config", json={"config": cfg})
        check(r.status_code == 200 and stored(sid)["plugins"]["bridge"]["token"] == "s3cret"
              and stored(sid)["tool_permissions"]["default"] == "deny",
              "round-tripping the masked config keeps the secret")
        # A stored entry that became invalid (plugin upgrade) does not block other edits.
        raw = stored(sid)
        raw["plugins"]["bridge"]["mode"] = "legacy-value"
        (HOME / "sessions" / sid / "config.json").write_text(json.dumps(raw), encoding="utf-8")
        v = views(client, sid)
        check(any("mode" in e for e in v["bridge"]["config_errors"]), "stale entry reported in config_errors")
        cfg = client.get(f"/api/v1/sessions/{sid}/config").json()
        cfg["tool_permissions"]["default"] = "ask"
        r = client.put(f"/api/v1/sessions/{sid}/config", json={"config": cfg})
        check(r.status_code == 200, "unchanged stale entry does not block saving other settings")
        cfg = client.get(f"/api/v1/sessions/{sid}/config").json()
        cfg["plugins"]["bridge"]["prefix"] = "changed"
        r = client.put(f"/api/v1/sessions/{sid}/config", json={"config": cfg})
        check(r.status_code == 400, "editing the stale entry itself is validated")
        cfg["plugins"] = {"bridge": {"enabled": "yes"}}
        check(client.put(f"/api/v1/sessions/{sid}/config", json={"config": cfg}).status_code == 400,
              "non-boolean enabled rejected")
        cfg["plugins"] = {"ghost": {"anything": [1, 2]}, "bridge": {"enabled": True}}
        r = client.put(f"/api/v1/sessions/{sid}/config", json={"config": cfg})
        check(r.status_code == 200 and stored(sid)["plugins"]["ghost"] == {"anything": [1, 2]},
              "entries of plugins that are not installed are kept as is")
        cfg["plugins"] = "nope"
        check(client.put(f"/api/v1/sessions/{sid}/config", json={"config": cfg}).status_code == 400,
              "plugins must be an object")

        section("disabled plugin with a session entry")
        cfg = client.get(f"/api/v1/sessions/{sid}/config").json()
        cfg["plugins"]["sleepy"] = {"enabled": True, "x": 3}
        check(client.put(f"/api/v1/sessions/{sid}/config", json={"config": cfg}).status_code == 200,
              "entry for a disabled plugin can be saved (manifest known)")
        v = views(client, sid)
        check("sleepy" in v and v["sleepy"]["state"] == "disabled" and v["sleepy"]["enabled"] is True,
              "disabled plugin with an entry stays visible")
        check(not host.session_enabled("sleepy", sid), "but it is not active while disabled globally")

        section("agent defaults")
        base = {"name": "with-plugins", "description": "", "agent_type": "tool_use", "system_prompt": "",
                "tool_names": [], "openai_model": "m"}
        r = client.post("/api/v1/agents", json={**base, "config": {"plugins": {"bridge": {"mode": "bad"}}}})
        check(r.status_code == 400, "agent with invalid plugin defaults rejected")
        r = client.post("/api/v1/agents", json={**base, "config": {"plugins": {
            "bridge": {"enabled": True, "prefix": "@", "token": "agent-secret"}, "watcher": {"enabled": False}}}})
        check(r.status_code == 201, "agent with plugin defaults saved")
        a = r.json()
        check(a["config"]["plugins"]["bridge"]["token"] == MASK, "agent response masks plugin secrets")
        sid2 = new_session(a["id"])
        v = views(client, sid2)
        check(v["bridge"]["enabled"] is True and v["bridge"]["config"]["prefix"] == "@"
              and v["watcher"]["enabled"] is False, "new session starts from the agent's defaults")
        check(host.session_config("bridge", sid2)["token"] == "agent-secret", "agent secret reaches the session")
        a["config"]["plugins"]["bridge"]["prefix"] = "&"
        r = client.put(f"/api/v1/agents/{a['id']}", json={"config": a["config"]})
        check(r.status_code == 200, "agent update with masked secret accepted")
        r = client.get(f"/api/v1/agents/{a['id']}")
        check(r.json()["config"]["plugins"]["bridge"]["prefix"] == "&", "agent defaults updated")
        sid3 = new_session(a["id"])
        check(host.session_config("bridge", sid3)["token"] == "agent-secret",
              "masked agent secret round-trip kept the real secret")
        check(views(client, sid2)["bridge"]["config"]["prefix"] == "@", "existing sessions keep their copy")


main()
print()
if failures:
    fail(f"{failures} check(s) failed")
    sys.exit(1)
ok("all session plugin config checks passed")
