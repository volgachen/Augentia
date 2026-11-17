"""WeChat bridge for Augentia (plugin API v1, see backend/app/plugins/README.md).

One WeChat account per backend, logged in by a global ``bridge`` service.
Two session roles decide where text goes:

- **inbound session**: WeChat messages are sent into it. Set from a session's
  plugin card ("Receive WeChat here"); persisted.
- **mirror session**: everything the agent writes there (every assistant
  message, including text before tool calls) is forwarded to WeChat. None by
  default.

``\\cmd bind <session>`` in WeChat temporarily makes one session both inbound
and mirror (a conversation with it through WeChat); ``\\cmd unbind`` goes back
to the saved inbound session and no mirror. The binding is in memory only, so
a backend restart also ends it.

Outside the mirror the agent answers with the ``wechat__send`` tool; nothing
is forwarded automatically. Messages go to the WeChat contact who wrote last
(persisted).

Files in ``ctx.data_dir``: ``wechat_credentials.json`` (WeChatBot login) and
``state.json`` (``auto_connect``, ``inbound``, ``contact``).

WeChatBot API used (``wechatbot`` package): ``WeChatBot(cred_path=...,
on_qr_url=..., on_scanned=..., on_expired=..., on_error=...)``,
``@bot.on_message`` (handler gets ``msg.user_id`` / ``msg.text``),
``await bot.login()``, ``await bot.start()`` (runs until stopped),
``bot.stop()``, ``await bot.send(user_id, text)``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.plugins.sdk import Plugin, PluginContext, SessionContext, ToolContext, action, service, tool

CONNECT = {"id": "connect", "label": "Scan to connect", "style": "primary"}
DISCONNECT = {"id": "disconnect", "label": "Disconnect", "style": "danger"}
SET_INBOUND = {"id": "set_inbound", "label": "Receive WeChat here", "style": "default"}
CLEAR_INBOUND = {"id": "clear_inbound", "label": "Stop receiving WeChat here", "style": "default"}

BIND_LIST_LIMIT = 10


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


class WeChatPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__()
        self.ctx: PluginContext | None = None
        self._bot: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Connection card shared by every view: status / message / qr.
        self._conn: dict[str, Any] = {"status": "idle", "message": "Not connected."}
        # Saved: auto_connect (reconnect at startup), inbound {id, title}, contact.
        self._saved: dict[str, Any] = {}
        # \cmd bind target {id, title}: inbound and mirror while set. Memory only.
        self._bind: dict | None = None
        # Sessions with the plugin switched on: their cards show the bridge.
        self._session_ctxs: dict[str, SessionContext] = {}

    # ---- lifecycle ----

    async def on_load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._loop = asyncio.get_running_loop()
        self._saved = self._read_state()
        ctx.events.subscribe("message.created", self._on_message_created)
        ctx.events.subscribe("session.deleted", self._on_session_deleted)
        self._render()

    async def on_config_changed(self, ctx: PluginContext, old: dict, new: dict) -> None:
        # Prefixes are read per message; keep the WeChat connection.
        return None

    async def on_session_enabled(self, ctx: SessionContext) -> None:
        self._session_ctxs[ctx.session_id] = ctx
        self._render()

    async def on_session_disabled(self, ctx: SessionContext) -> None:
        self._session_ctxs.pop(ctx.session_id, None)
        # Switching WeChat off in the inbound session stops it receiving.
        if (self._saved.get("inbound") or {}).get("id") == ctx.session_id:
            self._save(inbound=None)
        self._render()

    # ---- persisted state ----

    def _state_path(self):
        assert self.ctx is not None
        return self.ctx.data_dir / "state.json"

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            if self.ctx is not None:
                self.ctx.log.warning(f"ignoring unreadable state.json: {exc}")
            return {}

    def _save(self, **changes: Any) -> None:
        self._saved.update(changes)
        self._state_path().write_text(json.dumps(self._saved, ensure_ascii=False, indent=2), encoding="utf-8")

    def _inbound(self) -> dict | None:
        return self._bind or self._saved.get("inbound")

    def _mirror(self) -> dict | None:
        return self._bind

    # ---- UI ----

    def _render(self) -> None:
        """Push the cards: the plugin page and every session with the plugin on.

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
        conn_action = DISCONNECT if self._conn["status"] != "idle" else CONNECT
        routing = self._routing_text()
        self.ctx.ui.update_nowait(self._card(routing, [conn_action]))
        inbound_id = (self._saved.get("inbound") or {}).get("id")
        for sid, sctx in list(self._session_ctxs.items()):
            here = inbound_id == sid
            lines = [routing]
            if here:
                lines.insert(0, "This session receives WeChat messages.")
            sctx.ui.update_nowait(self._card(" ".join(lines), [conn_action, CLEAR_INBOUND if here else SET_INBOUND]))

    def _card(self, routing: str, actions: list[dict]) -> dict:
        card = {"status": self._conn["status"], "message": f"{self._conn['message']} {routing}".strip(),
                "actions": actions}
        if self._conn.get("qr"):
            card["qr"] = self._conn["qr"]
        return card

    def _routing_text(self) -> str:
        if self._bind:
            return f"Bound from WeChat to {_describe(self._bind)} (inbound + mirror; \\cmd unbind to undo)."
        inbound = self._saved.get("inbound")
        text = f"Inbound session: {_describe(inbound)}." if inbound else "No inbound session set."
        contact = self._saved.get("contact")
        if contact:
            text += f" Replies go to {contact}."
        return text

    def _set_conn(self, status: str, message: str, qr: str | None = None) -> None:
        self._conn = {"status": status, "message": message}
        if qr:
            self._conn["qr"] = qr
        self._render()

    # ---- connection ----

    @service("bridge", scope="global", autostart=True, restart="on-failure")
    async def bridge(self, ctx: PluginContext) -> None:
        """Log in (QR code if no saved credentials) and relay messages until stopped.

        Autostarts with the plugin but only connects if the user connected
        before and did not disconnect (state.json auto_connect).
        """
        if not self._saved.get("auto_connect"):
            self._set_conn("idle", "Not connected.")
            return
        WeChatBot = _wechatbot()

        def on_qr_url(url: str) -> None:
            ctx.log.info("QR login required")
            self._set_conn("waiting", "Scan the QR code with WeChat to connect.", qr=url)

        def on_scanned() -> None:
            ctx.log.info("QR scanned")
            self._set_conn("waiting", "QR scanned. Confirm the login in WeChat.")

        def on_expired() -> None:
            ctx.log.warning("QR expired")
            self._set_conn("waiting", "The QR code expired; waiting for a new one…")

        def on_error(exc: Exception) -> None:
            ctx.log.error(f"WeChat error: {exc}")
            self._set_conn("error", f"WeChat error: {exc}")

        self._set_conn("waiting", "Connecting…")
        bot = WeChatBot(
            cred_path=str(ctx.data_dir / "wechat_credentials.json"),
            on_qr_url=on_qr_url,
            on_scanned=on_scanned,
            on_expired=on_expired,
            on_error=on_error,
        )

        @bot.on_message
        async def handle(msg: Any) -> None:
            await self._on_wechat_message(msg.user_id, msg.text or "")

        try:
            await bot.login()
            self._bot = bot
            ctx.log.info("connected")
            self._set_conn("ok", "Connected.")
            await bot.start()
        finally:
            self._bot = None
            try:
                bot.stop()
            except Exception as exc:  # the bot may not have started
                ctx.log.debug(f"bot.stop failed: {exc}")
            self._set_conn("idle", "Not connected.")

    @action("connect")
    async def connect(self, ctx: PluginContext, data: dict) -> str:
        self._save(auto_connect=True)
        # Connecting from a session card with nothing receiving yet: this one does.
        if isinstance(ctx, SessionContext) and not self._saved.get("inbound"):
            await self._set_inbound(ctx)
        await ctx.services.get("bridge").start()
        return "connecting"

    @action("disconnect")
    async def disconnect(self, ctx: PluginContext, data: dict) -> str:
        self._save(auto_connect=False)
        await ctx.services.get("bridge").stop()
        self._set_conn("idle", "Not connected.")
        return "disconnected"

    @action("set_inbound")
    async def set_inbound(self, ctx: PluginContext, data: dict) -> str:
        if not isinstance(ctx, SessionContext):
            raise ValueError("set the inbound session from that session's plugin card")
        await self._set_inbound(ctx)
        return "inbound"

    @action("clear_inbound")
    async def clear_inbound(self, ctx: PluginContext, data: dict) -> str:
        if isinstance(ctx, SessionContext) and (self._saved.get("inbound") or {}).get("id") != ctx.session_id:
            return "not inbound"
        self._save(inbound=None)
        self._render()
        return "cleared"

    async def _set_inbound(self, ctx: SessionContext) -> None:
        info = await ctx.session()
        self._save(inbound={"id": info.id, "title": info.title})
        ctx.log.info("inbound session set")
        self._render()

    # ---- WeChat -> Augentia ----

    async def _on_wechat_message(self, user_id: str, text: str) -> None:
        assert self.ctx is not None
        ctx = self.ctx
        if self._saved.get("contact") != user_id:
            self._save(contact=user_id)
            self._render()
        prefix = ctx.global_config.get("command_prefix") or ""
        if prefix and text.startswith(prefix):
            await self._command(user_id, text[len(prefix):].strip())
            return
        if not text.strip():
            return
        target = self._inbound()
        if target is None:
            await self._reply(user_id, f"No Augentia session receives WeChat messages yet. Set one from a "
                                       f"session's WeChat card, or use {prefix} bind <session>.")
            return
        ctx.log.info("message from WeChat", user=user_id, length=len(text), session_id=target["id"])
        try:
            await ctx.sessions.send(target["id"], f"{ctx.global_config.get('message_prefix', '')}{text}",
                                    label=f"wechat:{user_id}")
        except KeyError:
            await self._forget_session(target["id"])
            await self._reply(user_id, f"Session {_describe(target)} no longer exists; the message was not "
                                       f"delivered. Use {prefix} bind <session> or set a new inbound session.")

    async def _command(self, user_id: str, command: str) -> None:
        assert self.ctx is not None
        prefix = self.ctx.global_config.get("command_prefix") or ""
        name, _, arg = command.partition(" ")
        name, arg = (name or "help").lower(), arg.strip()
        if name == "bind":
            await self._command_bind(user_id, arg, prefix)
        elif name == "unbind":
            if self._bind is None:
                await self._reply(user_id, "Not bound. " + self._routing_text())
                return
            self._bind = None
            self._render()
            await self._reply(user_id, "Unbound. " + self._routing_text())
        elif name == "status":
            state = "connected" if self._bot is not None else self._conn["status"]
            await self._reply(user_id, f"WeChat bridge {state}. {self._routing_text()}")
        elif name == "help":
            await self._reply(user_id, (
                f"{prefix} bind <session id, id prefix or title>: talk to that session here "
                f"(everything its agent writes comes back).\n"
                f"{prefix} unbind: back to the default inbound session.\n"
                f"{prefix} status: where messages go.\n"
                f"Anything else goes to the inbound session."))
        else:
            await self._reply(user_id, f"Unknown command: {name}. Try {prefix} help.")

    async def _command_bind(self, user_id: str, ref: str, prefix: str) -> None:
        assert self.ctx is not None
        sessions = list(reversed(await self.ctx.sessions.list()))  # newest first
        if not ref:
            listing = "\n".join(f"{_short(s.id)}  {s.title or '(untitled)'}" for s in sessions[:BIND_LIST_LIMIT])
            await self._reply(user_id, f"Usage: {prefix} bind <session id, id prefix or title>\n{listing}")
            return
        lowered = ref.lower()
        matches = [s for s in sessions if s.id == ref]
        matches = matches or [s for s in sessions if s.id.startswith(ref)]
        matches = matches or [s for s in sessions if (s.title or "").lower() == lowered]
        if len(matches) != 1:
            if not matches:
                await self._reply(user_id, f"No session matches \"{ref}\". {prefix} bind lists recent ones.")
            else:
                listing = "\n".join(f"{_short(s.id)}  {s.title or '(untitled)'}" for s in matches[:BIND_LIST_LIMIT])
                await self._reply(user_id, f"\"{ref}\" matches {len(matches)} sessions:\n{listing}")
            return
        target = matches[0]
        self._bind = {"id": target.id, "title": target.title}
        self.ctx.log.info("bound from WeChat", session_id=target.id)
        self._render()
        await self._reply(user_id, f"Bound to {_describe(self._bind)}. Messages go there and everything its "
                                   f"agent writes comes back here. {prefix} unbind to undo.")

    async def _on_session_deleted(self, event: Any) -> None:
        session_id = (event.data or {}).get("session_id")
        if session_id:
            await self._forget_session(session_id)

    async def _forget_session(self, session_id: str) -> None:
        changed = False
        if (self._bind or {}).get("id") == session_id:
            self._bind = None
            changed = True
        if (self._saved.get("inbound") or {}).get("id") == session_id:
            self._save(inbound=None)
            changed = True
        self._session_ctxs.pop(session_id, None)
        if changed:
            self._render()

    # ---- Augentia -> WeChat ----

    async def _on_message_created(self, event: Any) -> None:
        """Mirror session: forward every assistant message (with text) to WeChat."""
        mirror = self._mirror()
        data = event.data or {}
        if mirror is None or data.get("session_id") != mirror["id"] or data.get("role") != "agent":
            return
        text = _assistant_text(data.get("content") or "")
        if text.strip():
            await self._send_to_contact(text)

    async def _reply(self, user_id: str, text: str) -> None:
        if self._bot is None:
            return
        try:
            await self._bot.send(user_id, text)
        except Exception as exc:
            if self.ctx is not None:
                self.ctx.log.error(f"could not send to WeChat: {exc}")

    async def _send_to_contact(self, text: str) -> str | None:
        """Send to the saved contact; returns an error message, or None when sent."""
        if self._bot is None:
            return "WeChat is not connected."
        contact = self._saved.get("contact")
        if not contact:
            return "No WeChat contact has written yet, so there is nobody to send to."
        try:
            await self._bot.send(contact, text.strip())
        except Exception as exc:
            if self.ctx is not None:
                self.ctx.log.error(f"could not send to WeChat: {exc}")
            return "Sending to WeChat failed; see the plugin logs."
        return None

    # ---- agent tool ----

    @tool(
        "send",
        description="Send a WeChat message to the user. Messages that start with "
                    "\"[from WeChat\" came from WeChat: answer them with this tool, since "
                    "your normal replies are not forwarded there.",
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
        error = await self._send_to_contact(text)
        return error or "Sent."
