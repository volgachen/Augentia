"""Plugin host P0: discovery, manifest validation, loading, enable/disable, global config.

Builds throwaway plugins under a temp AUGENTIA_HOME (and AUGENTIA_PLUGIN_PATH)
and drives them through PluginHost and the HTTP API. No network, no model.

Run from backend/:  python scripts/test_plugins_host.py
"""
import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import _common  # noqa: F401 — sets sys.path + logging

from _common import fail, ok, section, use_isolated_state

ROOT = use_isolated_state("augentia-plugins-host-")
HOME = ROOT / "home"
EXTRA = ROOT / "extra-plugins"
os.environ["AUGENTIA_PLUGIN_PATH"] = str(EXTRA)
from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from fastapi.testclient import TestClient  # noqa: E402

from app.plugins import settings as plugin_settings  # noqa: E402
from app.plugins.host import PluginConfigError, PluginHost  # noqa: E402

failures = 0


def check(cond: bool, msg: str) -> None:
    global failures
    if cond:
        ok(msg)
    else:
        failures += 1
        fail(msg)


def write_plugin(root: Path, plugin_id: str, code: str, manifest: dict | None = None, files: dict | None = None):
    d = root / plugin_id
    d.mkdir(parents=True, exist_ok=True)
    base = {
        "id": plugin_id,
        "name": plugin_id.title(),
        "version": "1.0.0",
        "api_version": 1,
        "entry": "plugin:P",
    }
    if manifest:
        base.update(manifest)
    (d / "plugin.json").write_text(json.dumps(base), encoding="utf-8")
    (d / "plugin.py").write_text(textwrap.dedent(code), encoding="utf-8")
    for name, text in (files or {}).items():
        (d / name).write_text(textwrap.dedent(text), encoding="utf-8")
    return d


PLUGINS = HOME / "plugins"

GOOD = """
from app.plugins.sdk import Plugin, tool, hook, service, action
from .helper import GREETING

LOADED = []

class P(Plugin):
    async def on_load(self, ctx):
        LOADED.append(ctx.plugin_id)
        self.cfg_at_load = ctx.global_config
        (ctx.data_dir / "marker").write_text(GREETING)

    async def on_unload(self, ctx):
        (ctx.data_dir / "unloaded").write_text("yes")

    async def on_config_changed(self, ctx, old, new):
        (ctx.data_dir / "config_changed").write_text(f"{old.get('interval')}->{new.get('interval')}")

    @tool("echo", description="Echo text back.",
          parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
          requires_approval=False)
    async def echo(self, ctx, text):
        return text

    @hook("tool.before", priority=5)
    @hook("turn.end")
    async def watch(self, ctx, *args):
        return None

    @service("poller", scope="session", autostart=False)
    async def poller(self, ctx):
        pass

    @action("connect")
    async def connect(self, ctx, data):
        return {"ok": True}
"""

GOOD_MANIFEST = {
    "description": "A good plugin",
    "config": {
        "global": {
            "type": "object",
            "properties": {
                "interval": {"type": "integer", "minimum": 1, "default": 30},
                "token": {"type": "string", "x-secret": True},
                "nested": {"type": "object", "properties": {"flag": {"type": "boolean", "default": True}}},
            },
            "additionalProperties": False,
        },
        "session": {"type": "object", "properties": {"prefix": {"type": "string", "default": "!"}}},
    },
    "session": {"default_enabled": True},
    "http": {"public_routes": ["POST /callback"]},
}

write_plugin(PLUGINS, "good", GOOD, GOOD_MANIFEST, files={"helper.py": "GREETING = 'hi'\n"})
write_plugin(PLUGINS, "bad-json", "")
(PLUGINS / "bad-json" / "plugin.json").write_text("{not json", encoding="utf-8")
write_plugin(PLUGINS, "bad-field", "", {"subscriptions": ["x"]})
write_plugin(PLUGINS, "wrong-id", "", {"id": "other"})
write_plugin(PLUGINS, "future-api", "", {"api_version": 99})
write_plugin(PLUGINS, "needs-dep", "", {"python_dependencies": ["definitely-not-installed-pkg-xyz>=1"]})
write_plugin(PLUGINS, "syntax", "def broken(:\n")
write_plugin(PLUGINS, "raises", "raise RuntimeError('boom at import')\n")
write_plugin(PLUGINS, "not-plugin", "class P:\n    pass\n")
write_plugin(PLUGINS, "no-class", "X = 1\n")
write_plugin(PLUGINS, "load-fails", """
from app.plugins.sdk import Plugin
class P(Plugin):
    async def on_load(self, ctx):
        raise ValueError('cannot connect')
""")
write_plugin(PLUGINS, "dup-tool", """
from app.plugins.sdk import Plugin, tool
S = {"type": "object", "properties": {}}
class P(Plugin):
    @tool("a", description="x", parameters=S)
    async def one(self, ctx): pass
    @tool("a", description="y", parameters=S)
    async def two(self, ctx): pass
""")
write_plugin(PLUGINS, "sync-tool", """
from app.plugins.sdk import Plugin, tool
class P(Plugin):
    @tool("a", description="x", parameters={"type": "object"})
    def one(self, ctx): pass
""")
write_plugin(PLUGINS, "stream", "", {})  # reserved id (/plugins/stream)
(PLUGINS / "no-manifest").mkdir()
(PLUGINS / ".hidden").mkdir()
(PLUGINS / ".hidden" / "plugin.json").write_text("{}", encoding="utf-8")
# Same id in the extra path: shadowed by the home copy.
write_plugin(EXTRA, "good", "raise SystemExit('must not be imported')\n")
write_plugin(EXTRA, "extra-only", """
from app.plugins.sdk import Plugin
class P(Plugin):
    pass
""")


