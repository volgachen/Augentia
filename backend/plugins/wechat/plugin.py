"""WeChat bridge for Augentia (plugin API v1, see backend/app/plugins/README.md).

Several WeChat accounts can be logged in at once. Each account has:

- an **inbound session**: its WeChat messages are sent into it. Set from a
  session's plugin card; persisted. One account has at most one inbound
  session, and a session receives from at most one account.
- an optional **mirror session**, only set by ``\\cmd bind``: everything the
  agent writes there (every assistant message, including text before tool
  calls) is forwarded to that account's WeChat.

``\\cmd bind <session>`` (sent from an account's WeChat) temporarily makes one
session both inbound and mirror for that account; ``\\cmd unbind`` goes back
to the saved inbound session and no mirror. Bindings are in memory only.

Outside the mirror the agent answers with the ``wechat__send`` tool, which
sends through the account the session is bound to (bind first, else saved
inbound) to that account's last contact. A session bound to no account gets
an error from the tool.

The global ``bridge`` service runs one task per connected account (each with
its own reconnect backoff); stopping the service disconnects them all.

Files in ``ctx.data_dir``: ``accounts.json`` (per account: alias, wechat_id,
auto_connect, inbound, contact) and ``accounts/<key>/credentials.json``
(the WeChatBot login). ``key`` is a local id: the WeChat id is only known
after the QR login. 0.3's single-account ``state.json`` +
``wechat_credentials.json`` are migrated into the first account.

WeChatBot API used (pip ``wechatbot-sdk``, imported as ``wechatbot``):
``WeChatBot(cred_path=..., on_qr_url=..., on_scanned=..., on_expired=...,
on_error=...)``, ``@bot.on_message`` (handler gets ``msg.user_id`` /
``msg.text``), ``await bot.login()`` (returns credentials with
``account_id``), ``await bot.start()`` (runs until stopped), ``bot.stop()``,
``await bot.send(user_id, text)``. ``send`` needs a message from that user
received by this bot instance (its context token is kept in memory), else it
raises ``NoContextError``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.plugins.sdk import Plugin, PluginContext, SessionContext, ToolContext, action, service, tool

BIND_LIST_LIMIT = 10
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0


def _wechatbot():
    # Imported lazily so the plugin module loads (and reports a clear
    # "missing Python packages" error from the host) without the package.
    from wechatbot import WeChatBot

    return WeChatBot


def _short(session_id: str) -> str:
    return session_id[:8]


def _describe(ref: dict | None) -> str:
    if not ref:
        return "none"
    title = ref.get("title")
    return f"\"{title}\" ({_short(ref['id'])})" if title else _short(ref["id"])


def _assistant_text(content: str) -> str:
    """Text of a stored agent row (OpenAI assistant JSON; plain text for old rows)."""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return content or ""
    if not isinstance(payload, dict):
        return ""
    body = payload.get("content")
    if isinstance(body, str):
        return body
    if isinstance(body, list):
        return "".join(part.get("text", "") for part in body
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


class _Duplicate(Exception):
    """The QR login was for a WeChat account that is already added."""

    def __init__(self, other: str) -> None:
        super().__init__(other)
        self.other = other


@dataclass
class _Runtime:
    """In-memory state of one account."""
    task: asyncio.Task | None = None
    bot: Any = None
    conn: dict[str, Any] = field(default_factory=lambda: {"status": "idle", "message": "Not connected."})
    # \cmd bind target {id, title}: inbound + mirror while set.
    bind: dict | None = None


def _act(action_id: str, label: str, key: str | None = None, style: str = "default", **extra: Any) -> dict:
    item: dict[str, Any] = {"id": action_id, "label": label, "style": style}
    if key is not None:
        item["data"] = {"account": key}
    item.update(extra)
    return item


class WeChatPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__()
        self.ctx: PluginContext | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Saved per account (accounts.json), in display order.
        self._accounts: dict[str, dict[str, Any]] = {}
        self._rt: dict[str, _Runtime] = {}
        # Shown on the plugin page card until the next account is added.
        self._notice: str | None = None
        # Sessions with the plugin switched on: their cards list the accounts.
        self._session_ctxs: dict[str, SessionContext] = {}

    # ---- lifecycle ----

    async def on_load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._loop = asyncio.get_running_loop()
        self._accounts = self._read_accounts()
        self._migrate_single_account()
        ctx.events.subscribe("message.created", self._on_message_created)
        ctx.events.subscribe("session.deleted", self._on_session_deleted)
        self._render()

    async def on_config_changed(self, ctx: PluginContext, old: dict, new: dict) -> None:
        # Prefixes are read per message; keep the WeChat connections.
        return None

    async def on_session_enabled(self, ctx: SessionContext) -> None:
        self._session_ctxs[ctx.session_id] = ctx
        self._render()

    async def on_session_disabled(self, ctx: SessionContext) -> None:
        self._session_ctxs.pop(ctx.session_id, None)
        # Switching WeChat off in a session stops it receiving.
        for acc in self._accounts.values():
            if (acc.get("inbound") or {}).get("id") == ctx.session_id:
                acc["inbound"] = None
        self._write_accounts()
        self._render()

    # ---- persisted state ----

    def _accounts_path(self) -> Path:
        assert self.ctx is not None
        return self.ctx.data_dir / "accounts.json"

    def _account_dir(self, key: str) -> Path:
        assert self.ctx is not None
        path = self.ctx.data_dir / "accounts" / key
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cred_path(self, key: str) -> Path:
        return self._account_dir(key) / "credentials.json"

    def _logged_in_before(self, key: str) -> bool:
        return self._cred_path(key).exists()

    def _read_accounts(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._accounts_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            if self.ctx is not None:
                self.ctx.log.warning(f"ignoring unreadable accounts.json: {exc}")
            return {}
        accounts = data.get("accounts") if isinstance(data, dict) else None
        return {k: v for k, v in accounts.items() if isinstance(v, dict)} if isinstance(accounts, dict) else {}

    def _write_accounts(self) -> None:
        self._accounts_path().write_text(
            json.dumps({"accounts": self._accounts}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _migrate_single_account(self) -> None:
        """0.3 kept one account in state.json + wechat_credentials.json."""
        assert self.ctx is not None
        old_state = self.ctx.data_dir / "state.json"
        old_creds = self.ctx.data_dir / "wechat_credentials.json"
        if self._accounts or not old_creds.exists():
            return
        try:
            state = json.loads(old_state.read_text(encoding="utf-8")) if old_state.exists() else {}
        except (OSError, ValueError):
            state = {}
        key = self._new_key()
        shutil.move(str(old_creds), str(self._cred_path(key)))
        self._accounts[key] = {
            "alias": None, "wechat_id": None, "auto_connect": bool(state.get("auto_connect")),
            "inbound": state.get("inbound"), "contact": state.get("contact"),
        }
        self._write_accounts()
        if old_state.exists():
            old_state.replace(old_state.with_name("state.json.migrated"))
        self.ctx.log.info("migrated the single 0.3 account", account=key)

    def _new_key(self) -> str:
        while True:
            key = uuid.uuid4().hex[:8]
            if key not in self._accounts:
                return key

    def _rt_of(self, key: str) -> _Runtime:
        return self._rt.setdefault(key, _Runtime())

    def _account_key(self, data: dict) -> str:
        key = data.get("account") if isinstance(data, dict) else None
        if not isinstance(key, str) or key not in self._accounts:
            raise ValueError("unknown WeChat account")
        return key

    def _name(self, key: str) -> str:
        acc = self._accounts.get(key) or {}
        if acc.get("alias"):
            return acc["alias"]
        if acc.get("wechat_id"):
            return f"WeChat {acc['wechat_id'][:12]}"
        return "New account" if not self._logged_in_before(key) else f"WeChat account {key}"

    # ---- routing ----

    def _inbound(self, key: str) -> dict | None:
        return self._rt_of(key).bind or (self._accounts.get(key) or {}).get("inbound")

    def _account_for_session(self, session_id: str) -> str | None:
        """The account a session sends through: bound there first, else saved inbound."""
        for key in self._accounts:
            if (self._rt_of(key).bind or {}).get("id") == session_id:
                return key
        for key, acc in self._accounts.items():
            if (acc.get("inbound") or {}).get("id") == session_id:
                return key
        return None

    def _session_taken_by(self, session_id: str, *, besides: str) -> str | None:
        """Another account that receives in this session (saved or bound)."""
        for key, acc in self._accounts.items():
            if key == besides:
                continue
            if (acc.get("inbound") or {}).get("id") == session_id or (self._rt_of(key).bind or {}).get("id") == session_id:
                return key
        return None

    def _routing_text(self, key: str) -> str:
        acc = self._accounts.get(key) or {}
        bind = self._rt_of(key).bind
        if bind:
            text = f"Bound from WeChat to {_describe(bind)} (inbound + mirror; \\cmd unbind to undo)."
        elif acc.get("inbound"):
            text = f"Inbound session: {_describe(acc['inbound'])}."
        else:
            text = "No inbound session."
        if acc.get("contact"):
            text += f" Replies go to {acc['contact']}."
        return text

    # ---- UI ----

    def _render(self) -> None:
        """Push the plugin page card and every session card.

        Safe to call from any thread (WeChatBot callbacks may run off the loop).
        """
        loop = self._loop
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if loop is not None and not on_loop:
            loop.call_soon_threadsafe(self._render)
            return
        if self.ctx is None:
            return
        self.ctx.ui.update_nowait(self._page_card())
        for sid, sctx in list(self._session_ctxs.items()):
            sctx.ui.update_nowait(self._session_card(sid))

    def _conn_actions(self, key: str) -> list[dict]:
        rt = self._rt_of(key)
        if not self._logged_in_before(key) and rt.task is None:
            return [_act("connect", "Scan to connect", key, "primary"),
                    _act("remove_account", "Remove", key, "danger")]
        if not self._logged_in_before(key):
            return [_act("remove_account", "Cancel", key, "danger")]
        if rt.task is not None:
            return [_act("disconnect", "Disconnect", key, "danger")]
        return [_act("connect", "Connect", key, "primary")]

    def _section(self, key: str, actions: list[dict], extra: str = "") -> dict:
        conn = self._rt_of(key).conn
        section = {"title": self._name(key), "status": conn["status"],
                   "message": " ".join(p for p in (conn["message"], extra, self._routing_text(key)) if p),
                   "actions": actions}
        if conn.get("qr"):
            section["qr"] = conn["qr"]
        return section

    def _page_card(self) -> dict:
        sections = []
        for key in self._accounts:
            acc = self._accounts[key]
            actions = self._conn_actions(key)
            if self._logged_in_before(key):
                actions = [_act("rename", "Rename", key, input={
                    "name": "alias", "placeholder": "Name for this account", "value": acc.get("alias") or ""})
                ] + actions + [_act("remove_account", "Remove", key, "danger")]
            sections.append(self._section(key, actions))
        statuses = [s["status"] for s in sections]
        status = "error" if "error" in statuses else "waiting" if "waiting" in statuses else (
            "ok" if "ok" in statuses else "idle")
        message = self._notice or (f"{len(sections)} WeChat account(s)." if sections else "No WeChat account yet.")
        return {"status": status, "message": message, "sections": sections,
                "actions": [_act("add_account", "Add WeChat account", style="primary")]}

    def _session_card(self, session_id: str) -> dict:
        mine = self._account_for_session(session_id)
        sections = []
        for key in self._accounts:
            acc = self._accounts[key]
            receiving = (acc.get("inbound") or {}).get("id") == session_id
            actions = self._conn_actions(key)
            if receiving:
                actions.append(_act("clear_inbound", "Stop receiving here", key))
            elif self._logged_in_before(key) and (mine is None or mine == key):
                actions.append(_act("set_inbound", "Receive here", key))
            extra = "Sends to this session." if receiving else ""
            sections.append(self._section(key, actions, extra))
        if mine is not None:
            message = f"This session uses WeChat account {self._name(mine)}."
            status = self._rt_of(mine).conn["status"]
        else:
            message = "No WeChat account sends messages to this session."
            status = "idle"
        return {"status": status, "message": message, "sections": sections,
                "actions": [_act("add_account", "Add WeChat account", style="primary")]}

    def _set_conn(self, key: str, status: str, message: str, qr: str | None = None) -> None:
        conn: dict[str, Any] = {"status": status, "message": message}
        if qr:
            conn["qr"] = qr
        self._rt_of(key).conn = conn
        self._render()

    # ---- connections ----

    @service("bridge", scope="global", autostart=True, restart="on-failure")
    async def bridge(self, ctx: PluginContext) -> None:
        """Hold one task per connected account until stopped.

        Accounts that were connected (auto_connect) reconnect at startup.
        """
        try:
            for key, acc in list(self._accounts.items()):
                if acc.get("auto_connect"):
                    self._start_account(key)
            self._render()
            await asyncio.Event().wait()
        finally:
            tasks = [rt.task for rt in self._rt.values() if rt.task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._render()

    async def _ensure_bridge(self, ctx: PluginContext) -> None:
        handle = ctx.services.get("bridge")
        if not handle.running:
            await handle.start()

    def _start_account(self, key: str) -> None:
        rt = self._rt_of(key)
        if rt.task is not None and not rt.task.done():
            return
        rt.task = asyncio.create_task(self._run_account(key), name=f"wechat-account-{key}")

    async def _stop_account(self, key: str) -> None:
        rt = self._rt_of(key)
        task, rt.task = rt.task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run_account(self, key: str) -> None:
        """Log in and relay; reconnect with backoff while the account should be connected."""
        assert self.ctx is not None
        log = self.ctx.log
        backoff = BACKOFF_START
        try:
            while key in self._accounts:
                had_creds = self._logged_in_before(key)
                try:
                    if await self._connect_once(key):
                        backoff = BACKOFF_START  # it was connected: start over
                except asyncio.CancelledError:
                    raise
                except _Duplicate as dup:
                    log.warning("account already added", account=key, other=dup.other)
                    self._notice = (f"That WeChat account is already added as {self._name(dup.other)}; "
                                    f"the new entry was removed.")
                    self._drop_account(key)
                    return
                except Exception as exc:
                    log.error(f"WeChat account {key} failed: {type(exc).__name__}: {exc}", account=key)
                    if not had_creds and not self._logged_in_before(key):
                        # A QR login that never finished: don't keep producing QR codes.
                        self._notice = f"Login for the new account did not finish ({exc}). Add it again to retry."
                        self._drop_account(key)
                        return
                    self._set_conn(key, "error", f"WeChat error: {exc}")
                acc = self._accounts.get(key)
                if acc is None or not acc.get("auto_connect"):
                    return
                self._set_conn(key, "waiting", f"Reconnecting in {backoff:g}s…")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
        finally:
            rt = self._rt.get(key)
            if rt is not None:
                if rt.task is asyncio.current_task():
                    rt.task = None
                rt.bot = None
                if rt.conn["status"] != "error" and key in self._accounts:
                    rt.conn = {"status": "idle", "message": "Not connected."}
            self._render()

    async def _connect_once(self, key: str) -> bool:
        """One login + receive loop; returns (True: had logged in) when the bot stops."""
        assert self.ctx is not None
        log = self.ctx.log
        WeChatBot = _wechatbot()

        def on_qr_url(url: str) -> None:
            log.info("QR login required", account=key)
            self._set_conn(key, "waiting", "Scan the QR code with WeChat to connect.", qr=url)

        def on_scanned() -> None:
            self._set_conn(key, "waiting", "QR scanned. Confirm the login in WeChat.")

        def on_expired() -> None:
            self._set_conn(key, "waiting", "The QR code expired; waiting for a new one…")

        def on_error(exc: Exception) -> None:
            log.error(f"WeChat error: {exc}", account=key)
            self._set_conn(key, "error", f"WeChat error: {exc}")

        self._set_conn(key, "waiting", "Connecting…")
        bot = WeChatBot(cred_path=str(self._cred_path(key)), on_qr_url=on_qr_url, on_scanned=on_scanned,
                        on_expired=on_expired, on_error=on_error)

        @bot.on_message
        async def handle(msg: Any) -> None:
            await self._on_wechat_message(key, msg.user_id, msg.text or "")

        rt = self._rt_of(key)
        try:
            creds = await bot.login()
            wechat_id = getattr(creds, "account_id", None) or getattr(creds, "user_id", None)
            if wechat_id:
                other = next((k for k, a in self._accounts.items() if k != key and a.get("wechat_id") == wechat_id), None)
                if other is not None:
                    raise _Duplicate(other)
                if self._accounts[key].get("wechat_id") != wechat_id:
                    self._accounts[key]["wechat_id"] = wechat_id
                    self._write_accounts()
            rt.bot = bot
            log.info("connected", account=key)
            self._set_conn(key, "ok", "Connected.")
            await bot.start()
            return True
        finally:
            rt.bot = None
            try:
                bot.stop()
            except Exception as exc:  # the bot may not have started
                log.debug(f"bot.stop failed: {exc}")

    def _drop_account(self, key: str) -> None:
        """Forget an account and delete its credentials (its task must be done or be the caller)."""
        self._accounts.pop(key, None)
        self._rt.pop(key, None)
        self._write_accounts()
        assert self.ctx is not None
        shutil.rmtree(self.ctx.data_dir / "accounts" / key, ignore_errors=True)
        self._render()

    # ---- actions ----

    @action("add_account")
    async def add_account(self, ctx: PluginContext, data: dict) -> str:
        """Start a QR login for a new account. From a session card with no
        account yet, the new account sends to that session."""
        key = self._new_key()
        inbound = None
        if isinstance(ctx, SessionContext) and self._account_for_session(ctx.session_id) is None:
            info = await ctx.session()
            inbound = {"id": info.id, "title": info.title}
        self._accounts[key] = {"alias": None, "wechat_id": None, "auto_connect": True,
                               "inbound": inbound, "contact": None}
        self._write_accounts()
        self._notice = None
        await self._ensure_bridge(ctx)
        self._start_account(key)
        return key

    @action("connect")
    async def connect(self, ctx: PluginContext, data: dict) -> str:
        key = self._account_key(data)
        self._accounts[key]["auto_connect"] = True
        self._write_accounts()
        await self._ensure_bridge(ctx)
        self._start_account(key)
        return "connecting"

    @action("disconnect")
    async def disconnect(self, ctx: PluginContext, data: dict) -> str:
        key = self._account_key(data)
        self._accounts[key]["auto_connect"] = False
        self._write_accounts()
        await self._stop_account(key)
        self._set_conn(key, "idle", "Not connected.")
        return "disconnected"

    @action("remove_account")
    async def remove_account(self, ctx: PluginContext, data: dict) -> str:
        key = self._account_key(data)
        await self._stop_account(key)
        self._drop_account(key)
        return "removed"

    @action("rename")
    async def rename(self, ctx: PluginContext, data: dict) -> str:
        key = self._account_key(data)
        alias = str(data.get("alias") or "").strip()[:40] or None
        self._accounts[key]["alias"] = alias
        self._write_accounts()
        self._render()
        return alias or ""

    @action("set_inbound")
    async def set_inbound(self, ctx: PluginContext, data: dict) -> str:
        if not isinstance(ctx, SessionContext):
            raise ValueError("set the inbound session from that session's plugin card")
        key = self._account_key(data)
        other = self._session_taken_by(ctx.session_id, besides=key)
        if other is not None:
            raise ValueError(f"this session already receives WeChat account {self._name(other)}; "
                             f"stop that first")
        info = await ctx.session()
        self._accounts[key]["inbound"] = {"id": info.id, "title": info.title}
        self._write_accounts()
        ctx.log.info("inbound session set", account=key)
        self._render()
        return "inbound"

    @action("clear_inbound")
    async def clear_inbound(self, ctx: PluginContext, data: dict) -> str:
        key = self._account_key(data)
        acc = self._accounts[key]
        if isinstance(ctx, SessionContext) and (acc.get("inbound") or {}).get("id") != ctx.session_id:
            return "not inbound"
        acc["inbound"] = None
        self._write_accounts()
        self._render()
        return "cleared"

    # ---- WeChat -> Augentia ----

    async def _on_wechat_message(self, key: str, user_id: str, text: str) -> None:
        assert self.ctx is not None
        ctx = self.ctx
        acc = self._accounts.get(key)
        if acc is None:
            return
        if acc.get("contact") != user_id:
            acc["contact"] = user_id
            self._write_accounts()
            self._render()
        prefix = ctx.global_config.get("command_prefix") or ""
        if prefix and text.startswith(prefix):
            await self._command(key, user_id, text[len(prefix):].strip())
            return
        if not text.strip():
            return
        target = self._inbound(key)
        if target is None:
            await self._reply(key, user_id, f"No Augentia session receives this WeChat account's messages yet. "
                                            f"Set one from a session's WeChat card, or use {prefix} bind <session>.")
            return
        ctx.log.info("message from WeChat", account=key, user=user_id, length=len(text), session_id=target["id"])
        try:
            await ctx.sessions.send(target["id"], f"{ctx.global_config.get('message_prefix', '')}{text}",
                                    label=f"wechat:{user_id}")
        except KeyError:
            await self._forget_session(target["id"])
            await self._reply(key, user_id, f"Session {_describe(target)} no longer exists; the message was not "
                                            f"delivered. Use {prefix} bind <session> or set a new inbound session.")

    async def _command(self, key: str, user_id: str, command: str) -> None:
        assert self.ctx is not None
        prefix = self.ctx.global_config.get("command_prefix") or ""
        name, _, arg = command.partition(" ")
        name, arg = (name or "help").lower(), arg.strip()
        rt = self._rt_of(key)
        if name == "bind":
            await self._command_bind(key, user_id, arg, prefix)
        elif name == "unbind":
            if rt.bind is None:
                await self._reply(key, user_id, "Not bound. " + self._routing_text(key))
                return
            rt.bind = None
            self._render()
            await self._reply(key, user_id, "Unbound. " + self._routing_text(key))
        elif name == "status":
            await self._reply(key, user_id, f"Augentia WeChat account {self._name(key)}. {self._routing_text(key)}")
        elif name == "help":
            await self._reply(key, user_id, (
                f"{prefix} bind <session id, id prefix or title>: talk to that session here "
                f"(everything its agent writes comes back).\n"
                f"{prefix} unbind: back to the default inbound session.\n"
                f"{prefix} status: where messages go.\n"
                f"Anything else goes to the inbound session."))
        else:
            await self._reply(key, user_id, f"Unknown command: {name}. Try {prefix} help.")

    async def _command_bind(self, key: str, user_id: str, ref: str, prefix: str) -> None:
        assert self.ctx is not None
        sessions = list(reversed(await self.ctx.sessions.list()))  # newest first

        def listing(items) -> str:
            lines = []
            for s in items[:BIND_LIST_LIMIT]:
                other = self._session_taken_by(s.id, besides=key)
                taken = f"  (uses {self._name(other)})" if other else ""
                lines.append(f"{_short(s.id)}  {s.title or '(untitled)'}{taken}")
            return "\n".join(lines)

        if not ref:
            await self._reply(key, user_id, f"Usage: {prefix} bind <session id, id prefix or title>\n{listing(sessions)}")
            return
        lowered = ref.lower()
        matches = [s for s in sessions if s.id == ref]
        matches = matches or [s for s in sessions if s.id.startswith(ref)]
        matches = matches or [s for s in sessions if (s.title or "").lower() == lowered]
        if len(matches) != 1:
            if not matches:
                await self._reply(key, user_id, f"No session matches \"{ref}\". {prefix} bind lists recent ones.")
            else:
                await self._reply(key, user_id, f"\"{ref}\" matches {len(matches)} sessions:\n{listing(matches)}")
            return
        target = matches[0]
        other = self._session_taken_by(target.id, besides=key)
        if other is not None:
            await self._reply(key, user_id, f"Session {_describe({'id': target.id, 'title': target.title})} "
                                            f"already uses WeChat account {self._name(other)}; pick another.")
            return
        rt = self._rt_of(key)
        rt.bind = {"id": target.id, "title": target.title}
        self.ctx.log.info("bound from WeChat", account=key, session_id=target.id)
        self._render()
        await self._reply(key, user_id, f"Bound to {_describe(rt.bind)}. Messages go there and everything its "
                                        f"agent writes comes back here. {prefix} unbind to undo.")

    async def _on_session_deleted(self, event: Any) -> None:
        session_id = (event.data or {}).get("session_id")
        if session_id:
            await self._forget_session(session_id)

    async def _forget_session(self, session_id: str) -> None:
        changed = False
        for key, acc in self._accounts.items():
            rt = self._rt_of(key)
            if (rt.bind or {}).get("id") == session_id:
                rt.bind = None
                changed = True
            if (acc.get("inbound") or {}).get("id") == session_id:
                acc["inbound"] = None
                changed = True
        self._session_ctxs.pop(session_id, None)
        if changed:
            self._write_accounts()
            self._render()

    # ---- Augentia -> WeChat ----

    async def _on_message_created(self, event: Any) -> None:
        """Mirror sessions: forward every assistant message (with text) to their account."""
        data = event.data or {}
        if data.get("role") != "agent":
            return
        for key in list(self._accounts):
            if (self._rt_of(key).bind or {}).get("id") == data.get("session_id"):
                text = _assistant_text(data.get("content") or "")
                if text.strip():
                    await self._send_to_contact(key, text)

    async def _reply(self, key: str, user_id: str, text: str) -> None:
        bot = self._rt_of(key).bot
        if bot is None:
            return
        try:
            await bot.send(user_id, text)
        except Exception as exc:
            if self.ctx is not None:
                self.ctx.log.error(f"could not send to WeChat: {exc}", account=key)

    async def _send_to_contact(self, key: str, text: str) -> str | None:
        """Send to the account's last contact; returns an error message, or None when sent."""
        bot = self._rt_of(key).bot
        if bot is None:
            return f"WeChat account {self._name(key)} is not connected."
        contact = (self._accounts.get(key) or {}).get("contact")
        if not contact:
            return "No WeChat contact has written to this account yet, so there is nobody to send to."
        try:
            await bot.send(contact, text.strip())
        except Exception as exc:
            if type(exc).__name__ == "NoContextError":
                return ("WeChat only allows replying after the user has written since the account "
                        "(re)connected; ask them to send any message on WeChat first.")
            if self.ctx is not None:
                self.ctx.log.error(f"could not send to WeChat: {exc}", account=key)
            return "Sending to WeChat failed; see the plugin logs."
        return None

    # ---- agent tool ----

    @tool(
        "send",
        description="Send a WeChat message to the user through the WeChat account bound to this session. "
                    "Messages that start with \"[from WeChat\" came from WeChat: answer them with this tool, "
                    "since your normal replies are not forwarded there.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Message text."}},
            "required": ["text"],
            "additionalProperties": False,
        },
        requires_approval=False,
    )
    async def send(self, ctx: ToolContext, text: str) -> str:
        if not text.strip():
            return "Nothing to send: the text is empty."
        key = self._account_for_session(ctx.session_id)
        if key is None:
            return ("This session is not bound to a WeChat account. Set it as an account's inbound "
                    "session on its WeChat plugin card, or bind it from WeChat with \\cmd bind.")
        error = await self._send_to_contact(key, text)
        return error or "Sent."
