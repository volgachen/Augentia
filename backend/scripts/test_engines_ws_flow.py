"""Live WS confirm-flow test for BOTH tool-use engines (legacy + cagent).

Against a running gateway (DB_TYPE=mock is fine). For each engine: create a
session, send a prompt that triggers the gated write tool, approve the confirm,
and assert the turn completes with a persisted, pairable history.

    DB_TYPE=mock python -m uvicorn app.main:app --port 12598   # in another shell
    python scripts/test_engines_ws_flow.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
import websockets

BASE = os.getenv("AUGENTIA_BASE_URL", "http://127.0.0.1:12598/api/v1")
WS = BASE.replace("http://", "ws://").replace("https://", "wss://")

PROMPT = (
    "Use the write tool to create a file named hello-<engine>.txt containing the "
    "text 'hi from <engine> engine test'. Do it now with the write tool."
)


async def run_engine(c: httpx.AsyncClient, engine: str) -> bool:
    wd = Path(tempfile.mkdtemp(prefix=f"augentia-{engine}-"))
    if engine == "cagent":
        r = await c.post(f"{BASE}/agents", json={
            "name": f"ws-flow-{engine}",
            "description": "cagent engine WS flow test agent",
            "agent_type": "tool_use",
            "system_prompt": "You are a test agent. Use the write tool when asked.",
            "tool_names": ["write", "read"],
            "config": {"engine": "cagent"},
        })
        r.raise_for_status()
        agent_id = r.json()["id"]
    else:
        agents = (await c.get(f"{BASE}/agents")).json()
        agent_id = next(a["id"] for a in agents if a["agent_type"] == "tool_use")

    r = await c.post(f"{BASE}/sessions", json={"agent_id": agent_id, "source_dir": str(wd)})
    r.raise_for_status()
    sid = r.json()["id"]
    print(f"[{engine}] session={sid}")

    uri = f"{WS}/sessions/{sid}/stream"
    saw = {"confirm": False, "result": False, "done": False, "error": None}
    async with websockets.connect(uri, max_size=None) as ws:
        state = json.loads(await ws.recv())
        assert state["type"] == "session_state", state
        await ws.send(json.dumps({"content": PROMPT.replace("<engine>", engine)}))
        deadline = asyncio.get_event_loop().time() + 120
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
            except asyncio.TimeoutError:
                break
            frame = json.loads(raw)
            t, data = frame["type"], frame.get("data", "")
            if t == "tool_confirm":
                saw["confirm"] = True
                call_id = json.loads(data)["call_id"]
                await ws.send(json.dumps({"decision": "approve", "call_id": call_id}))
            elif t == "tool_result":
                saw["result"] = True
            elif t == "error":
                saw["error"] = str(data)[:200]
                break
            elif t == "done":
                saw["done"] = True
                break

    problems = []
    if not saw["confirm"]:
        problems.append("no tool_confirm frame (model may not have called the tool)")
    if not saw["result"]:
        problems.append("no tool_result frame")
    if not saw["done"]:
        problems.append(f"no done frame (error={saw['error']})")
    expected_file = wd / f"hello-{engine}.txt"
    if saw["confirm"] and not expected_file.exists():
        problems.append("approved write did not create the file")

    session = (await c.get(f"{BASE}/sessions/{sid}")).json()
    if session["status"] != "WAITING_USER":
        problems.append(f"final status={session['status']}")
    messages = (await c.get(f"{BASE}/sessions/{sid}/messages")).json()
    roles = [m["role"] for m in messages]
    if "mark" in roles:
        problems.append(f"mark rows leaked to clients: {roles}")
    if not any(r == "user" for r in roles):
        problems.append(f"no user row persisted: {roles}")

    await c.delete(f"{BASE}/sessions/{sid}")
    if problems:
        print(f"[{engine}] FAIL: {'; '.join(problems)}")
        return False
    print(f"[{engine}] PASS: confirm -> approve -> result -> done; roles={roles}")
    return True


async def main() -> int:
    async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
        results = [await run_engine(c, engine) for engine in ("legacy", "cagent")]
    if all(results):
        print("ALL ENGINE WS FLOWS PASSED")
        return 0
    print("SOME ENGINE WS FLOWS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