async def host_tests() -> None:
    host = PluginHost()
    await host.load_all()
    infos = {(i.id, i.path): i for i in host.list_info()}
    by_id = {i.id: i for i in host.list_info() if Path(i.path).parent == PLUGINS.resolve()
             or i.id == "extra-only"}

    section("discovery")
    check("no-manifest" not in by_id and ".hidden" not in by_id, "dirs without plugin.json and dot-dirs are skipped")
    check("extra-only" in by_id and by_id["extra-only"].state == "loaded",
          "AUGENTIA_PLUGIN_PATH plugins are discovered and loaded")
    shadowed = [i for (pid, path), i in infos.items() if pid == "good" and Path(path).parent == EXTRA.resolve()]
    check(len(shadowed) == 1 and shadowed[0].state == "error" and "already provided" in (shadowed[0].error or ""),
          "a second copy of an id is reported as shadowed, not imported")

    section("good plugin")
    good = by_id["good"]
    check(good.state == "loaded" and good.enabled, "good plugin loaded and enabled by default")
    check(good.name == "Good" and good.description == "A good plugin" and good.session_default_enabled,
          "manifest fields exposed")
    caps = good.capabilities
    check(caps is not None and [t.full_name for t in caps.tools] == ["good__echo"]
          and caps.tools[0].requires_approval is False, "tool collected with full name and approval default")
    check(caps is not None and [(h.event, h.priority) for h in caps.hooks] == [("tool.before", 5), ("turn.end", 100)],
          "stacked hooks collected in turn order")
    check(caps is not None and caps.services[0].scope == "session" and caps.actions == ["connect"],
          "services and actions collected")
    check(good.public_routes == ["POST /callback"], "public routes from manifest")
    data_dir = HOME / "plugin_data" / "good"
    check((data_dir / "marker").read_text() == "hi", "on_load ran, relative import + data_dir work")
    record = host.get("good")
    check(record.instance.cfg_at_load == {"interval": 30, "nested": {"flag": True}},
          "global_config has schema defaults (secret without default stays absent)")

    section("errors")
    expectations = {
        "bad-json": "not valid JSON",
        "bad-field": "subscriptions",
        "wrong-id": "must equal the directory name",
        "future-api": "api_version 99 is not supported",
        "needs-dep": "missing Python packages: definitely-not-installed-pkg-xyz>=1",
        "syntax": "SyntaxError",
        "raises": "boom at import (plugin.py:1)",
        "not-plugin": "subclass of app.plugins.sdk.Plugin",
        "no-class": "has no attribute 'P'",
        "load-fails": "on_load failed: ValueError: cannot connect",
        "dup-tool": "duplicate tool 'a'",
        "sync-tool": "must be an async function",
        "stream": "reserved",
    }
    for plugin_id, needle in expectations.items():
        info = by_id.get(plugin_id)
        check(info is not None and info.state == "error" and needle in (info.error or ""),
              f"{plugin_id}: error mentions {needle!r} (got {info.error if info else None!r})")
    check(by_id["needs-dep"].missing_dependencies == ["definitely-not-installed-pkg-xyz>=1"],
          "missing dependencies listed separately")
    check(not any(k.startswith("augentia_plugins.raises") for k in sys.modules),
          "failed import leaves no modules behind")

    section("global config")
    masked = host.masked_global_config("good")
    check(masked == {"interval": 30, "nested": {"flag": True}}, "defaults returned when nothing saved")
    result = await host.set_global_config("good", {"interval": 5, "token": "s3cret"})
    check(result["token"] == plugin_settings.SECRET_MASK and result["interval"] == 5, "secret masked on save response")
    check((data_dir / "config_changed").read_text() == "30->5", "on_config_changed got old and new effective config")
    stored = json.loads((HOME / "plugin_settings.json").read_text(encoding="utf-8"))
    check(stored["good"]["config"] == {"interval": 5, "token": "s3cret"}, "stored without defaults, secret in clear")
    await host.set_global_config("good", {"interval": 7, "token": plugin_settings.SECRET_MASK})
    check(plugin_settings.stored_global_config("good")["token"] == "s3cret", "mask sent back keeps the stored secret")
    check(host.get("good").ctx.global_config["interval"] == 7, "ctx.global_config sees the new value")
    try:
        await host.set_global_config("good", {"interval": 0, "bogus": 1})
        check(False, "invalid config rejected")
    except PluginConfigError as e:
        check(any("interval" in m for m in e.errors) and any("bogus" in m for m in e.errors),
              f"invalid config rejected with all errors: {e.errors}")
    check(plugin_settings.stored_global_config("good")["interval"] == 7, "rejected config not written")

    section("enable / disable")
    record = await host.set_enabled("good", False)
    check(record.state == "disabled" and record.instance is None, "disable unloads")
    check((data_dir / "unloaded").exists(), "on_unload ran")
    check(not any(k.startswith("augentia_plugins.good") for k in sys.modules), "modules purged on disable")
    check(host.info(record).capabilities is None and not host.info(record).enabled, "disabled info has no capabilities")

    host2 = PluginHost()
    await host2.load_all()
    check(host2.get("good").state == "disabled", "disabled flag persists across hosts (restart)")

    (PLUGINS / "good" / "helper.py").write_text("GREETING = 'version-2'\n", encoding="utf-8")
    record = await host.set_enabled("good", True)
    check(record.state == "loaded" and (data_dir / "marker").read_text() == "version-2",
          "re-enable re-imports code (picks up edits)")

    record = await host.set_enabled("bad-json", False)
    check(record.state == "error", "disabling a broken-manifest plugin keeps the error state")

    section("reload")
    (PLUGINS / "needs-dep" / "plugin.json").write_text(json.dumps({
        "id": "needs-dep", "name": "Needs", "version": "1", "api_version": 1, "entry": "plugin:P",
    }), encoding="utf-8")
    (PLUGINS / "needs-dep" / "plugin.py").write_text(
        "from app.plugins.sdk import Plugin\nclass P(Plugin):\n    pass\n", encoding="utf-8")
    write_plugin(PLUGINS, "brand-new", "from app.plugins.sdk import Plugin\nclass P(Plugin):\n    pass\n")
    import shutil
    shutil.rmtree(EXTRA / "extra-only")
    good_instance = host.get("good").instance
    changed = await host.reload()
    check("brand-new" in changed and host.get("brand-new").state == "loaded", "reload loads new plugins")
    check(host.get("needs-dep").state == "loaded", "reload retries plugins in error state")
    check("extra-only" in changed and "extra-only" not in {i.id for i in host.list_info()},
          "reload drops plugins whose directory is gone")
    check(host.get("good").instance is good_instance, "reload keeps loaded plugins as they are")

    await host.shutdown()
    check(all(r.state != "loaded" for r in host._records.values()), "shutdown unloads everything")
    await host2.shutdown()


