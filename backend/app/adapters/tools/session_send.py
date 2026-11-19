import json
import logging
import os
import httpx
from app.auth import internal_auth_headers
from app.adapters.tools.base import BaseTool
from app.adapters.tools.registry import register_tool

logger = logging.getLogger("augentia.tool.session_send")

_POST_MESSAGE_TIMEOUT = 15


@register_tool
class SessionSendTool(BaseTool):
    name = "session_send"
    description = (
        "Send a message to another agent session through the gateway. "
        "Use this to deliver results, ask follow-up questions, or hand off "
        "work to another session. The target session must exist; a stopped "
        "session is restarted automatically. The message will be processed "
        "asynchronously — this call queues the message and returns immediately, "
        "without the target's reply. If the target is in dispatch mode, the "
        "message runs in a new branch of it and the result is joined back.\n\n"
        "Use this when you need to communicate cross-session, for example "
        "sending research findings back to a coordinator agent, or delivering "
        "a sub-result to a parent session."
    )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "target_session_id": {
                    "type": "string",
                    "description": (
                        "The UUID of the target session to send the message to."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "The message text to deliver to the target session.",
                },
            },
            "required": ["target_session_id", "content"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        target_session_id: str,
        content: str,
    ) -> str:
        gateway_url = os.getenv("GATEWAY_URL", "http://localhost:12598")
        base = gateway_url.rstrip("/")

        body: dict = {"content": content}
        if self.session_id:
            body["from_session_id"] = self.session_id

        logger.info(
            "session_send from=%s to=%s len=%d",
            self.session_id, target_session_id, len(content),
        )

        try:
            async with httpx.AsyncClient(
                timeout=_POST_MESSAGE_TIMEOUT, trust_env=False
            ) as client:
                resp = await client.post(
                    f"{base}/api/v1/sessions/{target_session_id}/messages",
                    headers={"content-type": "application/json", **internal_auth_headers()},
                    content=json.dumps(body),
                )
        except httpx.TimeoutException:
            return (
                f"Error: Gateway timeout while sending message to session "
                f"{target_session_id}."
            )
        except httpx.ConnectError:
            return (
                f"Error: Cannot connect to gateway at {gateway_url}. "
                "Is Augentia running?"
            )
        except Exception as e:
            logger.exception("unexpected error sending to %s", target_session_id)
            return f"Error: Unexpected error sending message: {e}"

        if resp.status_code == 202:
            if _field(resp, "status") == "dispatched":
                return (
                    f"Message delivered to session {target_session_id} "
                    f"({len(content)} chars). It is in dispatch mode, so the "
                    f"message runs in new branch {_field(resp, 'branch_session_id')} "
                    "and the result is joined back into it."
                )
            return (
                f"Message delivered to session {target_session_id} "
                f"({len(content)} chars, queued)."
            )
        if resp.status_code == 404:
            return (
                f"Error: Session {target_session_id} not found."
            )
        if resp.status_code == 409:
            # The gateway says why: e.g. dispatched branches not joined back
            # yet (wait and send again), or the session could not be started.
            return (
                f"Error: Session {target_session_id} cannot take the message now: "
                f"{_field(resp, 'detail') or resp.text[:300]}"
            )
        return (
            f"Error: Gateway returned {resp.status_code}: "
            f"{resp.text[:300]}"
        )


def _field(resp: httpx.Response, key: str) -> str | None:
    """One field of the gateway's JSON response body, or None."""
    try:
        body = resp.json()
    except ValueError:
        return None
    value = body.get(key) if isinstance(body, dict) else None
    return str(value) if value is not None else None
