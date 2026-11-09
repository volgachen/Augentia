"""Offline checks for system prompt versioning (table system_prompts).

Fully isolated: temp SQLite + AUGENTIA_HOME, scripted model, no network.

Covers:
  - creating an agent stores its prompt as a first version; editing it adds
    a version chained to the previous one; an unchanged prompt adds none
  - a session's effective prompt is its own first version; each PUT
    /system-prompt adds one, GET .../system-prompt/history lists them
    newest first
  - every model reply records metadata.system_prompt_id: the version live
    when it was generated (before and after an edit, also across a runner
    rebuild)
  - diverge shares the source's current version; editing the branch chains
    a version of its own and leaves the source alone
  - opening a pre-versioning SQLite file moves the inline prompt columns
    into system_prompts with identical text

Run from backend/:  python scripts/test_system_prompt_versions.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import _common  # noqa: F401 — sys.path + .env
from _common import ok, fail, section

_common.use_isolated_state("augentia-promptver-")
os.environ["AUGENTIA_AUTH_ENABLED"] = "false"

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        ok(msg)
    else:
        fail(msg)
        FAILURES.append(msg)


def _system_of(messages: list[dict]) -> str | None:
    return next((m["content"] for m in messages if m["role"] == "system"), None)


def test_http() -> None:
    section("HTTP: versions for agents, sessions, replies, diverge")
    import app.adapters.cagent_adapter as cagent_module
    from fastapi.testclient import TestClient
    from app.main import app
    from app.adapters.registry import get_registry

    seen: list[list[dict]] = []

    async def model(*, messages, tools, tool_choice=None):
        seen.append(messages)
        return {"content": "ok"}

    original = cagent_module._make_model
    cagent_module._make_model = lambda client, name: model
    try:
        with TestClient(app) as c:
            r = c.post("/api/v1/agents", json={
                "name": "promptver", "agent_type": "tool_use",
                "system_prompt": "AGENT V1", "tool_names": [],
            })
            assert r.status_code == 201, r.text
            agent = r.json()
            agent_id = agent["id"]
            check(bool(agent.get("system_prompt_id")), "new agent has a prompt version")

            c.put(f"/api/v1/agents/{agent_id}", json={"system_prompt": "AGENT V1"})
            c.put(f"/api/v1/agents/{agent_id}", json={"system_prompt": "AGENT V2", "description": "d"})
            hist = c.get(f"/api/v1/agents/{agent_id}/system-prompt/history").json()
            check([v["content"] for v in hist] == ["AGENT V2", "AGENT V1"],
                  f"agent edits chain versions; unchanged text adds none (got {[v['content'] for v in hist]})")
            check(hist[0]["previous_id"] == hist[1]["id"] and hist[1]["previous_id"] is None,
                  "agent chain links via previous_id")
            check(c.get(f"/api/v1/agents/{agent_id}").json()["system_prompt"] == "AGENT V2",
                  "agent reads back the current version's text")

            def wait_idle(sid: str, agent_rows: int) -> list[dict]:
                deadline = time.time() + 15
                while time.time() < deadline:
                    msgs = c.get(f"/api/v1/sessions/{sid}/messages").json()
                    status = c.get(f"/api/v1/sessions/{sid}").json()["status"]
                    if status == "WAITING_USER" and sum(m["role"] == "agent" for m in msgs) >= agent_rows:
                        return msgs
                    time.sleep(0.05)
                raise AssertionError(f"session {sid} did not finish")

            def history(sid: str) -> list[dict]:
                return c.get(f"/api/v1/sessions/{sid}/system-prompt/history").json()

            r = c.post("/api/v1/sessions", json={"agent_id": agent_id})
            assert r.status_code == 201, r.text
            sid = r.json()["id"]
            first = history(sid)
            check(len(first) == 1 and first[0]["content"].startswith("AGENT V2")
                  and first[0]["previous_id"] is None,
                  "session starts with its own first version (effective prompt)")
            v1 = first[0]["id"]

            c.post(f"/api/v1/sessions/{sid}/messages", json={"content": "one"})
            msgs = wait_idle(sid, 1)
            replies = [m for m in msgs if m["role"] == "agent"]
            check(replies[-1]["metadata"].get("system_prompt_id") == v1,
                  "reply records the version it was generated with")

            r = c.put(f"/api/v1/sessions/{sid}/system-prompt", json={"system_prompt": "SESSION V2"})
            v2 = r.json().get("system_prompt_id")
            check(r.status_code == 200 and r.json()["applied"] == "live_adapter" and v2 and v2 != v1,
                  f"edit returns the new version id (got {r.text})")
            hist = history(sid)
            check([v["content"] for v in hist][:1] == ["SESSION V2"] and [v["id"] for v in hist] == [v2, v1],
                  "session history lists the edit before the first version")
            r = c.put(f"/api/v1/sessions/{sid}/system-prompt", json={"system_prompt": "SESSION V2"})
            check(len(history(sid)) == 2, "unchanged session prompt adds no version")

            c.post(f"/api/v1/sessions/{sid}/messages", json={"content": "two"})
            msgs = wait_idle(sid, 2)
            replies = [m for m in msgs if m["role"] == "agent"]
            check(_system_of(seen[-1]) == "SESSION V2", "model saw the edited prompt")
            check([m["metadata"].get("system_prompt_id") for m in replies] == [v1, v2],
                  "earlier reply keeps v1, new reply records v2")

            # Rebuilt runner (e.g. after a restart) still knows the version.
            c.portal.call(get_registry().remove, sid)
            c.post(f"/api/v1/sessions/{sid}/messages", json={"content": "three"})
            msgs = wait_idle(sid, 3)
            replies = [m for m in msgs if m["role"] == "agent"]
            check(replies[-1]["metadata"].get("system_prompt_id") == v2,
                  "rehydrated runner records the current version")

            # Diverge from the last user message: shares v2.
            last_user = [m for m in msgs if m["role"] == "user"][-1]
            r = c.post(f"/api/v1/sessions/{sid}/messages/{last_user['id']}/diverge", json={})
            assert r.status_code == 201, r.text
            bid = r.json()["id"]
            check(r.json()["system_prompt_id"] == v2 and r.json()["system_prompt_snapshot"] == "SESSION V2",
                  "branch shares the source's current version")
            r = c.put(f"/api/v1/sessions/{bid}/system-prompt", json={"system_prompt": "BRANCH V3"})
            bhist = history(bid)
            check([v["content"] for v in bhist] == ["BRANCH V3", "SESSION V2", hist[-1]["content"]],
                  "branch history walks back through the shared versions")
            check(c.get(f"/api/v1/sessions/{sid}").json()["system_prompt_snapshot"] == "SESSION V2"
                  and [v["id"] for v in history(sid)] == [v2, v1],
                  "editing the branch leaves the source untouched")

            check(c.get("/api/v1/sessions/nope/system-prompt/history").status_code == 404,
                  "unknown session history -> 404")
            check(c.get("/api/v1/agents/nope/system-prompt/history").status_code == 404,
                  "unknown agent history -> 404")
    finally:
        cagent_module._make_model = original


def test_sqlite_migration() -> None:
    section("SQLite: pre-versioning database is migrated on open")
    from app.db.sqlite import SqliteDatabase

    path = Path(tempfile.mkdtemp()) / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE agents (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
            agent_type TEXT NOT NULL, system_prompt TEXT NOT NULL, tool_names TEXT NOT NULL,
            config TEXT DEFAULT NULL, openai_model TEXT NOT NULL DEFAULT 'gpt-4o',
            openai_base_url TEXT DEFAULT NULL, created_at TEXT NOT NULL);
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, title TEXT DEFAULT NULL,
            working_dir TEXT DEFAULT NULL, parent_session_id TEXT DEFAULT NULL,
            additional_prompt TEXT DEFAULT NULL, additional_prompt_path TEXT DEFAULT NULL,
            system_prompt_snapshot TEXT DEFAULT NULL,
            status TEXT NOT NULL DEFAULT 'INITIALIZING', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, last_message_at TEXT DEFAULT NULL, deleted_at TEXT DEFAULT NULL);
        CREATE INDEX idx_sessions_agent ON sessions (agent_id);
        INSERT INTO agents VALUES ('a-old', 'old', '', 'tool_use', 'OLD AGENT PROMPT', '[]', NULL,
                                   'gpt-4o', NULL, '2026-01-01T00:00:00+00:00');
        INSERT INTO sessions VALUES ('s-old', 'a-old', 't', NULL, NULL, NULL, NULL, 'OLD SESSION PROMPT',
                                     'WAITING_USER', '2026-01-02T00:00:00+00:00',
                                     '2026-01-02T00:00:00+00:00', NULL, NULL);
        INSERT INTO sessions VALUES ('s-none', 'a-old', 't', NULL, NULL, NULL, NULL, NULL,
                                     'WAITING_USER', '2026-01-03T00:00:00+00:00',
                                     '2026-01-03T00:00:00+00:00', NULL, NULL);
    """)
    conn.commit()
    conn.close()

    async def run():
        db = SqliteDatabase(path)
        agent = await db.get_agent("a-old")
        session = await db.get_session("s-old")
        empty = await db.get_session("s-none")
        cols = lambda t: {r[1] for r in db._conn.execute(f"PRAGMA table_info({t})")}
        check(agent.system_prompt == "OLD AGENT PROMPT" and agent.system_prompt_id,
              "agent prompt moved into a version")
        check(session.system_prompt_snapshot == "OLD SESSION PROMPT" and session.system_prompt_id,
              "session prompt moved into a version")
        check(empty.system_prompt_snapshot is None and empty.system_prompt_id is None,
              "session without a prompt stays without one")
        version = await db.get_system_prompt(session.system_prompt_id)
        check(version.created_at.isoformat().startswith("2026-01-02"),
              "migrated version keeps the session's created_at")
        check("system_prompt" not in cols("agents") and "system_prompt_snapshot" not in cols("sessions"),
              "old text columns dropped")
        check(db._conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'idx_sessions_agent'")
              .fetchone()[0] == 1, "existing index kept")
        count = db._conn.execute("SELECT count(*) FROM system_prompts").fetchone()[0]
        await db.close()
        again = SqliteDatabase(path)
        check(again._conn.execute("SELECT count(*) FROM system_prompts").fetchone()[0] == count,
              "reopening does not migrate twice")
        await again.close()

    asyncio.run(run())


if __name__ == "__main__":
    test_sqlite_migration()
    test_http()
    if FAILURES:
        fail(f"{len(FAILURES)} check(s) failed")
        sys.exit(1)
    ok("ALL SYSTEM PROMPT VERSION CHECKS PASSED")
