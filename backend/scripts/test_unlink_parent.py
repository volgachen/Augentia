"""Offline checks for DELETE /sessions/{id}/parent (unbind a child session).

Isolated: temp SQLite + AUGENTIA_HOME via use_isolated_state, no network.
Also drives MockMemoryDatabase.update_session_parent directly.

Run from backend/:  python scripts/test_unlink_parent.py
"""
import asyncio
import sys

import _common  # noqa: F401 — sys.path + .env
from _common import fail, ok, section

_common.use_isolated_state("augentia-unlink-")

from fastapi.testclient import TestClient  # noqa: E402

from app.db.mock import MockMemoryDatabase  # noqa: E402
from app.main import app  # noqa: E402
from app.models.domain import AgentTemplate, AgentType  # noqa: E402

API = "/api/v1/sessions"
FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        ok(label)
    else:
        fail(label)
        FAILURES.append(label)


def test_http() -> None:
    section("HTTP: DELETE /sessions/{id}/parent")
    with TestClient(app) as c:
        r = c.post("/api/v1/agents", json={
            "name": "unlink", "agent_type": "tool_use", "system_prompt": "t", "tool_names": [],
        })
        assert r.status_code == 201, r.text
        agent = r.json()["id"]
        parent = c.post(API, json={"agent_id": agent}).json()
        child = c.post(API, json={"agent_id": agent, "parent_session_id": parent["id"]}).json()
        check(child["parent_session_id"] == parent["id"], "child starts linked to its parent")

        r = c.delete(f"{API}/{child['id']}/parent")
        check(r.status_code == 200 and r.json()["parent_session_id"] is None,
              f"unlink -> 200 with parent_session_id null (got {r.status_code})")
        listed = {s["id"]: s for s in c.get(API).json()}
        check(listed[child["id"]]["parent_session_id"] is None, "session list shows it unlinked")
        check(parent["id"] in listed and listed[child["id"]]["working_dir"] == child["working_dir"],
              "parent kept, child's working dir unchanged")

        r = c.delete(f"{API}/{child['id']}/parent")
        check(r.status_code == 200 and r.json()["parent_session_id"] is None,
              "unlinking a session without a parent is a no-op 200")
        r = c.delete(f"{API}/does-not-exist/parent")
        check(r.status_code == 404, f"unknown session -> 404 (got {r.status_code})")


async def test_mock_db() -> None:
    section("MockMemoryDatabase.update_session_parent")
    db = MockMemoryDatabase()
    await db.create_agent(AgentTemplate(id="a", name="a", description="", agent_type=AgentType.TOOL_USE,
                                        system_prompt="t", tool_names=[]))
    parent = await db.create_session("a", session_id="p")
    child = await db.create_session("a", session_id="c", parent_session_id=parent.id)
    updated = await db.update_session_parent(child.id, None)
    check(updated.parent_session_id is None and (await db.get_session("c")).parent_session_id is None,
          "mock clears the parent")
    try:
        await db.update_session_parent("missing", None)
    except KeyError:
        ok("mock: unknown session -> KeyError")
    else:
        fail("mock: unknown session accepted")
        FAILURES.append("mock unknown")


if __name__ == "__main__":
    test_http()
    asyncio.run(test_mock_db())
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        sys.exit(1)
    print("\nALL UNLINK PARENT CHECKS PASSED")
