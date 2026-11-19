"""Offline checks for the session_send tool's replies.

Runs the tool against a stub gateway (httpx.MockTransport), so no backend,
model or network is needed. Covers: 202 queued, 202 dispatched (dispatch
mode, names the branch), 409 relaying the gateway's reason (pending joins /
could not start), 404, and the from_session_id it sends.

Run from backend/:  python scripts/test_tool_session_send.py
"""
import asyncio
import json
import sys

import httpx

import _common  # noqa: F401 — sets sys.path so `app.*` imports resolve
from _common import ok, fail, section

import app.adapters.tools.session_send as session_send_module
from app.adapters.tools.session_send import SessionSendTool

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        ok(label)
    else:
        fail(label)
        FAILURES.append(label)


async def send_with(status: int, body, target: str = "target-1") -> tuple[str, list]:
    """Run the tool once against a gateway answering `status` / `body`."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    real_client = httpx.AsyncClient

    def stub_client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    session_send_module.httpx.AsyncClient = stub_client
    try:
        tool = SessionSendTool()
        tool.session_id = "sender-1"
        result = await tool.execute(target_session_id=target, content="hello")
    finally:
        session_send_module.httpx.AsyncClient = real_client
    return result, seen


async def main() -> int:
    section("session_send replies")

    result, seen = await send_with(202, {"status": "queued"})
    check(result.startswith("Message delivered") and "queued" in result, f"202 queued: {result!r}")
    sent = json.loads(seen[0].content)
    check(seen[0].url.path == "/api/v1/sessions/target-1/messages"
          and sent == {"content": "hello", "from_session_id": "sender-1"},
          f"posts content + from_session_id to /messages ({seen[0].url.path}, {sent})")

    result, _ = await send_with(202, {"status": "dispatched", "branch_session_id": "branch-9"})
    check(result.startswith("Message delivered") and "dispatch mode" in result
          and "branch-9" in result, f"202 dispatched names the branch: {result!r}")

    pending = "2 dispatched branch(es) not joined back yet; wait until they are"
    result, _ = await send_with(409, {"detail": pending})
    check(result.startswith("Error") and pending in result and "restarted" not in result,
          f"409 relays the pending-joins reason: {result!r}")

    result, _ = await send_with(409, {"detail": "session has no live adapter"})
    check("session has no live adapter" in result, f"409 relays the start failure: {result!r}")

    result, _ = await send_with(409, "plain text body")
    check("plain text body" in result, f"409 without JSON falls back to the body text: {result!r}")

    result, _ = await send_with(404, {"detail": "Session not found"})
    check(result == "Error: Session target-1 not found.", f"404: {result!r}")

    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        return 1
    print("\nALL SESSION_SEND CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
