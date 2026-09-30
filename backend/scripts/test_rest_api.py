"""REST smoke test — requires `uvicorn app.main:app` running on :12598.

Exercises: list agents -> create session -> get session -> get messages -> delete.
Picks the first tool_use agent from the registry automatically.
Set AUGENTIA_AGENT_ID to override the target agent.
"""
import os
import sys
import httpx

from _common import BASE_URL, ok, fail, info, section


def main() -> int:
    section("REST API smoke test")
    info(f"Base URL: {BASE_URL}")

    try:
        with httpx.Client(base_url=BASE_URL, timeout=30) as c:
            r = c.get("/health")
            r.raise_for_status()
            ok(f"GET /health -> {r.json()}")

            r = c.get("/api/v1/agents")
            r.raise_for_status()
            agents = r.json()
            ok(f"GET /api/v1/agents -> {len(agents)} agents")
            for a in agents:
                info(f"{a['id']}  ({a['agent_type']})  {a['name']}")

            agent_id = os.getenv("AUGENTIA_AGENT_ID")
            if not agent_id:
                tool_use = [a["id"] for a in agents if a["agent_type"] == "tool_use"]
                if not tool_use:
                    fail("no tool_use agent available to create a session with")
                    return 1
                agent_id = tool_use[0]
            info(f"Using agent: {agent_id}")

            r = c.post("/api/v1/sessions", json={"agent_id": agent_id})
            if r.status_code != 201:
                fail(f"POST /api/v1/sessions -> {r.status_code}: {r.text}")
                return 1
            session = r.json()
            session_id = session["id"]
            ok(f"POST /api/v1/sessions -> {session_id} (status={session['status']})")

            r = c.get(f"/api/v1/sessions/{session_id}")
            r.raise_for_status()
            ok(f"GET /api/v1/sessions/{{id}} -> status={r.json()['status']}")

            r = c.get(f"/api/v1/sessions/{session_id}/messages")
            r.raise_for_status()
            msgs = r.json()
            ok(f"GET messages -> {len(msgs)} message(s)")
            for m in msgs:
                info(f"[{m['role']}] {m['content'][:80]}")

            r = c.delete(f"/api/v1/sessions/{session_id}")
            if r.status_code != 204:
                fail(f"DELETE -> {r.status_code}: {r.text}")
                return 1
            ok("DELETE /api/v1/sessions/{id} -> 204")

            r = c.get(f"/api/v1/sessions/{session_id}")
            ok(f"GET after delete -> status={r.json().get('status')}")

    except httpx.ConnectError as e:
        fail(f"Cannot reach {BASE_URL} — is uvicorn running? ({e})")
        return 2
    except httpx.HTTPStatusError as e:
        fail(f"HTTP error: {e.response.status_code} {e.response.text}")
        return 1

    print("\nAll REST checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
