import asyncio
import logging
import os
import httpx
from app.auth import internal_auth_headers
from app.adapters.tools.base import BaseTool
from app.adapters.tools.registry import register_tool

logger = logging.getLogger("augentia.tool.list_subagents")

_LIST_TIMEOUT = 15
_MESSAGES_TIMEOUT = 15
_TASK_TRUNCATE = 240


@register_tool
class ListSubagentsTool(BaseTool):
    name = "list_subagents"
    requires_approval = False
    description = (
        "List all subagent sessions spawned from the current session. Returns "
        "each subagent's id, status, agent template, working directory, and "
        "the first user message (the task it was given) so you can tell at a "
        "glance what each one is working on.\n\n"
        "Use this to check progress on subagents you launched with the "
        "`subagent` tool, decide which to wait on, or pick a target id for "
        "`session_send`.\n\n"
        "Status values: INITIALIZING, RUNNING, WAITING_USER, COMPLETED, "
        "ERROR. RUNNING = still generating; WAITING_USER = finished its turn "
        "and is idle. Takes no parameters."
    )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    async def execute(self) -> str:
        parent_session_id = self.session_id
        if not parent_session_id:
            return (
                "Error: list_subagents has no parent session id — this tool "
                "must be called from inside a live session."
            )
        gateway_url = os.getenv("GATEWAY_URL", "http://localhost:12598")
        base = gateway_url.rstrip("/")

        # trust_env=False: loopback to our own gateway must bypass system/VPN
        # proxies, which would otherwise intercept localhost and 502.
        try:
            async with httpx.AsyncClient(
                timeout=_LIST_TIMEOUT, trust_env=False
            ) as client:
                resp = await client.get(
                    f"{base}/api/v1/sessions", headers=internal_auth_headers()
                )
        except httpx.TimeoutException:
            return "Error: Gateway timeout while listing sessions."
        except httpx.ConnectError:
            return (
                f"Error: Cannot connect to gateway at {gateway_url}. "
                "Is Augentia running?"
            )
        except Exception as e:
            logger.exception("unexpected error listing sessions")
            return f"Error: Unexpected error listing sessions: {e}"

        if resp.status_code != 200:
            return (
                f"Error: Gateway returned {resp.status_code}: "
                f"{resp.text[:300]}"
            )

        try:
            sessions = resp.json()
        except ValueError:
            return f"Error: Gateway returned non-JSON: {resp.text[:300]}"

        children = [
            s for s in sessions
            if s.get("parent_session_id") == parent_session_id
        ]
        if not children:
            return f"No subagents found for session {parent_session_id}."

        # Fetch first-user-message in parallel — one extra GET per child. Bound
        # by gather; failures degrade to "(no task recorded)" rather than
        # failing the whole listing.
        async with httpx.AsyncClient(
            timeout=_MESSAGES_TIMEOUT, trust_env=False
        ) as client:
            tasks = await asyncio.gather(
                *[
                    self._first_user_message(client, base, c["id"])
                    for c in children
                ],
                return_exceptions=True,
            )

        # Most recent first — agents usually care about what they just spawned.
        ordered = sorted(
            zip(children, tasks),
            key=lambda pair: pair[0].get("created_at") or "",
            reverse=True,
        )

        lines: list[str] = [
            f"Subagents of session {parent_session_id} "
            f"({len(children)} total):",
            "",
        ]
        for child, task in ordered:
            sid = child.get("id", "?")
            status = child.get("status", "?")
            agent = child.get("agent_id", "?")
            working_dir = child.get("working_dir") or "—"
            created = child.get("created_at", "?")

            if isinstance(task, BaseException) or not task:
                task_text = "(no task recorded)"
            else:
                task_text = task
            if len(task_text) > _TASK_TRUNCATE:
                task_text = task_text[: _TASK_TRUNCATE - 3] + "..."
            # Collapse newlines so each subagent stays on a tidy block.
            task_text = " ".join(task_text.split())

            lines.append(f"- session_id: {sid}")
            lines.append(f"  status: {status}")
            lines.append(f"  agent_id: {agent}")
            lines.append(f"  working_dir: {working_dir}")
            lines.append(f"  created_at: {created}")
            lines.append(f"  task: {task_text}")
            lines.append("")

        lines.append(
            "Fetch full history with GET /api/v1/sessions/<id>/messages, or "
            "send a follow-up via the session_send tool."
        )
        return "\n".join(lines).rstrip()

    @staticmethod
    async def _first_user_message(
        client: httpx.AsyncClient, base: str, session_id: str
    ) -> str | None:
        # The first user-role message is the task that kicked the subagent off
        # (subagent tool POSTs it right after creating the session). We strip
        # the "[from-session:<id>] " prefix the runner adds for cross-session
        # deliveries so the displayed task reads naturally.
        try:
            resp = await client.get(
                f"{base}/api/v1/sessions/{session_id}/messages",
                headers=internal_auth_headers(),
            )
        except Exception:
            logger.exception("failed to fetch messages for %s", session_id)
            return None
        if resp.status_code != 200:
            return None
        try:
            messages = resp.json()
        except ValueError:
            return None
        for m in messages:
            if m.get("role") == "user":
                content = m.get("content") or ""
                if content.startswith("[from-session:"):
                    end = content.find("] ")
                    if end != -1:
                        content = content[end + 2 :]
                return content
        return None
