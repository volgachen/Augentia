"""PUT /sessions/{id}/working-dir against the mock DB. Run: python scripts/test_working_dir.py (from backend/)."""
import asyncio, os, sys, tempfile
home = tempfile.mkdtemp()
os.environ.update(DB_TYPE="mock", AUGENTIA_HOME=home, AUGENTIA_ATTACHMENTS_DIR=os.path.join(home, "att"),
                  AUGENTIA_AUTH_ENABLED="true", AUGENTIA_AUTH_PASSWORD_HASH="x",
                  AUGENTIA_SESSION_SECRET="s"*40, AUGENTIA_INTERNAL_TOKEN="t"*40,
                  OPENAI_API_KEY=os.environ.get("OPENAI_API_KEY", "sk-test"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.db.deps import get_db
from app.adapters.registry import get_registry
from app.core.session_prompt import replace_runtime_working_dir, _display_path
from app.models.domain import AgentType, MessageRole, SessionStatus

AUTH = {"Authorization": "Bearer " + "t"*40}
dir_a = Path(tempfile.mkdtemp(dir=home)).resolve()
dir_b = Path(tempfile.mkdtemp(dir=home)).resolve()

# helper: only the runtime-context line naming the old dir is rewritten
P = "base\n- Current working directory: /keep\n# Runtime context\n- Operating system: x\n- Current working directory: /old\n- Session start time: t"
out = replace_runtime_working_dir(P, "/old", "/new")
assert out == P.replace("directory: /old", "directory: /new"), out
assert "directory: /keep" in out
assert replace_runtime_working_dir(P, "/other", "/new") is None      # hand-edited line
assert replace_runtime_working_dir("no context", "/old", "/new") is None
assert replace_runtime_working_dir(P, None, "/new") is not None       # created without a dir

with TestClient(app) as c:
    db = get_db()
    registry = get_registry()
    agent = next(a for a in asyncio.run(db.list_agents()) if a.agent_type == AgentType.TOOL_USE)
    r = c.post("/api/v1/sessions", json={"agent_id": agent.id, "source_dir": str(dir_a),
                                         "create_mode": "use_existing_directory"}, headers=AUTH)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    asyncio.run(db.add_message(sid, MessageRole.USER, "hi"))
    old_runner = registry.get(sid)
    snap = asyncio.run(db.get_session(sid)).system_prompt_snapshot
    has_ctx = f"- Current working directory: {_display_path(str(dir_a))}" in snap
    url = f"/api/v1/sessions/{sid}/working-dir"

    assert c.put(url, json={"working_dir": str(dir_b)}).status_code == 401
    assert c.put(url, json={"working_dir": "  "}, headers=AUTH).status_code == 400
    assert c.put(url, json={"working_dir": str(dir_b / "missing")}, headers=AUTH).status_code == 400
    assert c.put("/api/v1/sessions/nope/working-dir", json={"working_dir": str(dir_b)}, headers=AUTH).status_code == 404

    # busy -> 409, nothing changes
    asyncio.run(db.update_session_status(sid, SessionStatus.RUNNING))
    assert c.put(url, json={"working_dir": str(dir_b)}, headers=AUTH).status_code == 409
    asyncio.run(db.update_session_status(sid, SessionStatus.WAITING_USER))
    assert asyncio.run(db.get_session(sid)).working_dir == str(dir_a)

    r = c.put(url, json={"working_dir": str(dir_b)}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session"]["working_dir"] == str(dir_b), body
    assert body["prompt_updated"] is has_ctx, (body, has_ctx)
    s = asyncio.run(db.get_session(sid))
    if has_ctx:
        assert f"- Current working directory: {_display_path(str(dir_b))}" in s.system_prompt_snapshot
        assert _display_path(str(dir_a)) not in s.system_prompt_snapshot
        assert len(asyncio.run(db.system_prompt_history(s.system_prompt_id))) == 2
    new_runner = registry.get(sid)
    assert new_runner is not old_runner
    assert all(t.working_dir == str(dir_b) for t in new_runner._adapter.tools)
    assert [m.content for m in asyncio.run(db.get_messages(sid)) if m.role == MessageRole.USER] == ["hi"]

    # update_prompt=false keeps the prompt
    before = asyncio.run(db.get_session(sid)).system_prompt_snapshot
    r = c.put(url, json={"working_dir": str(dir_a), "update_prompt": False}, headers=AUTH)
    assert r.status_code == 200 and r.json()["prompt_updated"] is False, r.text
    s = asyncio.run(db.get_session(sid))
    assert s.working_dir == str(dir_a) and s.system_prompt_snapshot == before

    # prompt line no longer matches the dir -> left alone
    r = c.put(url, json={"working_dir": str(dir_b)}, headers=AUTH)
    assert r.json()["prompt_updated"] is False, r.text

    # no live runner: just persisted, nothing rebuilt
    c.portal.call(registry.remove, sid)  # on the app's loop, where the runner task lives
    r = c.put(url, json={"working_dir": str(dir_a)}, headers=AUTH)
    assert r.status_code == 200, r.text
    try:
        registry.get(sid); raise AssertionError("runner should not be rebuilt")
    except KeyError:
        pass
print("ALL OK")
