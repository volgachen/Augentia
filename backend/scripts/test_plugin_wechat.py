"""WeChat bridge plugin end to end, with a fake wechatbot package.

Covers several WeChat accounts at once: QR login per account (scanned as a
given WeChat id) named through the card input, compact cards (one row per
account), one inbound session per account (and per session), ``\\cmd bind`` / ``\\cmd unbind`` per account,
replies only through wechat__send via the session's account, the duplicate
account guard, migration of the 0.3 single account, and auto-reconnect
after a restart.

Loads backend/plugins/wechat through AUGENTIA_PLUGIN_PATH. A fake
``wechatbot.WeChatBot`` (installed into sys.modules and as the
``wechatbot-sdk`` distribution so the dependency check passes) lets the test
play WeChat. The sessions run the real CAgent engine with a scripted model.

Run from backend/:  python scripts/test_plugin_wechat.py
"""
import asyncio
import importlib.metadata
import json
import sys
import time
import types
from datetime import datetime
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


class FakeCreds:
    def __init__(self, account_id):
        self.account_id = account_id
        self.user_id = account_id


class FakeBot:
    instances: list["FakeBot"] = []
    # The WeChat id the next QR scan logs in as (set by the test).
    scan_as = "wx-bot-1"

    def __init__(self, *, cred_path, on_qr_url, on_scanned, on_expired, on_error):
        self.cred_path = cred_path
        self.callbacks = dict(qr=on_qr_url, scanned=on_scanned, expired=on_expired, error=on_error)
        self.handler = None
        self.sent: list[tuple[str, str]] = []
        self.media: list[tuple[str, dict]] = []
        self.allow_login = asyncio.Event()
        self.stopped = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.account_id = None
        FakeBot.instances.append(self)

    def on_message(self, fn):
        self.handler = fn
        return fn

    async def login(self):
        path = Path(self.cred_path)
        if not path.exists():
            self.callbacks["qr"](f"https://login.weixin.example/qr/{len(FakeBot.instances)}")
            await self.allow_login.wait()
            self.callbacks["scanned"]()
            path.write_text(json.dumps({"account_id": FakeBot.scan_as}))
        self.account_id = json.loads(path.read_text()).get("account_id")
        return FakeCreds(self.account_id)

    async def start(self):
        # Like wechatbot-sdk: a cancelled long-poll returns normally.
        try:
            await self.stopped.wait()
        except asyncio.CancelledError:
            pass

    def stop(self):
        self.loop.call_soon_threadsafe(self.stopped.set)

    async def send(self, user_id, text):
        self.sent.append((user_id, text))

    async def send_media(self, user_id, content):
        # Like wechatbot-sdk: bytes plus file_name (and an optional caption).
        assert isinstance(content["file"], bytes)
        self.media.append((user_id, content))

    async def deliver(self, user_id, text):
        await self.handler(FakeMsg(user_id, text))


fake_module = types.ModuleType("wechatbot")
fake_module.WeChatBot = FakeBot
sys.modules["wechatbot"] = fake_module
_real_distribution = importlib.metadata.distribution


def _distribution(name):
    if name == "wechatbot-sdk":
        return object()
    return _real_distribution(name)


importlib.metadata.distribution = _distribution


# ---- scripted model ----

def _call(text):
    return {"id": f"t{time.monotonic_ns()}", "name": "wechat__send", "arguments": {"text": text}}


def _file_call(path, caption=None):
    args = {"path": path}
    if caption:
        args["caption"] = caption
    return {"id": f"f{time.monotonic_ns()}", "name": "wechat__send_file", "arguments": args}


