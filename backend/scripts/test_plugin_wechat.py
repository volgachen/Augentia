"""WeChat bridge plugin (P5) end to end, with a fake wechatbot package.

Loads backend/plugins/wechat through AUGENTIA_PLUGIN_PATH. A fake
``wechatbot.WeChatBot`` (installed into sys.modules and as a distribution
so the dependency check passes) lets the test play WeChat: trigger the QR
callbacks, deliver incoming messages and read what the plugin sent back.
The session runs the real CAgent engine with a scripted model.

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
        self.logged_in = asyncio.Event()
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
        self.logged_in.set()

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

def model_factory():
    async def model(*, messages, tools, tool_choice=None):
        last = messages[-1]
        names = [t["name"] for t in tools]
        if last["role"] == "user" and "progress please" in str(last["content"]) and "wechat__send" in names:
            return {"content": "Working on it.", "tool_calls": [
                {"id": f"t{time.monotonic_ns()}", "name": "wechat__send", "arguments": {"text": "50% done"}}]}
        if last["role"] == "tool":
            return {"content": "All done.", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return {"content": f"Agent says: {str(last['content'])[-30:]}",
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

    with TestClient(app) as client:
        host = get_plugin_host()

        def api(method, path, **kw):
            return getattr(client, method)(f"/api/v1{path}", **kw)

        def ui(sid):
            return host.services.ui_state("wechat", sid)

        def bridge(sid):
            return host.services.get("wechat", "bridge", sid)

        def bot():
            return FakeBot.instances[-1]

        def portal(coro):
            return client.portal.call(lambda: coro)

        section("loading")
        info = api("get", "/plugins/wechat").json()
        check(info["state"] == "loaded", f"plugin loads from AUGENTIA_PLUGIN_PATH: {info['state']} {info['error']}")
        caps = info["capabilities"]
        check([t["full_name"] for t in caps["tools"]] == ["wechat__send"]
              and {h["event"] for h in caps["hooks"]} == {"message.generated", "turn.end"}
              and caps["services"][0]["name"] == "bridge" and caps["actions"] == ["connect", "disconnect"],
              "capabilities")
        legacy = api("get", "/plugins/wechat-bridge")
        check(legacy.status_code == 404, "the v1 wechat-bridge directory is gone")

        section("enable and connect")
        agent = api("post", "/agents", json={
            "name": "wx", "description": "", "agent_type": "tool_use", "system_prompt": "",
            "tool_names": ["wechat__send"], "openai_model": "m", "config": {}}).json()
        sid = api("post", "/sessions", json={"agent_id": agent["id"]}).json()["id"]
        check(ui(sid) is None, "no card while the plugin is off for the session")
        api("put", f"/sessions/{sid}/plugins/wechat", json={"enabled": True})
        card = ui(sid)
        check(card and card.status == "idle" and [a["id"] for a in card.actions] == ["connect"],
              f"enabling shows a Connect button: {card}")
        check(bridge(sid) is None, "bridge not started automatically (login needs the user)")

        r = api("post", "/plugins/wechat/actions/connect", json={"session_id": sid})
        check(r.status_code == 200, "connect action")
        check(wait_for(lambda: ui(sid) and ui(sid).qr), "QR code card shown")
        card = ui(sid)
        check(card.status == "waiting" and card.qr.endswith("/qr/123")
              and [a["id"] for a in card.actions] == ["disconnect"], f"QR card: {card}")
        cred = Path(bot().cred_path)
        check(cred.parent == ROOT / "home" / "plugin_data" / "wechat" / "sessions" / sid,
              f"credentials stored per session: {cred}")
        portal(asyncio.sleep(0))
        client.portal.call(bot().allow_login.set)
        check(wait_for(lambda: ui(sid).status == "ok"), "after scanning: connected card")
        check("Waiting for the first WeChat message" in (ui(sid).message or ""), ui(sid).message)

        section("WeChat -> agent -> WeChat")
        client.portal.call(bot().deliver, "wx-alice", "hello there")
        check(wait_for(lambda: bot().sent), "agent reply forwarded")
        check(bot().sent[0] == ("wx-alice", "Agent says: [send from wechat] hello there"),
              f"reply text and contact: {bot().sent[0]}")
        msgs = api("get", f"/sessions/{sid}/messages").json()
        user = next(m for m in msgs if m["role"] == "user")
        check(user["content"] == "[send from wechat] hello there" and user["from_session_id"] == "plugin:wechat"
              and user["metadata"]["plugin_label"] == "wechat:wx-alice", f"stored user message: {user}")
        check("wx-alice" in (ui(sid).message or ""), "card names the contact")

        bot().sent.clear()
        client.portal.call(bot().deliver, "wx-alice", "\\cmd status")
        check(wait_for(lambda: bot().sent) and "Connected to Augentia session" in bot().sent[0][1],
              f"\\cmd status answered in WeChat: {bot().sent}")
        n = len(api("get", f"/sessions/{sid}/messages").json())
        client.portal.call(bot().deliver, "wx-alice", "\\cmd help")
        time.sleep(0.2)
        check(len(api("get", f"/sessions/{sid}/messages").json()) == n, "commands do not reach the agent")

        section("wechat__send tool and reply_mode")
        bot().sent.clear()
        client.portal.call(bot().deliver, "wx-bob", "progress please")
        check(wait_for(lambda: len(bot().sent) >= 2), f"tool message + final answer: {bot().sent}")
        check(bot().sent[0] == ("wx-bob", "50% done") and bot().sent[-1] == ("wx-bob", "All done."),
              "tool sends to the latest contact; final answer follows")
        check(("wx-bob", "Working on it.") not in bot().sent, "final_only: intermediate text not forwarded")
        api("put", f"/sessions/{sid}/plugins/wechat", json={"config": {"reply_mode": "every_reply"}})
        bot().sent.clear()
        client.portal.call(bot().deliver, "wx-bob", "progress please")
        check(wait_for(lambda: ("wx-bob", "All done.") in bot().sent) and ("wx-bob", "Working on it.") in bot().sent,
              f"every_reply forwards intermediate text: {bot().sent}")

        section("runner rebuild keeps the connection")
        current = bot()
        api("put", f"/sessions/{sid}/model", json={"model": "other"})
        check(bridge(sid).status == "running" and bot() is current, "model change does not reconnect")
        current.sent.clear()
        client.portal.call(current.deliver, "wx-alice", "still there?")
        check(wait_for(lambda: current.sent), "messages still flow after the runner rebuild")

        section("disconnect / reconnect / switch off")
        r = api("post", "/plugins/wechat/actions/disconnect", json={"session_id": sid})
        check(r.status_code == 200 and bridge(sid).status == "stopped" and current.stopped.is_set(),
              "disconnect stops the bot")
        card = ui(sid)
        check(card.status == "idle" and [a["id"] for a in card.actions] == ["connect"], "back to Connect")
        msgs_before = len(api("get", f"/sessions/{sid}/messages").json())
        result = client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "hi from the console"})
        check(result.status_code == 202, "a console turn while disconnected")
        check(wait_for(lambda: len(api("get", f"/sessions/{sid}/messages").json()) > msgs_before + 1),
              "turn completes without a bot")
        api("post", "/plugins/wechat/actions/connect", json={"session_id": sid})
        check(wait_for(lambda: ui(sid).status == "ok") and bot() is not current,
              "reconnect uses saved credentials (no QR)")
        api("put", f"/sessions/{sid}/plugins/wechat", json={"enabled": False})
        check(bridge(sid).status == "stopped" and ui(sid) is None, "switching off stops the bridge and clears the card")

        section("tool without a connection")
        sid2 = api("post", "/sessions", json={"agent_id": agent["id"]}).json()["id"]
        client.post(f"/api/v1/sessions/{sid2}/messages", json={"content": "progress please"})
        check(wait_for(lambda: any(m["role"] == "tool" for m in api("get", f"/sessions/{sid2}/messages").json())),
              "tool ran")
        tool_row = next(m for m in api("get", f"/sessions/{sid2}/messages").json() if m["role"] == "tool")
        check("not connected" in json.loads(tool_row["content"])["content"], "tool explains it is not connected")


main()
print()
if failures:
    fail(f"{failures} check(s) failed")
    sys.exit(1)
ok("all WeChat plugin checks passed")
