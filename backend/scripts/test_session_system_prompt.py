"""Offline checks for PUT /sessions/{id}/system-prompt.

Fully isolated: temp SQLite + AUGENTIA_HOME, scripted model, no network.

Covers:
  - validation: empty prompt -> 400, unknown session -> 404
  - live cagent runner: prompt swapped in place, next model request sees it,
    history kept, GET /system-prompt reports it
  - busy session (turn in flight) -> 409
  - no live runner: snapshot persisted, rehydrated runner uses it
  - adapter without in-place support: runner is dropped and rebuilt
  - legacy OpenAIToolUseAdapter.set_system_prompt swaps/inserts/drops the
    leading system message

Run from backend/:  python scripts/test_session_system_prompt.py
"""
import asyncio
import os
import sys
import threading
import time

import _common  # noqa: F401 — sys.path + .env
from _common import ok, fail, section

_common.use_isolated_state("augentia-sysprompt-")
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
    section("HTTP: PUT /sessions/{id}/system-prompt")
    import app.adapters.cagent_adapter as cagent_module
    from fastapi.testclient import TestClient
    from app.main import app
    from app.adapters.registry import get_registry

    seen: list[list[dict]] = []
    gate = threading.Event()
    gate.set()

    async def model(*, messages, tools, tool_choice=None):
        seen.append(messages)
        while not gate.is_set():
            await asyncio.sleep(0.02)
        return {"content": "ok"}

    original = cagent_module._make_model
    cagent_module._make_model = lambda client, name: model
    try:
        with TestClient(app) as c:
            r = c.post("/api/v1/agents", json={
                "name": "sysprompt", "agent_type": "tool_use",
                "system_prompt": "ORIGINAL PROMPT", "tool_names": [],
            })
            assert r.status_code == 201, r.text
            agent_id = r.json()["id"]

            def make_session() -> str:
                r = c.post("/api/v1/sessions", json={"agent_id": agent_id})
                assert r.status_code == 201, r.text
                return r.json()["id"]

            def wait_idle(sid: str, agent_rows: int) -> list[dict]:
                deadline = time.time() + 15
                while time.time() < deadline:
                    msgs = c.get(f"/api/v1/sessions/{sid}/messages").json()
                    status = c.get(f"/api/v1/sessions/{sid}").json()["status"]
                    if status == "WAITING_USER" and sum(m["role"] == "agent" for m in msgs) >= agent_rows:
                        return msgs
                    time.sleep(0.05)
                raise AssertionError(f"session {sid} did not finish")

            def put(sid: str, prompt: str):
                return c.put(f"/api/v1/sessions/{sid}/system-prompt", json={"system_prompt": prompt})

            sid = make_session()
            check(put(sid, "   ").status_code == 400, "empty prompt -> 400")
            check(put("no-such-session", "x").status_code == 404, "unknown session -> 404")

            # First turn with the original prompt.
            c.post(f"/api/v1/sessions/{sid}/messages", json={"content": "hello"})
            wait_idle(sid, 1)
            check((_system_of(seen[-1]) or "").startswith("ORIGINAL PROMPT"),
                  "first turn uses the original prompt")

            # Live swap.
            r = put(sid, "NEW PROMPT")
            check(r.status_code == 200 and r.json()["applied"] == "live_adapter",
                  f"live runner swapped in place (got {r.status_code} {r.text})")
            g = c.get(f"/api/v1/sessions/{sid}/system-prompt").json()
            check(g == {"system_prompt": "NEW PROMPT", "source": "live_adapter"},
                  "GET reports the new live prompt")
            check(c.get(f"/api/v1/sessions/{sid}").json()["system_prompt_snapshot"] == "NEW PROMPT",
                  "snapshot persisted")
            c.post(f"/api/v1/sessions/{sid}/messages", json={"content": "again"})
            wait_idle(sid, 2)
            last = seen[-1]
            check(_system_of(last) == "NEW PROMPT", "next model request uses the new prompt")
            check(any(m["role"] == "user" and m["content"] == "hello" for m in last),
                  "history from before the change is still sent")

            # Busy -> 409.
            gate.clear()
            c.post(f"/api/v1/sessions/{sid}/messages", json={"content": "slow"})
            deadline = time.time() + 5
            while time.time() < deadline and c.get(f"/api/v1/sessions/{sid}").json()["status"] != "RUNNING":
                time.sleep(0.02)
            check(put(sid, "DURING TURN").status_code == 409, "running session -> 409")
            gate.set()
            wait_idle(sid, 3)
            check(c.get(f"/api/v1/sessions/{sid}").json()["system_prompt_snapshot"] == "NEW PROMPT",
                  "rejected update left the snapshot alone")

            # No live runner: persist only, rehydrate picks it up.
            c.portal.call(get_registry().remove, sid)  # on the app's event loop
            r = put(sid, "OFFLINE PROMPT")
            check(r.status_code == 200 and r.json()["applied"] == "persisted",
                  "no runner -> persisted only")
            c.post(f"/api/v1/sessions/{sid}/messages", json={"content": "after restart"})
            wait_idle(sid, 4)
            check(_system_of(seen[-1]) == "OFFLINE PROMPT", "rehydrated runner uses the persisted prompt")

            # Adapter without in-place support -> runner restarted.
            orig_set = cagent_module.CAgentAdapter.set_system_prompt

            async def unsupported(self, prompt):
                raise NotImplementedError

            cagent_module.CAgentAdapter.set_system_prompt = unsupported
            try:
                r = put(sid, "RESTART PROMPT")
                check(r.status_code == 200 and r.json()["applied"] == "runner_restarted",
                      "unsupported adapter -> runner restarted")
                try:
                    get_registry().get(sid)
                    check(False, "old runner removed from registry")
                except KeyError:
                    check(True, "old runner removed from registry")
            finally:
                cagent_module.CAgentAdapter.set_system_prompt = orig_set
            c.post(f"/api/v1/sessions/{sid}/messages", json={"content": "post restart"})
            wait_idle(sid, 5)
            check(_system_of(seen[-1]) == "RESTART PROMPT", "rebuilt runner uses the new prompt")
    finally:
        cagent_module._make_model = original


async def test_legacy_adapter() -> None:
    section("legacy adapter: set_system_prompt")
    from app.adapters.openai_tool_use import OpenAIToolUseAdapter

    a = OpenAIToolUseAdapter(tool_names=[], session_id="s", config={})
    a._messages = [{"role": "system", "content": "old"}, {"role": "user", "content": "hi"}]
    await a.set_system_prompt("new")
    check(a._messages == [{"role": "system", "content": "new"}, {"role": "user", "content": "hi"}],
          "replaces the leading system message")
    a._messages = [{"role": "user", "content": "hi"}]
    await a.set_system_prompt("added")
    check(a.current_system_prompt() == "added" and len(a._messages) == 2, "inserts when missing")
    await a.set_system_prompt("")
    check(a._messages == [{"role": "user", "content": "hi"}], "empty prompt drops it")


if __name__ == "__main__":
    asyncio.run(test_legacy_adapter())
    test_http()
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        sys.exit(1)
    print("\nALL SYSTEM PROMPT CHECKS PASSED")
