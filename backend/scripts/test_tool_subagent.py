"""Hermetic check for the `subagent` tool — git worktree isolation + spawn.

No network, no server, no real git remote. Exercises:
  - SubagentTool._make_worktree (real git worktree, and non-git fallback)
  - SubagentTool.execute end-to-end, with httpx.AsyncClient redirected to the
    in-process FastAPI app (ASGITransport) so the tool drives the real sessions
    router + mock DB. Asserts the child records its parent, gets its own
    worktree, and that no .env is cloned into it.

Requires `git` in PATH (for the worktree case).
"""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import _common  # noqa: F401 — sys.path + .env
from _common import ok, fail, info, section

_common.use_isolated_state("augentia-subagent-")

import httpx
from app.main import app
from app.adapters.tools.subagent import SubagentTool


async def _ensure_agent(client: httpx.AsyncClient) -> str:
    """Create a minimal tool-use agent template; return its id."""
    r = await client.post("/api/v1/agents", json={
        "name": "subagent-e2e",
        "description": "tool-use agent for the subagent e2e check",
        "agent_type": "tool_use",
        "system_prompt": "You are a test agent.",
        "tool_names": ["read"],
    })
    assert r.status_code == 201, (r.status_code, r.text)
    return r.json()["id"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text("hello from parent\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")


async def main() -> int:
    section("subagent tool: worktree isolation + spawn (hermetic)")
    tool = SubagentTool()
    failures = 0

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        td = Path(td)
        wt_root = td / "worktrees"
        os.environ["AUGENTIA_WORKTREE_ROOT"] = str(wt_root)

        parent_repo = td / "parent"
        _make_repo(parent_repo)

        # ---- _make_worktree: git repo parent ----
        section("_make_worktree (git repo parent)")
        work_dir, branch = await tool._make_worktree(str(parent_repo))
        if branch and branch.startswith("subagent/"):
            ok(f"created branch {branch}")
        else:
            fail(f"expected subagent/<id> branch, got {branch!r}"); failures += 1
        if Path(work_dir).is_dir() and (Path(work_dir) / "README.md").exists():
            ok("worktree sees parent's committed files")
        else:
            fail("worktree missing or does not see parent files"); failures += 1
        if str(wt_root) in os.path.abspath(work_dir):
            ok("worktree lives under AUGENTIA_WORKTREE_ROOT")
        else:
            fail(f"worktree not under configured root: {work_dir}"); failures += 1
        # clean up the worktree so the temp dir can be removed
        if branch:
            _git(parent_repo, "worktree", "remove", "--force", work_dir)
            _git(parent_repo, "branch", "-D", branch)

        # ---- _make_worktree: non-git parent -> fallback ----
        section("_make_worktree (non-git parent -> fallback)")
        nongit = td / "plain"
        nongit.mkdir()
        wd2, b2 = await tool._make_worktree(str(nongit))
        if b2 is None and Path(wd2).is_dir() and not os.listdir(wd2):
            ok("non-git parent falls back to empty scratch dir, branch=None")
        else:
            fail(f"fallback wrong: branch={b2!r} dir={wd2}"); failures += 1
        wd3, b3 = await tool._make_worktree(None)
        if b3 is None and Path(wd3).is_dir():
            ok("no parent dir falls back to scratch dir")
        else:
            fail(f"no-parent fallback wrong: branch={b3!r}"); failures += 1

        # ---- execute end-to-end (httpx -> in-process app) ----
        section("execute end-to-end (worktree spawn through the router)")
        failures += await _execute_e2e(tool, td)

    if failures:
        print(f"\n{failures} CHECK(S) FAILED")
        return 1
    print("\nALL SUBAGENT CHECKS PASSED")
    return 0


async def _execute_e2e(tool: SubagentTool, td: Path) -> int:
    # Redirect every httpx.AsyncClient the tool creates to the ASGI app, so
    # GET /sessions/{parent} and POST /sessions / messages hit the real router.
    # Uses a tool-use agent so the spawned session's adapter starts in-process.
    transport = httpx.ASGITransport(app=app)
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=transport, base_url="http://test", **kwargs)

    parent_repo = td / "e2e_parent"
    _make_repo(parent_repo)

    import app.adapters.tools.subagent as sa
    failures = 0
    try:
        sa.httpx.AsyncClient = patched  # type: ignore[assignment]

        async with real_client(transport=transport, base_url="http://test") as c:
            agent_id = await _ensure_agent(c)
            r = await c.post("/api/v1/sessions", json={
                "agent_id": agent_id, "source_dir": str(parent_repo),
            })
            assert r.status_code == 201, (r.status_code, r.text)
            parent_id = r.json()["id"]
        info(f"parent session: {parent_id}")

        # A parent .env with real config: the child must NOT get a copy of it.
        # Runtime config now reaches children through the backend process env,
        # not by cloning files into the working dir.
        (parent_repo / ".env").write_text(
            "OPENAI_API_KEY=sk-e2e\nOPENAI_MODEL=gpt-4o\n", encoding="utf-8"
        )

        tool.session_id = parent_id
        result = await tool.execute(
            agent_id=agent_id, task="say hi", isolation="worktree",
        )
        info(result.splitlines()[0])

        # Pull the child id out of the result text and inspect its working dir.
        child_id = None
        for line in result.splitlines():
            if "session_id:" in line:
                child_id = line.split("session_id:")[1].strip()
                break
        if not child_id:
            fail(f"could not parse child session id from result:\n{result}")
            return failures + 1

        async with real_client(transport=transport, base_url="http://test") as c:
            r = await c.get(f"/api/v1/sessions/{child_id}")
            assert r.status_code == 200, (r.status_code, r.text)
            child = r.json()
            child_wd = child["working_dir"]
        info(f"child working_dir: {child_wd}")

        if child["parent_session_id"] == parent_id:
            ok("child records parent_session_id")
        else:
            fail(f"child parent_session_id={child['parent_session_id']!r}"); failures += 1
        if Path(child_wd) != Path(str(parent_repo)) and Path(child_wd).is_dir():
            ok("child got its own worktree dir")
        else:
            fail(f"worktree dir wrong: {child_wd}"); failures += 1
        # No .env is cloned or synthesized any more — the parent's secrets stay put.
        if not (Path(child_wd) / ".env").exists():
            ok("no .env written into the child working dir")
        else:
            fail("child working dir unexpectedly has a .env"); failures += 1

        # Clean up the worktree git created during execute().
        child_wd_path = Path(child_wd)
        if (child_wd_path / ".git").exists():
            try:
                _git(parent_repo, "worktree", "remove", "--force", str(child_wd_path))
            except subprocess.CalledProcessError:
                pass
    finally:
        sa.httpx.AsyncClient = real_client  # type: ignore[assignment]

    return failures


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
