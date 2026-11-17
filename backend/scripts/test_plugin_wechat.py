"""WeChat bridge plugin end to end, with a fake wechatbot package.

Covers the account-level bridge: one global login, an inbound session set
from a session card, ``\\cmd bind`` / ``\\cmd unbind`` for a temporary
inbound + mirror session, and replies only through the wechat__send tool.

Loads backend/plugins/wechat through AUGENTIA_PLUGIN_PATH. A fake
``wechatbot.WeChatBot`` (installed into sys.modules and as a distribution
so the dependency check passes) lets the test play WeChat: trigger the QR
callbacks, deliver incoming messages and read what the plugin sent back.
The sessions run the real CAgent engine with a scripted model.

Run from backend/:  python scripts/test_plugin_wechat.py
"""
import asyncio
import importlib.metadata
import json
import sys
import time
import types
from pathlib import Path

import _common  # noqa: F401 — sets sys.path + logging

from _common import fail, ok, section, use_isolated_state

ROOT = use_isolated_state("augentia-plugin-wechat-")
BACKEND = Path(__file__).resolve().parent.parent
import os  # noqa: E402

os.environ["AUGENTIA_PLUGIN_PATH"] = str(BACKEND / "plugins")
from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

failures = 0


def check(cond: bool, msg: str) -> None:
    global failures
    if cond:
        ok(msg)
    else:
        failures += 1
        fail(msg)


# ---- fake wechatbot ----

class FakeMsg:
    def __init__(self, user_id, text):
        self.user_id = user_id
        self.text = text


class FakeBot:
    instances: list["FakeBot"] = []
    need_qr = True

    def __init__(self, *, cred_path, on_qr_url, on_scanned, on_expired, on_error):
        self.cred_path = cred_path
        self.callbacks = dict(qr=on_qr_url, scanned=on_scanned, expired=on_expired, error=on_error)
        self.handler = None
        self.sent: list[tuple[str, str]] = []
        self.allow_login = asyncio.Event()
        self.stopped = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        FakeBot.instances.append(self)

    def on_message(self, fn):
        self.handler = fn
        return fn

    async def login(self):
        if FakeBot.need_qr and not Path(self.cred_path).exists():
            self.callbacks["qr"]("https://login.weixin.example/qr/123")
            await self.allow_login.wait()
            self.callbacks["scanned"]()
            Path(self.cred_path).write_text("{}")

    async def start(self):
        await self.stopped.wait()

    def stop(self):
        self.loop.call_soon_threadsafe(self.stopped.set)

    async def send(self, user_id, text):
        self.sent.append((user_id, text))

    async def deliver(self, user_id, text):
        await self.handler(FakeMsg(user_id, text))


fake_module = types.ModuleType("wechatbot")
fake_module.WeChatBot = FakeBot
sys.modules["wechatbot"] = fake_module
_real_distribution = importlib.metadata.distribution


def _distribution(name):
    if name == "wechatbot":
        return object()
    return _real_distribution(name)


importlib.metadata.distribution = _distribution


# ---- scripted model ----

def _call(text):
    return {"id": f"t{time.monotonic_ns()}", "name": "wechat__send", "arguments": {"text": text}}


