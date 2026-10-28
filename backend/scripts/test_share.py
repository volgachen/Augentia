"""Share endpoints against the mock DB with auth on. Run: python scripts/test_share.py (from backend/)."""
import asyncio, os, sys, tempfile
home = tempfile.mkdtemp()
os.environ.update(DB_TYPE="mock", AUGENTIA_HOME=home, AUGENTIA_ATTACHMENTS_DIR=os.path.join(home, "att"),
                  AUGENTIA_AUTH_ENABLED="true", AUGENTIA_AUTH_PASSWORD_HASH="x",
                  AUGENTIA_SESSION_SECRET="s"*40, AUGENTIA_INTERNAL_TOKEN="t"*40)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient
from app.main import app
from app.db.deps import get_db
from app.models.domain import MessageRole
from app.core.attachments import save_attachment
import base64

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
AUTH = {"Authorization": "Bearer " + "t"*40}
with TestClient(app) as c:
    db = get_db()
    agent = asyncio.run(db.list_agents())[0]
    s = asyncio.run(db.create_session(agent.id, working_dir="C:/secret", title="demo"))
    att = save_attachment(PNG)
    other = save_attachment(PNG[:-1] + b"\x00")  # different hash, not in transcript
    asyncio.run(db.add_message(s.id, MessageRole.USER, "hi", metadata={"images": [{"ref": att.ref}]}))
    asyncio.run(db.add_message(s.id, MessageRole.AGENT, "hello"))
    asyncio.run(db.add_message(s.id, MessageRole.MARK, '{"name":"turn_end"}'))

    r = c.get(f"/api/v1/sessions/{s.id}"); assert r.status_code == 401, r.status_code
    r = c.get(f"/api/v1/share/{s.id}"); assert r.status_code == 404, r.text
    r = c.get("/api/v1/share/nope"); assert r.status_code == 404
    r = c.put(f"/api/v1/sessions/{s.id}/share", json={"enabled": True}); assert r.status_code == 401
    r = c.put(f"/api/v1/sessions/{s.id}/share", json={"enabled": True}, headers=AUTH); assert r.json()["enabled"] is True, r.text
    assert c.get(f"/api/v1/sessions/{s.id}/share", headers=AUTH).json()["enabled"] is True

    r = c.get(f"/api/v1/share/{s.id}"); assert r.status_code == 200, r.text
    body = r.json()
    assert "working_dir" not in body["session"] and "system_prompt_snapshot" not in body["session"]
    assert body["session"]["title"] == "demo" and body["session"]["agent_name"] == agent.name
    assert [m["role"] for m in body["messages"]] == ["user", "agent"], body["messages"]
    assert "C:/secret" not in r.text

    assert c.get(f"/api/v1/share/{s.id}/attachments/{att.id}").status_code == 200
    assert c.get(f"/api/v1/share/{s.id}/attachments/{other.id}").status_code == 404
    assert c.get(f"/api/v1/share/{s.id}/attachments/..%2F..%2Fx").status_code == 404
    # write endpoints are not reachable through /share
    assert c.post(f"/api/v1/share/{s.id}").status_code == 405
    assert c.post(f"/api/v1/share/{s.id}/../sessions/{s.id}/messages", json={"content": "x"}).status_code in (401, 404)

    c.put(f"/api/v1/sessions/{s.id}/share", json={"enabled": False}, headers=AUTH)
    assert c.get(f"/api/v1/share/{s.id}").status_code == 404
    assert c.get(f"/api/v1/share/{s.id}/attachments/{att.id}").status_code == 404

    asyncio.run(db.soft_delete_session(s.id))
    from app.core.session_share import write_share; write_share(s.id, True)
    assert c.get(f"/api/v1/share/{s.id}").status_code == 404
print("ALL OK")
