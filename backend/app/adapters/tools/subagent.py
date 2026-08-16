import asyncio
import json
import logging
import os
import uuid
import httpx

from app.auth import internal_auth_headers
from app.adapters.tools.base import BaseTool
from app.adapters.tools.registry import register_tool

logger = logging.getLogger("augentia.tool.subagent")

_CREATE_SESSION_TIMEOUT = 30
_POST_MESSAGE_TIMEOUT = 15
_GET_SESSION_TIMEOUT = 15
_GIT_TIMEOUT = 30


def _worktree_root() -> str:
    # AUGENTIA_WORKTREE_ROOT lets operators relocate subagent worktrees
    # globally. Resolved at call time (not import) so the env var stays mutable.
    default = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "tmp", "sessions"
    )
    return os.getenv("AUGENTIA_WORKTREE_ROOT", default)


@register_tool
class SubagentTool(BaseTool):
    name = "subagent"
    description = (
        "Launch a new subagent session to handle a complex, multi-step task "
        "in the background. The subagent runs independently — you get back a "
        "session ID immediately and continue your work without waiting.\n\n"
        "When the subagent finishes, it will POST its results back to your "
        "session automatically.\n\n"
        "You can only launch available agent templates.\n\n"
        "Choose isolation=\"worktree\" when the subagent needs its own isolated "
        "working directory (recommended for Claude Code agents). This creates a "
        "real git worktree off your working directory on a new branch "
        "(subagent/<id>), so the subagent sees your current code and its commits "
        "land on that branch for you to merge later. If your working directory "
        "is not a git repo, it falls back to an isolated empty directory. Omit "
        "isolation to share the parent context."
    )

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": (
                        "Agent template ID. Available: 'agent-claude-code-001' "
                        "(Claude Code CLI) or 'agent-research-001' (OpenAI tool-use)."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "The task for the subagent to perform. Be specific — include "
                        "what to do, what output format you expect, and any "
                        "constraints. This is sent as the first message."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Short label (3-5 words) describing the task, for logging.",
                },
                "isolation": {
                    "type": "string",
                    "description": (
                        "'worktree' creates a git worktree off your working "
                        "directory on a new branch (subagent/<id>) so the "
                        "subagent sees your current code and commits there. Falls "
                        "back to an isolated empty directory under the worktree "
                        "root if your working directory is not a git repo. Omit to "
                        "run without filesystem isolation (tool-use agents don't "
                        "need it)."
                    ),
                    "enum": ["worktree"],
                },
            },
            "required": ["agent_id", "task"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        agent_id: str,
        task: str,
        description: str | None = None,
        isolation: str | None = None,
    ) -> str:
        parent_session_id = self.session_id
        gateway_url = os.getenv("GATEWAY_URL", "http://localhost:12598")
        base = gateway_url.rstrip("/")

        # ---- Build create-session body ----
        body: dict = {"agent_id": agent_id}
        if parent_session_id:
            body["parent_session_id"] = parent_session_id

        # The parent's working dir anchors the git worktree when isolation is
        # requested; nothing else needs it.
        parent_dir = await self._parent_working_dir(base, parent_session_id)

        work_dir: str | None = None
        branch: str | None = None
        if isolation == "worktree":
            work_dir, branch = await self._make_worktree(parent_dir)
            body["source_dir"] = work_dir
            body["create_mode"] = "use_existing_directory"
            logger.info(
                "worktree isolation: work_dir=%s branch=%s", work_dir, branch
            )
        elif parent_dir:
            body["source_dir"] = parent_dir
            body["create_mode"] = "use_existing_directory"

        label = description or f"subagent:{agent_id}"
        # Persist the task description as the session's title so spawned
        # sub-sessions are identifiable in the UI (not just an auto agent-name
        # label). Falls back to the same string used for logging.
        body["title"] = label
        logger.info(
            "launching %s agent=%s parent=%s isolation=%s",
            label, agent_id, parent_session_id, isolation or "none",
        )

        # ---- 1) Create the session ----
        # trust_env=False: these calls always target our own gateway on
        # localhost, so they must bypass any HTTP(S)_PROXY / system proxy (a VPN
        # proxy would otherwise intercept the loopback request and 502).
        try:
            async with httpx.AsyncClient(
                timeout=_CREATE_SESSION_TIMEOUT, trust_env=False
            ) as client:
                resp = await client.post(
                    f"{base}/api/v1/sessions",
                    headers={"content-type": "application/json", **internal_auth_headers()},
                    content=json.dumps(body),
                )
        except httpx.TimeoutException:
            return "Error: Gateway timeout while creating subagent session."
        except httpx.ConnectError:
            return (
                f"Error: Cannot connect to gateway at {gateway_url}. "
                "Is Augentia running?"
            )
        except Exception as e:
            logger.exception("unexpected error creating subagent session")
            return f"Error: Unexpected error creating subagent session: {e}"

        if resp.status_code != 201:
            return self._format_create_error(resp, agent_id, parent_session_id)

        session = resp.json()
        new_id = session["id"]
        logger.info("subagent session created id=%s label=%s", new_id, label)

        # ---- 2) Send the first task ----
        msg_body = {"content": task}
        try:
            async with httpx.AsyncClient(
                timeout=_POST_MESSAGE_TIMEOUT, trust_env=False
            ) as client:
                msg_resp = await client.post(
                    f"{base}/api/v1/sessions/{new_id}/messages",
                    headers={"content-type": "application/json", **internal_auth_headers()},
                    content=json.dumps(msg_body),
                )
        except httpx.TimeoutException:
            return (
                f"Session created (id={new_id}) but posting the first message "
                f"timed out. The session exists but has no task yet."
            )
        except Exception as e:
            logger.exception("unexpected error posting first message to %s", new_id)
            return (
                f"Session created (id={new_id}) but posting the first message "
                f"failed: {e}. The session exists but has no task yet."
            )

        if msg_resp.status_code != 202:
            body_text = msg_resp.text
            logger.warning(
                "post message returned %d for session=%s: %s",
                msg_resp.status_code, new_id, body_text,
            )
            return (
                f"Session created (id={new_id}) but posting first message returned "
                f"{msg_resp.status_code}: {body_text[:300]}. "
                "The session may not have received the task."
            )

        # ---- Result ----
        lines = [
            f"Subagent launched: {label}",
            f"  session_id: {new_id}",
        ]
        if parent_session_id:
            lines.append(
                f"  parent_session_id: {parent_session_id} "
                "(results will be reported back to you when done)"
            )
        lines.append(f"  agent_id: {agent_id}")
        if isolation == "worktree":
            if branch:
                lines.append(f"  isolation: worktree (git, branch={branch})")
            else:
                lines.append(
                    "  isolation: worktree (scratch dir — working directory is "
                    "not a git repo)"
                )
        elif isolation:
            lines.append(f"  isolation: {isolation}")
        if work_dir:
            lines.append(f"  working_dir: {work_dir}")
        lines.append("")
        if branch:
            lines.append(
                f"The subagent's commits land on branch '{branch}'. When it's "
                f"done, run `git merge {branch}` in your working directory to "
                "integrate them."
            )
        lines.append(f"Check progress: GET {base}/api/v1/sessions/{new_id}")
        lines.append(f"Send follow-up: POST {base}/api/v1/sessions/{new_id}/messages")

        return "\n".join(lines)

    @staticmethod
    def _format_create_error(
        resp: httpx.Response, agent_id: str, parent_session_id: str | None
    ) -> str:
        detail = "unknown error"
        try:
            detail = resp.json().get("detail", detail)
        except Exception:
            detail = resp.text[:500]

        if resp.status_code == 404:
            return (
                f"Error: {detail} (agent_id='{agent_id}' or "
                f"parent_session_id='{parent_session_id}' not found)."
            )
        if resp.status_code == 400:
            return f"Error: {detail}"
        if resp.status_code == 409:
            return f"Error: {detail}"
        logger.error("create session failed status=%d body=%s", resp.status_code, detail)
        return f"Error: Gateway returned {resp.status_code}: {detail}"

    async def _make_worktree(
        self, parent_dir: str | None
    ) -> tuple[str, str | None]:
        # Returns (work_dir, branch). branch is non-None only when we built a
        # real git worktree; otherwise we fall back to an empty scratch dir and
        # branch is None. work_dir always ends up created on disk (by git for
        # the worktree case, by os.makedirs for the fallback).
        short_id = uuid.uuid4().hex[:8]
        work_dir = os.path.abspath(os.path.join(_worktree_root(), short_id))

        if parent_dir and await self._is_git_repo(parent_dir):
            branch = f"subagent/{short_id}"
            # base = parent's current HEAD: the subagent continues from the
            # parent's working state, not from a remote ref. git creates work_dir.
            ok = await self._run_git(
                parent_dir,
                "worktree", "add", "-b", branch, work_dir, "HEAD",
            )
            if ok:
                return work_dir, branch
            logger.warning(
                "git worktree add failed for parent=%s; falling back to scratch dir",
                parent_dir,
            )

        # Fallback: isolated empty directory, no git.
        os.makedirs(work_dir, exist_ok=False)
        return work_dir, None

    async def _parent_working_dir(
        self, base: str, parent_session_id: str | None
    ) -> str | None:
        if not parent_session_id:
            return None
        try:
            async with httpx.AsyncClient(
                timeout=_GET_SESSION_TIMEOUT, trust_env=False
            ) as client:
                resp = await client.get(
                    f"{base}/api/v1/sessions/{parent_session_id}",
                    headers=internal_auth_headers(),
                )
        except Exception:
            logger.exception(
                "failed to fetch parent session %s for worktree", parent_session_id
            )
            return None
        if resp.status_code != 200:
            logger.warning(
                "parent session %s lookup returned %d", parent_session_id, resp.status_code
            )
            return None
        return resp.json().get("working_dir")

    @staticmethod
    async def _is_git_repo(path: str) -> bool:
        return await SubagentTool._run_git(
            path, "rev-parse", "--is-inside-work-tree"
        )

    @staticmethod
    async def _run_git(cwd: str, *args: str) -> bool:
        # exec (not shell) — no injection surface. Returns True on exit code 0.
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_GIT_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError):
            logger.exception("git %s in %s failed to run", " ".join(args), cwd)
            return False
        if proc.returncode != 0:
            logger.warning(
                "git %s in %s exited %s: %s",
                " ".join(args), cwd, proc.returncode,
                stderr.decode(errors="replace").strip()[:300],
            )
            return False
        return True