def api_tests() -> None:
    section("HTTP API")
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/v1/plugins")
        check(r.status_code == 200 and any(p["id"] == "good" for p in r.json()), "GET /plugins lists plugins")
        r = client.get("/api/v1/plugins/good")
        check(r.status_code == 200 and r.json()["state"] == "loaded", "GET /plugins/{id}")
        check(client.get("/api/v1/plugins/nope").status_code == 404, "unknown plugin -> 404")
        r = client.get("/api/v1/plugins/good/config")
        check(r.status_code == 200 and r.json()["token"] == plugin_settings.SECRET_MASK, "GET config masks secret")
        r = client.put("/api/v1/plugins/good/config", json={"config": {"interval": -1}})
        check(r.status_code == 400 and r.json()["detail"]["errors"], "PUT invalid config -> 400 with errors")
        r = client.put("/api/v1/plugins/good/config", json={"config": {"interval": 9}})
        check(r.status_code == 200 and r.json()["interval"] == 9, "PUT config")
        check(client.get("/api/v1/plugins/bad-json/config").status_code == 409, "config of broken plugin -> 409")
        r = client.put("/api/v1/plugins/good/enabled", json={"enabled": False})
        check(r.status_code == 200 and r.json()["state"] == "disabled", "PUT enabled=false")
        r = client.put("/api/v1/plugins/good/enabled", json={"enabled": True})
        check(r.status_code == 200 and r.json()["state"] == "loaded", "PUT enabled=true")
        r = client.post("/api/v1/plugins/reload")
        check(r.status_code == 200 and "plugins" in r.json(), "POST /plugins/reload")
        # The v1 instance API is gone (P5).
        check(client.get("/api/v1/plugins/instances").status_code == 404, "v1 /plugins/instances removed")


asyncio.run(host_tests())
api_tests()

print()
if failures:
    fail(f"{failures} check(s) failed")
    sys.exit(1)
ok("all plugin host checks passed")
