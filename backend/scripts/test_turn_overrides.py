"""Offline checks for per-turn overrides (turn_overrides on POST /messages and /retry).

Fully isolated: DB_TYPE=sqlite on a temp file, AUGENTIA_HOME in a temp dir,
scripted model — no network, never touches backend/.env's real database.

Covers:
  - validate_turn_overrides: allowed keys, types, null only where it means "off"
  - CAgent adapter: max_steps / tool_limit_at / step_reminder apply to that
    turn only; null switches a configured setting off for the turn; the user
    row records metadata.turn_overrides
  - HTTP: POST /messages and /retry accept them (202) and the turn honors
    them; bad values -> 400

Run from backend/:  python scripts/test_turn_overrides.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="augentia-overrides-"))
# Must be set before _common loads .env (load_dotenv does not override).
os.environ["DB_TYPE"] = "sqlite"
os.environ["SQLITE_PATH"] = str(_TMP / "test.db")
os.environ["AUGENTIA_HOME"] = str(_TMP / "home")
os.environ["AUGENTIA_AUTH_ENABLED"] = "false"

import _common  # noqa: E402,F401 — sys.path + logging
from _common import ok, fail, section  # noqa: E402

from app.adapters.cagent_adapter import CAgentAdapter  # noqa: E402
from app.adapters.cagent_agent import validate_turn_overrides  # noqa: E402
from app.db.mock import MockMemoryDatabase  # noqa: E402
from app.models.domain import AgentTemplate, AgentType, MessageRole  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        ok(label)
    else:
        fail(label)
        FAILURES.append(label)


def tool_loop_model(calls: list[int]):
    """Reads notes.txt on every step, so only max_steps ends the turn.

    Appends one entry per call to `calls`, so the caller sees the step count.
    """
    async def model(*, messages, tools, tool_choice=None):
        calls.append(1)
        n = len(calls)
        return {"tool_calls": [{"id": f"c{n}", "name": "read",
                                "arguments": {"file_path": "notes.txt"}}]}
    return model


def tool_results(rows) -> list[str]:
    return [json.loads(m.content)["content"] for m in rows if m.role == MessageRole.TOOL]


# ---------------------------------------------------------------- validation

def test_validation() -> None:
    section("validate_turn_overrides")
    good = {"max_steps": 3, "step_reminder_at": None, "step_reminder": "快结束了",
            "tool_limit_at": 2, "tool_limit_message": "收尾"}
    check(validate_turn_overrides(good) == good, "all five keys accepted")
    check(validate_turn_overrides(None) == {}, "None -> no overrides")
    check(validate_turn_overrides({"tool_limit_at": None}) == {"tool_limit_at": None},
          "tool_limit_at=null accepted (off for the turn)")
    for bad, label in (
        ({"max_step": 3}, "unknown key"),
        ({"max_steps": None}, "max_steps=null"),
        ({"max_steps": 0}, "max_steps=0"),
        ({"max_steps": True}, "max_steps=true"),
        ({"max_steps": "3"}, "max_steps as string"),
        ({"step_reminder_at": 1.5}, "step_reminder_at float"),
        ({"step_reminder": ""}, "empty step_reminder"),
        ({"step_reminder": None}, "step_reminder=null"),
        ({"tool_limit_message": "  "}, "blank tool_limit_message"),
        ([1], "not an object"),
    ):
        try:
            validate_turn_overrides(bad)
        except ValueError:
            ok(f"rejected: {label}")
        else:
            fail(f"accepted: {label}")
            FAILURES.append(label)


# ---------------------------------------------------------------- adapter

async def run_turn(adapter, text, overrides=None):
    await adapter.send(text, turn_overrides=overrides)
    async for _ in adapter.stream():
        pass


async def test_adapter() -> None:
    section("CAgent adapter: overrides apply to one turn")
    db = MockMemoryDatabase()
    await db.create_agent(AgentTemplate(
        id="agent-ov", name="ov", description="", agent_type=AgentType.TOOL_USE,
        system_prompt="test", tool_names=["read"], config={}, openai_model="test-model",
    ))
    wd = _TMP / "wd"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "notes.txt").write_text("hello", encoding="utf-8")

    # max_steps: session allows 6 steps, this turn only 2; the next turn is back to 6.
    session = await db.create_session("agent-ov", working_dir=str(wd), session_id="s-max")
    calls: list[int] = []
    adapter = CAgentAdapter(db=db, tool_names=["read"], session_id=session.id, working_dir=str(wd),
                            config={"tool_approvals": {"read": False}, "max_steps": 6},
                            model_callable=tool_loop_model(calls))
    await adapter.start("test")
    try:
        await run_turn(adapter, "one", {"max_steps": 2})
        first = len(calls)
        await run_turn(adapter, "two")
        second = len(calls) - first
        check(first == 2, f"turn with max_steps=2 made 2 model calls (got {first})")
        check(second == 6, f"next turn without overrides uses the session's 6 (got {second})")
        check(adapter._agent_cls.MAX_STEPS == 6, "session agent class unchanged")
        rows = await db.get_messages(session.id)
        users = [m for m in rows if m.role == MessageRole.USER]
        check((users[0].metadata or {}).get("turn_overrides") == {"max_steps": 2},
              "user row records metadata.turn_overrides")
        check("turn_overrides" not in (users[1].metadata or {}),
              "turn without overrides records none")
    finally:
        await adapter.stop()

    # tool_limit_at + tool_limit_message + step_reminder for one turn.
    session = await db.create_session("agent-ov", working_dir=str(wd), session_id="s-limit")
    calls = []

    async def wrap_up_model(*, messages, tools, tool_choice=None):
        calls.append(1)
        last = messages[-1]
        if last.get("role") == "tool" and "收尾吧" in str(last.get("content")):
            return {"content": "done"}
        return {"tool_calls": [{"id": f"l{len(calls)}", "name": "read",
                                "arguments": {"file_path": "notes.txt"}}]}

    adapter = CAgentAdapter(db=db, tool_names=["read"], session_id=session.id, working_dir=str(wd),
                            config={"tool_approvals": {"read": False}, "max_steps": 10},
                            model_callable=wrap_up_model)
    await adapter.start("test")
    try:
        await run_turn(adapter, "go", {"tool_limit_at": 3, "tool_limit_message": "收尾吧",
                                       "step_reminder_at": 2, "step_reminder": "还剩{remain_steps_to_limit}步"})
        results = tool_results(await db.get_messages(session.id))
        check(len(calls) == 4, f"tools stopped at step 3, model wrapped up on step 4 (calls={len(calls)})")
        # read results carry line numbers ("1\thello").
        check(len(results) == 3 and results[0].endswith("hello") and "还剩" not in results[0]
              and results[1].endswith("hello\n\n还剩0步") and results[2] == "收尾吧",
              f"reminder from step 2, limit message from step 3 (results={results})")
    finally:
        await adapter.stop()

    # null switches a configured setting off for the turn.
    session = await db.create_session("agent-ov", working_dir=str(wd), session_id="s-null")
    calls = []
    adapter = CAgentAdapter(db=db, tool_names=["read"], session_id=session.id, working_dir=str(wd),
                            config={"tool_approvals": {"read": False}, "max_steps": 3,
                                    "tool_limit_at": 1, "tool_limit_message": "停"},
                            model_callable=tool_loop_model(calls))
    await adapter.start("test")
    try:
        await run_turn(adapter, "go", {"tool_limit_at": None})
        results = tool_results(await db.get_messages(session.id))
        check(len(results) == 3 and all(r.endswith("hello") for r in results), f"tool_limit_at=null: configured limit off this turn ({results})")
    finally:
        await adapter.stop()


# ---------------------------------------------------------------- HTTP

def test_http() -> None:
    section("HTTP: POST /messages and /retry with turn_overrides")
    import app.adapters.cagent_adapter as cagent_module
    from fastapi.testclient import TestClient
    from app.main import app

    calls: list[int] = []

    async def model(*, messages, tools, tool_choice=None):
        calls.append(1)
        return {"tool_calls": [{"id": f"h{len(calls)}", "name": "read",
                                "arguments": {"file_path": "notes.txt"}}]}

    original = cagent_module._make_model
    cagent_module._make_model = lambda client, name: model
    try:
        with TestClient(app) as c:
            def make_agent(name, config):
                r = c.post("/api/v1/agents", json={
                    "name": name, "agent_type": "tool_use", "system_prompt": "test",
                    "tool_names": ["read"], "config": config,
                })
                assert r.status_code == 201, r.text
                return r.json()["id"]

            def make_session(agent_id):
                wd = _TMP / f"http-{agent_id}"
                wd.mkdir(parents=True, exist_ok=True)
                (wd / "notes.txt").write_text("hello", encoding="utf-8")
                r = c.post("/api/v1/sessions", json={"agent_id": agent_id, "working_dir": str(wd)})
                assert r.status_code == 201, r.text
                sid = r.json()["id"]
                r = c.put(f"/api/v1/sessions/{sid}/config",
                          json={"config": {"tool_approvals": {"read": False}}})
                assert r.status_code == 200, r.text
                return sid

            def wait_idle(sid, n_user):
                deadline = time.time() + 15
                while time.time() < deadline:
                    msgs = c.get(f"/api/v1/sessions/{sid}/messages").json()
                    status = c.get(f"/api/v1/sessions/{sid}").json()["status"]
                    if status == "WAITING_USER" and sum(m["role"] == "user" for m in msgs) >= n_user \
                            and any(m["role"] == "tool" for m in msgs):
                        return msgs
                    time.sleep(0.1)
                raise AssertionError(f"session {sid} did not finish")

            sid = make_session(make_agent("ov-http", {"max_steps": 5}))
            r = c.post(f"/api/v1/sessions/{sid}/messages",
                       json={"content": "go", "turn_overrides": {"max_steps": 2}})
            check(r.status_code == 202, f"POST with turn_overrides -> 202 (got {r.status_code})")
            msgs = wait_idle(sid, 1)
            check(len(calls) == 2, f"turn ran 2 steps, not the agent's 5 (got {len(calls)})")
            user = next(m for m in msgs if m["role"] == "user")
            check(user["metadata"].get("turn_overrides") == {"max_steps": 2},
                  "message listing exposes metadata.turn_overrides")

            calls.clear()
            r = c.post(f"/api/v1/sessions/{sid}/messages/{user['id']}/retry",
                       json={"content": "again", "turn_overrides": {"max_steps": 3}})
            check(r.status_code == 202, f"retry with turn_overrides -> 202 (got {r.status_code})")
            msgs = wait_idle(sid, 1)
            check(len(calls) == 3, f"retried turn ran 3 steps (got {len(calls)})")
            retried = next(m for m in msgs if m["role"] == "user")
            check(retried["metadata"].get("turn_overrides") == {"max_steps": 3},
                  "retried row records the new overrides")

            calls.clear()
            r = c.post(f"/api/v1/sessions/{sid}/messages/{retried['id']}/retry", json={"content": "plain"})
            msgs = wait_idle(sid, 1)
            plain = next(m for m in msgs if m["role"] == "user")
            check(r.status_code == 202 and len(calls) == 5 and "turn_overrides" not in plain["metadata"],
                  f"retry without overrides does not inherit them (steps={len(calls)})")

            r = c.post(f"/api/v1/sessions/{sid}/messages",
                       json={"content": "go", "turn_overrides": {"max_steps": 0}})
            check(r.status_code == 400 and "max_steps" in r.json()["detail"], "invalid value -> 400")
            r = c.post(f"/api/v1/sessions/{sid}/messages",
                       json={"content": "go", "turn_overrides": {"tool_reminder": "x"}})
            check(r.status_code == 400 and "unknown" in r.json()["detail"], "unknown key -> 400")
            r = c.post(f"/api/v1/sessions/{sid}/messages/{plain['id']}/retry",
                       json={"content": "x", "turn_overrides": {"max_steps": -1}})
            check(r.status_code == 400, "invalid value on retry -> 400")
            msgs = c.get(f"/api/v1/sessions/{sid}/messages").json()
            check(any(m["id"] == plain["id"] for m in msgs),
                  "rejected retry deleted nothing")

    finally:
        cagent_module._make_model = original


if __name__ == "__main__":
    test_validation()
    asyncio.run(test_adapter())
    test_http()  # TestClient runs its own event loop; keep it outside asyncio.run
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        sys.exit(1)
    print("\nALL TURN OVERRIDE CHECKS PASSED")