def model_factory():
    async def model(*, messages, tools, tool_choice=None):
        last = messages[-1]
        names = [t["name"] for t in tools]
        content = str(last["content"])
        # "send file <path> [| caption]" -> wechat__send_file
        if last["role"] == "user" and "send file " in content and "wechat__send_file" in names:
            spec = content.split("send file ", 1)[1]
            path, _, caption = spec.partition(" | ")
            return {"content": "", "tool_calls": [_file_call(path.strip(), caption.strip() or None)]}
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
    data_dir.mkdir(parents=True, exist_ok=True)

    # A 0.3 single account on disk: migrated into the first account.
    (data_dir / "wechat_credentials.json").write_text(json.dumps({"account_id": "wx-bot-1"}))
    (data_dir / "state.json").write_text(json.dumps({"auto_connect": True, "inbound": None, "contact": "wx-me"}))

    def saved():
        return json.loads((data_dir / "accounts.json").read_text(encoding="utf-8"))["accounts"]

    with TestClient(app) as client:
        host = get_plugin_host()

        def api(method, path, **kw):
            return getattr(client, method)(f"/api/v1{path}", **kw)

        def ui(sid=None):
            return host.services.ui_state("wechat", sid)

        def act(action_id, sid=None, **data):
            return api("post", f"/plugins/wechat/actions/{action_id}", json={"session_id": sid, "data": data})

        def bot_of(wechat_id):
            live = [b for b in FakeBot.instances if b.account_id == wechat_id and not b.stopped.is_set()]
            return live[-1] if live else None

        def deliver(wechat_id, user, text):
            client.portal.call(bot_of(wechat_id).deliver, user, text)

        def messages(sid):
            return api("get", f"/sessions/{sid}/messages").json()

        def user_rows(sid):
            return [m for m in messages(sid) if m["role"] == "user"]

        def sec(card, title):
            return next((s for s in card.sections if s["title"] == title), None) if card else None

        def ids(block):
            return [a["id"] for a in block["actions"]] if block else None

        section("loading and migration")
        info = api("get", "/plugins/wechat").json()
        check(info["state"] == "loaded", f"plugin loads: {info['state']} {info['error']}")
        caps = info["capabilities"]
        check(sorted(t["full_name"] for t in caps["tools"]) == ["wechat__send", "wechat__send_file"]
              and not caps["hooks"] and caps["services"][0]["scope"] == "global",
              "two tools (send, send_file), no hooks, a global bridge")
        check(set(caps["actions"]) == {"add_account", "remove_account", "set_inbound", "unbind"},
              f"actions: {caps['actions']}")
        accounts = saved()
        check(len(accounts) == 1 and not (data_dir / "wechat_credentials.json").exists()
              and (data_dir / "state.json.migrated").exists(), f"0.3 account migrated: {accounts}")
        key1 = next(iter(accounts))
        check((data_dir / "accounts" / key1 / "credentials.json").exists() and accounts[key1]["contact"] == "wx-me",
              "credentials and contact moved")
        check(wait_for(lambda: bot_of("wx-bot-1") is not None), "every added account connects at startup")
        check(wait_for(lambda: saved()[key1]["wechat_id"] == "wx-bot-1"), "WeChat id recorded at login")
        W = "WeChat wx-bot-1"
        check(wait_for(lambda: ui().sections and ui().sections[0]["title"] == W), f"unnamed: titled by WeChat id: {ui()}")
        row = ui().sections[0]
        check(ids(row) == ["remove_account"] and row["message"] is None,
              f"page row when connected and unbound: just the name and Remove: {row}")
        add = ui().actions[0]
        check(add["id"] == "add_account" and add["input"]["name"] == "alias", f"add button takes a name: {add}")

        section("second account from a session card, named")
        agent = api("post", "/agents", json={
            "name": "wx", "description": "", "agent_type": "tool_use", "system_prompt": "",
            "tool_names": ["wechat__send"], "openai_model": "m", "config": {}}).json()
        sid_a = api("post", "/sessions", json={"agent_id": agent["id"], "title": "Alpha"}).json()["id"]
        sid_b = api("post", "/sessions", json={"agent_id": agent["id"], "title": "Beta"}).json()["id"]
        sid_c = api("post", "/sessions", json={"agent_id": agent["id"], "title": "Gamma"}).json()["id"]
        for sid in (sid_a, sid_b, sid_c):
            api("put", f"/sessions/{sid}/plugins/wechat", json={"enabled": True})
        check(ids(sec(ui(sid_a), W)) == ["set_inbound"] and sec(ui(sid_a), W)["actions"][0]["label"] == "Bind",
              f"unbound session lists accounts with Bind: {ui(sid_a)}")

        FakeBot.scan_as = "wx-bot-2"
        r = act("add_account", sid_b, alias="Home")
        key2 = r.json()["result"]
        check(r.status_code == 200 and wait_for(lambda: any(s.get("qr") for s in ui(sid_b).sections)),
              "add_account: the QR code shows under the new account's row")
        check(sec(ui(sid_b), "Home") is not None and len(ui(sid_b).sections) == 1,
              f"Beta is bound to the new account: its card shows only that one: {ui(sid_b)}")
        client.portal.call(FakeBot.instances[-1].allow_login.set)
        check(wait_for(lambda: saved()[key2]["wechat_id"] == "wx-bot-2"), "second account logged in")
        check(saved()[key2]["alias"] == "Home" and saved()[key2]["inbound"]["id"] == sid_b, "named Home, bound to Beta")
        check(wait_for(lambda: ui(sid_b).sections[0]["message"] is None) and ids(ui(sid_b).sections[0]) == ["unbind"],
              f"bound session card: Home with Unbind only: {ui(sid_b)}")
        home_on_a = sec(ui(sid_a), "Home")
        check(home_on_a["actions"][0].get("disabled") and "Beta" in home_on_a["actions"][0]["label"],
              f"Alpha sees Home as not bindable: {home_on_a}")
        page_home = sec(ui(), "Home")
        check(ids(page_home) == ["unbind", "remove_account"] and "Beta" in page_home["message"],
              f"page row: name, bound session, Unbind, Remove: {page_home}")

        section("one session per account, one account per session")
        r = act("set_inbound", sid_b, account=key1)
        check(r.status_code == 422 and "already receives" in r.text, f"Beta already has Home: {r.text}")
        check(act("set_inbound", sid_a, account=key1).status_code == 200, "Alpha binds the first account")
        check(saved()[key1]["inbound"]["id"] == sid_a and len(ui(sid_a).sections) == 1, "saved; Alpha shows only it")
        gamma = ui(sid_c).sections
        check(len(gamma) == 2 and all(s["actions"][0].get("disabled") for s in gamma),
              f"Gamma: both accounts taken: {gamma}")

        section("routing per account; replies only via the session's account")
        deliver("wx-bot-1", "wx-me", "hello work")
        deliver("wx-bot-2", "wx-mom", "hello home")
        check(wait_for(lambda: len(user_rows(sid_a)) == 1 and len(user_rows(sid_b)) == 1),
              "each account's message lands in its own session")
        check(user_rows(sid_a)[0]["metadata"]["plugin_label"] == "wechat:wx-me", "label carries the contact")

        section("message_prefix placeholders")
        r = api("put", "/plugins/wechat/config", json={"config": {
            "message_prefix": "[WeChat {time} {weekday} from {contact} via {account} {nope}] "}})
        check(r.status_code == 200, f"set the prefix: {r.text}")
        n_a = len(user_rows(sid_a))
        before = datetime.now()
        deliver("wx-bot-1", "wx-me", "what time is it")
        check(wait_for(lambda: len(user_rows(sid_a)) == n_a + 1), "delivered")
        content = user_rows(sid_a)[-1]["content"]
        after = datetime.now()
        stamps = {f"{t:%Y-%m-%d %H:%M} {t:%A}" for t in (before, after)}
        check(any(content == f"[WeChat {s} from wx-me via {W} {{nope}}] what time is it" for s in stamps),
              f"placeholders filled, unknown kept: {content!r}")
        api("put", "/plugins/wechat/config", json={"config": {}})
        time.sleep(0.3)
        check(not bot_of("wx-bot-1").sent and not bot_of("wx-bot-2").sent, "plain answers are not forwarded")
        deliver("wx-bot-2", "wx-mom", "please reply")
        check(wait_for(lambda: ("wx-mom", "hi from the agent") in bot_of("wx-bot-2").sent),
              f"Beta's wechat__send goes out through Home: {bot_of('wx-bot-2').sent}")
        check(not bot_of("wx-bot-1").sent, "nothing through the first account")
        client.post(f"/api/v1/sessions/{sid_c}/messages", json={"content": "please reply"})
        check(wait_for(lambda: any(m["role"] == "tool" and "not bound to a WeChat account" in m["content"]
                                   for m in messages(sid_c))), "unbound session: the tool says so")

        section("wechat__send_file")
        workdir = ROOT / "wx-workdir"
        (workdir / "out").mkdir(parents=True)
        png = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10]) + b"0" * 32
        (workdir / "out" / "chart.png").write_bytes(png)
        (workdir / "report.pdf").write_bytes(b"%PDF-1.4 report")
        (workdir / "empty.txt").write_bytes(b"")
        (ROOT / "secret.txt").write_text("outside the working dir")
        check(any(t["name"] == "wechat__send_file" for t in api("get", "/tools").json()), "send_file is listed")
        agent_f = api("post", "/agents", json={
            "name": "wxf", "description": "", "agent_type": "tool_use", "system_prompt": "",
            "tool_names": ["wechat__send", "wechat__send_file"], "openai_model": "m", "config": {}}).json()
        sid_f = api("post", "/sessions", json={"agent_id": agent_f["id"], "title": "Files",
                                                "source_dir": str(workdir)}).json()["id"]
        api("put", f"/sessions/{sid_f}/plugins/wechat", json={"enabled": True})

        def file_result(sid, n_before):
            rows = [m for m in messages(sid) if m["role"] == "tool"]
            return json.loads(rows[-1]["content"])["content"] if len(rows) > n_before else None

        def run_file(sid, line):
            n = len([m for m in messages(sid) if m["role"] == "tool"])
            client.post(f"/api/v1/sessions/{sid}/messages", json={"content": line})
            check(wait_for(lambda: file_result(sid, n) is not None), f"tool ran for {line!r}")
            return file_result(sid, n)

        result = run_file(sid_f, "send file out/chart.png")
        check("not bound to a WeChat account" in result, f"unbound session can't send files: {result}")

        act("set_inbound", sid_f, account=key2)
        home = bot_of("wx-bot-2")
        home.media.clear()
        result = run_file(sid_f, "send file out/chart.png | the chart")
        check(result.startswith("Sent chart.png"), f"image sent: {result}")
        check(home.media and home.media[-1][0] == "wx-mom" and home.media[-1][1] == {
            "file": png, "file_name": "chart.png", "caption": "the chart"},
            f"through the session's account to its contact, bytes + name + caption: {home.media[-1:]}")
        result = run_file(sid_f, f"send file {workdir / 'report.pdf'}")
        check(result.startswith("Sent report.pdf") and home.media[-1][1]["file_name"] == "report.pdf"
              and "caption" not in home.media[-1][1], f"absolute path inside the dir, no caption: {result}")

        n_media = len(home.media)
        for line, expect in [("send file ../secret.txt", "outside this session's working directory"),
                             (f"send file {ROOT / 'secret.txt'}", "outside this session's working directory"),
                             ("send file missing.png", "does not exist"),
                             ("send file out", "is not a file"),
                             ("send file empty.txt", "is empty")]:
            result = run_file(sid_f, line)
            check(expect in result, f"{line!r} -> {result}")
        check(len(home.media) == n_media, "nothing sent for rejected paths")
        act("unbind", sid_f, account=key2)
        act("set_inbound", sid_b, account=key2)

        section("\\cmd bind per account")
        w = bot_of("wx-bot-1")
        alpha_rows = len(user_rows(sid_a))
        w.sent.clear()
        deliver("wx-bot-1", "wx-me", "\\cmd bind")
        check(wait_for(lambda: w.sent) and "(uses Home)" in w.sent[0][1], f"listing shows taken sessions: {w.sent}")
        w.sent.clear()
        deliver("wx-bot-1", "wx-me", "\\cmd bind beta")
        check(wait_for(lambda: w.sent) and "already uses WeChat account Home" in w.sent[0][1],
              f"can't bind a session another account receives: {w.sent}")
        w.sent.clear()
        deliver("wx-bot-1", "wx-me", "\\cmd bind gamma")
        check(wait_for(lambda: w.sent) and "Bound to \"Gamma\"" in w.sent[0][1], f"bind to Gamma: {w.sent}")
        check("Gamma" in sec(ui(), W)["message"] and len(ui(sid_c).sections) == 1,
              "page row and Gamma's card follow the binding")
        w.sent.clear()
        deliver("wx-bot-1", "wx-me", "use a tool")
        check(wait_for(lambda: ("wx-me", "Done.") in w.sent), f"Gamma mirrored: {w.sent}")
        check(("wx-me", "Let me check.") in w.sent and ("wx-me", "tool text") in w.sent,
              "text before tool calls and the tool's message arrive too")
        check(len(user_rows(sid_a)) == alpha_rows, "Alpha did not get it while bound")
        check(not [m for m in bot_of("wx-bot-2").sent if m[1] in ("Done.", "tool text")], "Home unaffected")
        w.sent.clear()
        deliver("wx-bot-1", "wx-me", "\\cmd unbind")
        check(wait_for(lambda: w.sent) and "Alpha" in w.sent[0][1], f"unbind back to Alpha: {w.sent}")

        section("Unbind button")
        check(act("unbind", sid_a, account=key1).status_code == 200 and saved()[key1]["inbound"] is None,
              "Unbind clears the saved session")
        check(ids(sec(ui(sid_a), W)) == ["set_inbound"] and len(ui(sid_a).sections) == 2,
              "Alpha lists the accounts again")
        w.sent.clear()
        deliver("wx-bot-1", "wx-me", "anyone?")
        check(wait_for(lambda: w.sent) and "No Augentia session receives" in w.sent[0][1], f"told so: {w.sent}")
        act("set_inbound", sid_a, account=key1)

        section("duplicate account")
        FakeBot.scan_as = "wx-bot-2"
        n_accounts, n_bots = len(saved()), len(FakeBot.instances)
        act("add_account")
        check(wait_for(lambda: len(FakeBot.instances) > n_bots and len(saved()) == n_accounts + 1),
              "a new entry waits for its QR scan")
        client.portal.call(FakeBot.instances[-1].allow_login.set)
        check(wait_for(lambda: len(saved()) == n_accounts and "already added as Home" in (ui().message or "")),
              f"scanning an added account again drops the new entry: {ui().message}")

        section("deleting a bound session")
        api("delete", f"/sessions/{sid_b}")
        check(wait_for(lambda: saved()[key2]["inbound"] is None), "deleting Beta unbinds Home")

        bots_at_shutdown = len(FakeBot.instances)

    section("shutdown does not reconnect")
    check(len(FakeBot.instances) == bots_at_shutdown,
          f"no login while shutting down: {len(FakeBot.instances) - bots_at_shutdown} new bot(s)")

    section("restart: every account reconnects")
    from app.adapters.registry import get_registry
    get_registry()._runners.clear()  # runners of the first app's event loop
    FakeBot.instances.clear()
    with TestClient(app) as client:
        host = get_plugin_host()
        check(wait_for(lambda: {b.account_id for b in FakeBot.instances} == {"wx-bot-1", "wx-bot-2"}),
              "both accounts logged in at startup")
        users = lambda: [m for m in client.get(f"/api/v1/sessions/{sid_a}/messages").json() if m["role"] == "user"]
        n = len(users())
        work = next(b for b in FakeBot.instances if b.account_id == "wx-bot-1")
        check(wait_for(lambda: work.handler is not None), "handler registered")
        client.portal.call(work.deliver, "wx-me", "after restart")
        check(wait_for(lambda: len(users()) == n + 1), "Work still sends to Alpha")
        n_bots = len(FakeBot.instances)
        r = client.post(f"/api/v1/plugins/wechat/actions/remove_account", json={"data": {"account": key2}})
        check(len(FakeBot.instances) == n_bots, "Remove does not log the account in again")
        check(r.status_code == 200 and key2 not in saved() and not (data_dir / "accounts" / key2).exists(),
              "remove deletes the account and its credentials")


main()
print()
if failures:
    fail(f"{failures} check(s) failed")
    sys.exit(1)
ok("all WeChat plugin checks passed")