def model_factory():
    async def model(*, messages, tools, tool_choice=None):
        last = messages[-1]
        names = [t["name"] for t in tools]
        content = str(last["content"])
        if last["role"] == "user" and "please reply" in content and "wechat__send" in names:
            return {"content": "Replying on WeChat.", "tool_calls": [_call("hi from the agent")]}
        if last["role"] == "user" and "use a tool" in content and "wechat__send" in names:
            return {"content": "Let me check.", "tool_calls": [_call("tool text")]}
        if last["role"] == "tool":
            return {"content": "Done.", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return {"content": f"Agent says: {content[-20:]}",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    return model


def wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def main() -> None:
    import app.adapters.cagent_adapter as adapter_module
    original_init = adapter_module.CAgentAdapter.__init__

    def init_with_script(self, *args, **kwargs):
        kwargs["model_callable"] = model_factory()
        original_init(self, *args, **kwargs)

    adapter_module.CAgentAdapter.__init__ = init_with_script

    from fastapi.testclient import TestClient
    from app.main import app
    from app.plugins.host import get_plugin_host

    data_dir = ROOT / "home" / "plugin_data" / "wechat"

    def saved_state():
        return json.loads((data_dir / "state.json").read_text(encoding="utf-8"))

    with TestClient(app) as client:
        host = get_plugin_host()

        def api(method, path, **kw):
            return getattr(client, method)(f"/api/v1{path}", **kw)

        def ui(sid=None):
            return host.services.ui_state("wechat", sid)

        def bridge():
            return host.services.get("wechat", "bridge", None)

        def bot():
            return FakeBot.instances[-1]

        def deliver(user, text):
            client.portal.call(bot().deliver, user, text)

        def messages(sid):
            return api("get", f"/sessions/{sid}/messages").json()

        def user_rows(sid):
            return [m for m in messages(sid) if m["role"] == "user"]

        def actions(card):
            return [a["id"] for a in card.actions] if card else None

        section("loading")
        info = api("get", "/plugins/wechat").json()
        check(info["state"] == "loaded", f"plugin loads from AUGENTIA_PLUGIN_PATH: {info['state']} {info['error']}")
        caps = info["capabilities"]
        check([t["full_name"] for t in caps["tools"]] == ["wechat__send"] and not caps["hooks"]
              and caps["services"][0]["name"] == "bridge" and caps["services"][0]["scope"] == "global",
              f"capabilities: one tool, no hooks, a global bridge: {caps}")
        check(set(caps["actions"]) == {"connect", "disconnect", "set_inbound", "clear_inbound"},
              f"actions: {caps['actions']}")
        check(wait_for(lambda: bridge() is not None and bridge().status not in ("starting", "running"))
              and not FakeBot.instances,
              f"never connected before: no login at startup ({bridge()})")
        check(actions(ui()) == ["connect"], f"plugin page card offers Connect: {ui()}")

        section("connect from a session card")
        agent = api("post", "/agents", json={
            "name": "wx", "description": "", "agent_type": "tool_use", "system_prompt": "",
            "tool_names": ["wechat__send"], "openai_model": "m", "config": {}}).json()
        sid_a = api("post", "/sessions", json={"agent_id": agent["id"], "title": "Alpha"}).json()["id"]
        sid_b = api("post", "/sessions", json={"agent_id": agent["id"], "title": "Beta"}).json()["id"]
        check(ui(sid_a) is None, "no card while the plugin is off for the session")
        api("put", f"/sessions/{sid_a}/plugins/wechat", json={"enabled": True})
        check(actions(ui(sid_a)) == ["connect", "set_inbound"], f"session card: {ui(sid_a)}")

        r = api("post", "/plugins/wechat/actions/connect", json={"session_id": sid_a})
        check(r.status_code == 200, f"connect action: {r.text}")
        check(wait_for(lambda: ui(sid_a) and ui(sid_a).qr), "QR code shown on the session card")
        check(ui().qr and ui().qr.endswith("/qr/123"), "and on the plugin page")
        check(Path(bot().cred_path) == data_dir / "wechat_credentials.json",
              f"one account-level credential file: {bot().cred_path}")
        client.portal.call(bot().allow_login.set)
        check(wait_for(lambda: ui(sid_a).status == "ok"), "after scanning: connected")
        check(actions(ui(sid_a)) == ["disconnect", "clear_inbound"] and "receives WeChat" in ui(sid_a).message,
              f"connecting from a session made it the inbound session: {ui(sid_a)}")
        state = saved_state()
        check(state["auto_connect"] is True and state["inbound"]["id"] == sid_a, f"state.json: {state}")

        section("WeChat -> inbound session; replies only through the tool")
        deliver("wx-me", "hello there")
        check(wait_for(lambda: any(m["role"] == "agent" for m in messages(sid_a))), "the agent answered in the session")
        time.sleep(0.3)
        check(bot().sent == [], f"the plain answer is not forwarded: {bot().sent}")
        row = user_rows(sid_a)[0]
        check(row["content"] == "[from WeChat; reply with the wechat__send tool] hello there"
              and row["from_session_id"] == "plugin:wechat" and row["metadata"]["plugin_label"] == "wechat:wx-me",
              f"stored user message: {row}")
        deliver("wx-me", "please reply")
        check(wait_for(lambda: ("wx-me", "hi from the agent") in bot().sent),
              f"wechat__send reaches WeChat: {bot().sent}")
        time.sleep(0.3)
        check(bot().sent == [("wx-me", "hi from the agent")], f"only the tool's text: {bot().sent}")

        section("commands")
        bot().sent.clear()
        n = len(messages(sid_a))
        deliver("wx-me", "\\cmd status")
        check(wait_for(lambda: bot().sent) and "Alpha" in bot().sent[0][1],
              f"status names the inbound session: {bot().sent}")
        deliver("wx-me", "\\cmd help")
        check(wait_for(lambda: len(bot().sent) == 2) and "bind" in bot().sent[1][1], "help")
        time.sleep(0.2)
        check(len(messages(sid_a)) == n, "commands do not reach the agent")

        section("\\cmd bind: inbound + mirror")
        bot().sent.clear()
        deliver("wx-me", "\\cmd bind")
        check(wait_for(lambda: bot().sent) and sid_b[:8] in bot().sent[0][1],
              f"bind without argument lists sessions: {bot().sent}")
        bot().sent.clear()
        deliver("wx-me", "\\cmd bind nope-nothing")
        check(wait_for(lambda: bot().sent) and "No session matches" in bot().sent[0][1], "unknown session")
        bot().sent.clear()
        deliver("wx-me", "\\cmd bind beta")
        check(wait_for(lambda: bot().sent) and "Bound to \"Beta\"" in bot().sent[0][1], f"bind by title: {bot().sent}")
        check("Bound from WeChat" in ui(sid_a).message, "cards show the binding")
        bot().sent.clear()
        a_before = len(user_rows(sid_a))
        deliver("wx-me", "use a tool")
        check(wait_for(lambda: ("wx-me", "Done.") in bot().sent), f"bound session's final answer mirrored: {bot().sent}")
        check(("wx-me", "Let me check.") in bot().sent and ("wx-me", "tool text") in bot().sent,
              f"text before tool calls and the tool's message also arrive: {bot().sent}")
        check(len(user_rows(sid_b)) == 1 and len(user_rows(sid_a)) == a_before, "the message went to the bound session")

        bot().sent.clear()
        r = client.post(f"/api/v1/sessions/{sid_b}/messages", json={"content": "typed in the console"})
        check(r.status_code == 202 and wait_for(lambda: bot().sent), f"mirror also forwards console turns: {bot().sent}")

        time.sleep(0.3)
        bot().sent.clear()
        deliver("wx-me", "\\cmd unbind")
        check(wait_for(lambda: bot().sent) and "Unbound" in bot().sent[0][1] and "Alpha" in bot().sent[0][1],
              f"unbind restores the inbound session: {bot().sent}")
        bot().sent.clear()
        r = client.post(f"/api/v1/sessions/{sid_b}/messages", json={"content": "not mirrored now"})
        check(r.status_code == 202, "console turn after unbind")
        time.sleep(0.5)
        check(bot().sent == [], f"no mirror after unbind: {bot().sent}")
        deliver("wx-me", "back to alpha")
        check(wait_for(lambda: len(user_rows(sid_a)) == a_before + 1), "messages go to the inbound session again")

        section("changing the inbound session")
        api("put", f"/sessions/{sid_b}/plugins/wechat", json={"enabled": True})
        check(actions(ui(sid_b)) == ["disconnect", "set_inbound"], f"second session card: {ui(sid_b)}")
        r = api("post", "/plugins/wechat/actions/set_inbound", json={"session_id": sid_b})
        check(r.status_code == 200 and actions(ui(sid_b))[1] == "clear_inbound"
              and actions(ui(sid_a))[1] == "set_inbound", "set_inbound moves the role")
        api("post", "/plugins/wechat/actions/clear_inbound", json={"session_id": sid_b})
        bot().sent.clear()
        deliver("wx-me", "anyone?")
        check(wait_for(lambda: bot().sent) and "No Augentia session receives" in bot().sent[0][1],
              f"no inbound session: WeChat is told so: {bot().sent}")
        api("post", "/plugins/wechat/actions/set_inbound", json={"session_id": sid_b})
        api("delete", f"/sessions/{sid_b}")
        check(wait_for(lambda: saved_state()["inbound"] is None), "deleting the inbound session clears it")

        section("disconnect / reconnect")
        current = bot()
        r = api("post", "/plugins/wechat/actions/disconnect", json={})
        check(r.status_code == 200 and current.stopped.is_set() and actions(ui()) == ["connect"],
              "disconnect from the plugin page")
        check(saved_state()["auto_connect"] is False, "auto_connect off")
        api("post", "/plugins/wechat/actions/connect", json={})
        check(wait_for(lambda: ui().status == "ok") and bot() is not current, "reconnect with saved credentials (no QR)")

        section("tool without a connection")
        api("post", "/plugins/wechat/actions/disconnect", json={})
        client.post(f"/api/v1/sessions/{sid_a}/messages", json={"content": "please reply"})
        check(wait_for(lambda: any(m["role"] == "tool" and "not connected" in m["content"] for m in messages(sid_a))),
              "the tool explains WeChat is not connected")

    section("restart: reconnects on its own")
    # A real restart is a new process; here the session runners of the first
    # app (bound to its closed event loop) must go.
    from app.adapters.registry import get_registry
    get_registry()._runners.clear()
    FakeBot.instances.clear()
    (data_dir / "state.json").write_text(json.dumps({
        "auto_connect": True, "inbound": {"id": sid_a, "title": "Alpha"}, "contact": "wx-me"}), encoding="utf-8")
    with TestClient(app) as client:
        host = get_plugin_host()

        def users():
            return [m for m in client.get(f"/api/v1/sessions/{sid_a}/messages").json() if m["role"] == "user"]

        check(wait_for(lambda: host.services.ui_state("wechat", None) is not None
                       and host.services.ui_state("wechat", None).status == "ok"),
              "auto_connect: logged in at startup without a click")
        n = len(users())
        client.portal.call(FakeBot.instances[-1].deliver, "wx-me", "after restart")
        check(wait_for(lambda: len(users()) == n + 1), "saved inbound session still receives")


main()
print()
if failures:
    fail(f"{failures} check(s) failed")
    sys.exit(1)
ok("all WeChat plugin checks passed")
