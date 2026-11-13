"""Offline checks for the per-session model (GET/PUT /sessions/{id}/model).

Fully isolated: temp SQLite + AUGENTIA_HOME, the model factory replaced by a
recorder, no network.

Covers:
  - PUT sets model / model_params; the next turn calls that model with those
    params; null puts the agent's back
  - model_params replaces the agent's config.model_params as a whole
  - validation: reserved keys / non-object params -> 400; unknown session 404
  - busy session -> 409
  - diverge: the branch starts with the source's model
  - the value survives a rehydrate (runner rebuilt from the database)

Run from backend/:  python scripts/test_session_model.py
"""
import sys
import time

import _common  # noqa: F401 — sys.path + .env
from _common import fail, ok, section

_common.use_isolated_state("augentia-session-model-")

import app.adapters.cagent_adapter as cagent_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.adapters.registry import get_registry  # noqa: E402
from app.db.deps import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.domain import SessionStatus  # noqa: E402

API = "/api/v1/sessions"
FAILURES: list[str] = []
CALLS: list[dict] = []


def check(condition: bool, label: str) -> None:
    if condition:
        ok(label)
    else:
        fail(label)
        FAILURES.append(label)


def fake_make_model(client, name, params=None):
    async def model(*, messages, tools, tool_choice=None):
        CALLS.append({"model": name, "params": dict(params or {})})
        return {"content": f"answered by {name}"}
    return model


def main() -> None:
    original = cagent_module._make_model
    cagent_module._make_model = fake_make_model
    try:
        with TestClient(app) as c:
            r = c.post("/api/v1/agents", json={
                "name": "model-agent", "agent_type": "tool_use", "system_prompt": "t",
                "tool_names": [], "openai_model": "agent-model",
                "config": {"model_params": {"temperature": 0.9, "top_p": 0.5}},
            })
            assert r.status_code == 201, r.text
            agent = r.json()["id"]
            sid = c.post(API, json={"agent_id": agent}).json()["id"]

            def turn(text: str) -> dict:
                before = len(CALLS)
                r = c.post(f"{API}/{sid}/messages", json={"content": text})
                assert r.status_code == 202, r.text
                deadline = time.time() + 15
                while time.time() < deadline:
                    if len(CALLS) > before and c.get(f"{API}/{sid}").json()["status"] == "WAITING_USER":
                        return CALLS[-1]
                    time.sleep(0.05)
                raise AssertionError("turn did not finish")

            section("defaults: the agent's model and params")
            view = c.get(f"{API}/{sid}/model").json()
            check(view["model"] is None and view["effective_model"] == "agent-model"
                  and view["effective_model_params"] == {"temperature": 0.9, "top_p": 0.5},
                  f"GET reports the agent's values ({view})")
            call = turn("one")
            check(call == {"model": "agent-model", "params": {"temperature": 0.9, "top_p": 0.5}},
                  f"first turn uses the agent's model ({call})")

            section("PUT model + params")
            r = c.put(f"{API}/{sid}/model", json={
                "model": "  session-model ", "model_params": {"reasoning_effort": "high"}})
            body = r.json()
            check(r.status_code == 200 and body["model"] == "session-model"
                  and body["session"]["model"] == "session-model"
                  and body["effective_model_params"] == {"reasoning_effort": "high"},
                  f"PUT -> 200, trimmed model, params stored ({r.status_code})")
            call = turn("two")
            check(call == {"model": "session-model", "params": {"reasoning_effort": "high"}},
                  f"next turn uses the session's model and only its params ({call})")
            msgs = c.get(f"{API}/{sid}/messages").json()
            check(c.get(f"{API}/{sid}").json()["model"] == "session-model"
                  and sum(m["role"] == "user" for m in msgs) == 2,
                  "session shows the model; history kept")

            section("model only (params null -> agent's params)")
            r = c.put(f"{API}/{sid}/model", json={"model": "other-model", "model_params": None})
            call = turn("three")
            check(r.status_code == 200 and call == {
                "model": "other-model", "params": {"temperature": 0.9, "top_p": 0.5}},
                f"model override keeps the agent's params ({call})")

            section("rehydrate keeps it")
            c.put(f"{API}/{sid}/model", json={"model": "kept-model", "model_params": {}})
            # Drop the live runner (as a backend restart does); the next turn
            # rebuilds it from the stored session.
            c.portal.call(get_registry().remove, sid)
            call = turn("four")
            check(call == {"model": "kept-model", "params": {}},
                  f"rebuilt runner reads it back; params {{}} means none ({call})")

            section("diverge inherits")
            user = next(m for m in c.get(f"{API}/{sid}/messages").json() if m["role"] == "user")
            r = c.post(f"{API}/{sid}/messages/{user['id']}/diverge", json={})
            branch = r.json()
            check(r.status_code == 201 and branch["model"] == "kept-model" and branch["model_params"] == {},
                  f"branch starts on the source's model ({branch.get('model')}, {branch.get('model_params')})")

            section("reset to the agent")
            r = c.put(f"{API}/{sid}/model", json={"model": None, "model_params": None})
            call = turn("five")
            check(r.status_code == 200 and r.json()["model"] is None
                  and call == {"model": "agent-model", "params": {"temperature": 0.9, "top_p": 0.5}},
                  f"null/null -> agent's model and params ({call})")

            section("validation")
            r = c.put(f"{API}/{sid}/model", json={"model": "x", "model_params": {"tool_choice": "auto"}})
            check(r.status_code == 400 and "tool_choice" in r.json()["detail"], "reserved key -> 400")
            r = c.put(f"{API}/{sid}/model", json={"model": "x", "model_params": {"extra_body": 1}})
            check(r.status_code == 400, "bad extra_body -> 400")
            r = c.put(f"{API}/{sid}/model", json={"model": "x", "model_params": [1]})
            check(r.status_code == 422, f"non-object params -> 422 ({r.status_code})")
            check(c.put(f"{API}/nope/model", json={"model": "x"}).status_code == 404, "unknown -> 404")
            check(c.get(f"{API}/nope/model").status_code == 404, "GET unknown -> 404")
            check(c.get(f"{API}/{sid}").json()["model"] is None, "rejected PUTs changed nothing")

            section("busy -> 409")
            db = get_db()
            c.portal.call(db.update_session_status, sid, SessionStatus.RUNNING)
            r = c.put(f"{API}/{sid}/model", json={"model": "x"})
            check(r.status_code == 409, f"RUNNING -> 409 ({r.status_code})")
            c.portal.call(db.update_session_status, sid, SessionStatus.WAITING_USER)
    finally:
        cagent_module._make_model = original


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        sys.exit(1)
    print("\nALL SESSION MODEL CHECKS PASSED")
