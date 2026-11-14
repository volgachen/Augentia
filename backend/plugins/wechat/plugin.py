"""WeChat bridge for Augentia (plugin API v1, see backend/app/plugins/README.md).

Per session, the user connects a WeChat account by scanning a QR code; after
that:

- messages from WeChat are sent into the session (``ctx.sessions.send``), and
  the agent's reply goes back to whoever wrote last;
- messages starting with the command prefix are bridge commands, answered
  in WeChat directly (``\\cmd status``, ``\\cmd help``);
- the agent can message the contact itself with the ``wechat__send`` tool.

Pieces: the ``bridge`` session service owns the WeChatBot (login + long-poll
loop) and keeps it in ``ctx.state``; the ``connect`` / ``disconnect`` actions
start and stop that service; ``message.generated`` / ``turn.end`` hooks
forward replies. The service is not autostarted: logging in needs the user,
so the session panel shows a "Scan to connect" button.

WeChatBot API used (``wechatbot`` package): ``WeChatBot(cred_path=...,
on_qr_url=..., on_scanned=..., on_expired=..., on_error=...)``,
``@bot.on_message`` (handler gets ``msg.user_id`` / ``msg.text``),
``await bot.login()``, ``await bot.start()`` (runs until stopped),
``bot.stop()``, ``await bot.send(user_id, text)``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.plugins.sdk import Plugin, SessionContext, ToolContext, TurnSummary, action, hook, service, tool

BOT = "bot"
LAST_USER = "last_user"

CONNECT = {"id": "connect", "label": "Scan to connect", "style": "primary"}
DISCONNECT = {"id": "disconnect", "label": "Disconnect", "style": "danger"}


def _wechatbot():
    # Imported lazily so the plugin module loads (and reports a clear
    # "missing Python packages" error from the host) without the package.
    from wechatbot import WeChatBot

    return WeChatBot


class WeChatPlugin(Plugin):
    # ---- connection ----

    @service("bridge", scope="session", autostart=False, restart="on-failure")
    async def bridge(self, ctx: SessionContext) -> None:
        """Log in (QR code if no saved credentials) and relay messages until stopped."""
        WeChatBot = _wechatbot()
        loop = asyncio.get_running_loop()

        def show(status: str, message: str, *, qr: str | None = None, actions=(DISCONNECT,)) -> None:
            ui = {"status": status, "message": message, "actions": list(actions)}
            if qr:
                ui["qr"] = qr
            # WeChatBot callbacks may run off the event loop thread; on the
            # loop, update right away so updates keep their order.
            try:
                on_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                on_loop = False
            if on_loop:
                ctx.ui.update_nowait(ui)
            else:
                loop.call_soon_threadsafe(ctx.ui.update_nowait, ui)

        def on_qr_url(url: str) -> None:
            ctx.log.info("QR login required")
            show("waiting", "Scan the QR code with WeChat to connect this session.", qr=url)

        def on_scanned() -> None:
            ctx.log.info("QR scanned")
            show("waiting", "QR scanned. Confirm the login in WeChat.")

        def on_expired() -> None:
            ctx.log.warning("QR expired")
            show("waiting", "The QR code expired; waiting for a new one…")

        def on_error(exc: Exception) -> None:
            ctx.log.error(f"WeChat error: {exc}")
            show("error", f"WeChat error: {exc}")

        show("waiting", "Connecting…")
        bot = WeChatBot(
            cred_path=str(ctx.session_data_dir / "wechat_credentials.json"),
            on_qr_url=on_qr_url,
            on_scanned=on_scanned,
            on_expired=on_expired,
            on_error=on_error,
        )

        @bot.on_message
        async def handle(msg: Any) -> None:
            await self._on_wechat_message(ctx, bot, msg.user_id, msg.text or "")

        try:
            await bot.login()
            ctx.state[BOT] = bot
            ctx.log.info("connected")
            await ctx.ui.update({"status": "ok", "message": self._connected_text(ctx),
                                 "actions": [DISCONNECT]})
            await bot.start()
        finally:
            ctx.state.pop(BOT, None)
            try:
                bot.stop()
            except Exception as exc:  # the bot may not have started
                ctx.log.debug(f"bot.stop failed: {exc}")
            ctx.ui.update_nowait({"status": "idle", "message": "Not connected.", "actions": [CONNECT]})

    def _connected_text(self, ctx: SessionContext) -> str:
        user = ctx.state.get(LAST_USER)
        if user:
            return f"Connected. Replies go to WeChat contact {user}."
        return "Connected. Waiting for the first WeChat message; replies go to whoever writes last."

    async def _on_wechat_message(self, ctx: SessionContext, bot: Any, user_id: str, text: str) -> None:
        config = ctx.config
        first = ctx.state.get(LAST_USER) != user_id
        ctx.state[LAST_USER] = user_id
        if first:
            await ctx.ui.update({"status": "ok", "message": self._connected_text(ctx), "actions": [DISCONNECT]})
        prefix = config.get("command_prefix") or ""
        if prefix and text.startswith(prefix):
            await self._command(ctx, bot, user_id, text[len(prefix):].strip())
            return
        if not text.strip():
            return
        ctx.log.info("message from WeChat", user=user_id, length=len(text))
        await ctx.sessions.send(ctx.session_id, f"{config.get('message_prefix', '')}{text}",
                                label=f"wechat:{user_id}")

    async def _command(self, ctx: SessionContext, bot: Any, user_id: str, command: str) -> None:
        name = command.split()[0] if command else "help"
        prefix = ctx.config.get("command_prefix") or ""
        if name == "status":
            info = await ctx.session()
            await bot.send(user_id, f"Connected to Augentia session \"{info.title or info.id}\" ({info.status}).")
        elif name == "help":
            await bot.send(user_id, f"Commands: {prefix} status, {prefix} help. Anything else goes to the agent.")
        else:
            await bot.send(user_id, f"Unknown command: {name}. Try {prefix} help.")

    @action("connect")
    async def connect(self, ctx: SessionContext, data: dict) -> str:
        await ctx.services.get("bridge").start()
        return "connecting"

    @action("disconnect")
    async def disconnect(self, ctx: SessionContext, data: dict) -> str:
        await ctx.services.get("bridge").stop()
        return "disconnected"

    async def on_session_enabled(self, ctx: SessionContext) -> None:
        if ctx.state.get(BOT) is None:
            await ctx.ui.update({"status": "idle", "message": "Not connected.", "actions": [CONNECT]})

    async def on_session_disabled(self, ctx: SessionContext) -> None:
        ctx.state.pop(LAST_USER, None)

    # ---- replies to WeChat ----

    @hook("message.generated")
    async def forward_intermediate(self, ctx: SessionContext, reply) -> None:
        """every_reply mode: forward text the agent writes alongside tool calls."""
        if reply.final or not reply.content.strip() or ctx.config.get("reply_mode") != "every_reply":
            return None
        await self._send_to_contact(ctx, reply.content)
        return None

    @hook("turn.end")
    async def forward_reply(self, ctx: SessionContext, summary: TurnSummary) -> None:
        if summary.stop_reason != "completed" or not (summary.final_text or "").strip():
            return
        await self._send_to_contact(ctx, summary.final_text or "")

    async def _send_to_contact(self, ctx: SessionContext, text: str) -> bool:
        bot, user = ctx.state.get(BOT), ctx.state.get(LAST_USER)
        if bot is None or not user:
            if bot is not None:
                ctx.log.warning("no WeChat contact has written yet; reply not forwarded")
            return False
        try:
            await bot.send(user, text.strip())
            return True
        except Exception as exc:
            ctx.log.error(f"could not send to WeChat: {exc}")
            return False

    # ---- agent tool ----

    @tool(
        "send",
        description="Send a WeChat message to the contact connected to this session "
                    "(the last person who wrote). Use it for progress updates during long tasks; "
                    "your final answer is forwarded automatically.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Message text."}},
            "required": ["text"],
            "additionalProperties": False,
        },
        requires_approval=False,
    )
    async def send(self, ctx: ToolContext, text: str) -> str:
        if ctx.state.get(BOT) is None:
            return "WeChat is not connected for this session."
        if not ctx.state.get(LAST_USER):
            return "No WeChat contact has written to this session yet, so there is nobody to send to."
        if not await self._send_to_contact(ctx, text):
            return "Sending to WeChat failed; see the plugin logs."
        return "Sent."
