"""Hermetic check for parent_session_id: persistence + working dir + 404.

Runs the real FastAPI app via TestClient (no network/port). Exercises the
sessions router and the mock DB with a tool_use agent.
"""
import tempfile
from pathlib import Path

import _common  # noqa: F401 — sys.path + .env

_common.use_isolated_state("augentia-spawn-")

from fastapi.testclient import TestClient

from app.main import app

API = "/api/v1/sessions"


def _pick_agent(client: TestClient) -> str:
    r = client.get("/api/v1/agents")
    r.raise_for_status()
    agents = r.json()
    tool_use = [a["id"] for a in agents if a["agent_type"] == "tool_use"]
    assert tool_use, f"no tool_use agent available: {agents}"
    return tool_use[0]


def main() -> None:
    with TestClient(app) as client, tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        agent = _pick_agent(client)
        child_source = tmp / "src"
        child_source.mkdir()
        (child_source / "note.txt").write_text("workspace file\n", encoding="utf-8")

        # 1) parent session
        r = client.post(API, json={"agent_id": agent})
        assert r.status_code == 201, (r.status_code, r.text)
        parent_id = r.json()["id"]
        print("parent created:", parent_id)

        # 2) child spawned by parent, sharing the parent's workspace source
        r = client.post(API, json={
            "agent_id": agent,
            "source_dir": str(child_source),
            "parent_session_id": parent_id,
        })
        assert r.status_code == 201, (r.status_code, r.text)
        child = r.json()
        print("child created:", child["id"], "parent_session_id=", child["parent_session_id"])
        assert child["parent_session_id"] == parent_id, child

        # 3) the child's working dir is the requested source dir
        assert child["working_dir"] == str(child_source), child

        # 4) unknown parent -> 404
        r = client.post(API, json={"agent_id": agent, "parent_session_id": "does-not-exist"})
        assert r.status_code == 404, (r.status_code, r.text)
        print("unknown parent -> 404 OK:", r.json()["detail"])

        # 5) GET child round-trips the field
        r = client.get(f"{API}/{child['id']}")
        assert r.status_code == 200 and r.json()["parent_session_id"] == parent_id

    print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
