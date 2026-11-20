"""Offline checks for the Responses API wire format (api_format "responses").

Drives the CAgent engine with a fake AsyncOpenAI whose responses.create and
chat.completions.create record their kwargs and answer from a script, so no
network is needed. Covers:

  - translation: instructions from the system prompt, user text / images
    (input_text / input_image with detail), assistant text (output_text),
    tool calls (function_call) and results (function_call_output: a string,
    or input_text / input_image parts in order for image results)
  - flat function tools with strict=False; tool_choice="none" for the
    summarizer; model_params merged as they are (no renaming)
  - parsing: text, tool calls, refusal, usage mapped to prompt_tokens /
    completion_tokens, finish_reason (stop / tool_calls / length), failed
    status raises
  - tool_image_mode "user_message" still moves tool images to a user message
  - format selection: agent default, session override (PUT /model), branch
    inheritance, GET effective_api_format, validation (400), the summarizer
    on its own config.compression_api_format

Run from backend/:  python scripts/test_responses_api.py
"""
import asyncio
import json
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

from openai.types.chat import ChatCompletion

import _common  # noqa: F401 — sys.path + .env
from _common import fail, ok, section, use_isolated_state

use_isolated_state("augentia-responses-api-")

import app.adapters.cagent_adapter as cagent_module  # noqa: E402
from app.adapters.cagent_adapter import (  # noqa: E402
    CAgentAdapter, _parse_responses_output, _responses_input, _responses_usage,
)
from app.db.mock import MockMemoryDatabase  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        ok(label)
    else:
        fail(label)
        FAILURES.append(label)


def make_png(rgb, size=8) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ------------------------------------------------------------ fake client

def ns(**kwargs):
    return SimpleNamespace(**kwargs)


def response(*items, status="completed", usage=(10, 5), incomplete=None, error=None):
    """A Responses-API-shaped object (attribute access, like the SDK's)."""
    return ns(
        id="resp-1", model="resp-model", status=status, output=list(items),
        incomplete_details=ns(reason=incomplete) if incomplete else None, error=error,
        usage=ns(model_dump=lambda: {"input_tokens": usage[0], "output_tokens": usage[1],
                                     "total_tokens": sum(usage)}),
    )


def text_item(text):
    return ns(type="message", role="assistant",
              content=[ns(type="output_text", text=text, annotations=[])])


def call_item(call_id, name, arguments):
    return ns(type="function_call", call_id=call_id, name=name,
              arguments=json.dumps(arguments))


def chat_response(content):
    return ChatCompletion.model_validate({
        "id": "chat-1", "object": "chat.completion", "created": 0, "model": "chat-model",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    })


class FakeClient:
    """Stands in for AsyncOpenAI; scripted replies per API, every call recorded."""

    calls: list[tuple[str, dict]] = []
    # Callables (kwargs) -> response; the default answers with text.
    responses_reply = None
    chat_reply = None

    def __init__(self, **kwargs):
        self.responses = ns(create=self._responses)
        self.chat = ns(completions=ns(create=self._chat))

    async def _responses(self, **kwargs):
        FakeClient.calls.append(("responses", kwargs))
        reply = FakeClient.responses_reply
        return reply(kwargs) if reply else response(text_item("ok from responses"))

    async def _chat(self, **kwargs):
        FakeClient.calls.append(("chat", kwargs))
        reply = FakeClient.chat_reply
        return reply(kwargs) if reply else chat_response("ok from chat")


def reset_client(responses_reply=None, chat_reply=None):
    FakeClient.calls = []
    FakeClient.responses_reply = responses_reply
    FakeClient.chat_reply = chat_reply


# ------------------------------------------------------------ unit: translation

def test_translation() -> None:
    section("translation: messages -> instructions + input items")
    url = "data:image/png;base64,AAAA"
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [{"type": "text", "text": "what is this"},
                                     {"type": "image_url", "image_url": {"url": url, "detail": "low"}}]},
        {"role": "assistant", "content": "let me look",
         "tool_calls": [{"id": "c1", "name": "read", "arguments": {"file_path": "a.png"}},
                        {"id": "c2", "name": "read", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": [{"type": "text", "text": "before "},
                     {"type": "image_url", "image_url": {"url": url}},
                     {"type": "text", "text": " after"}]},
        {"role": "tool", "tool_call_id": "c2", "content": "plain result"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "user", "content": "thanks"},
    ]
    instructions, items = _responses_input(messages)
    check(instructions == "be brief", f"system prompt -> instructions ({instructions!r})")
    check(items[0] == {"role": "user", "content": [
        {"type": "input_text", "text": "what is this"},
        {"type": "input_image", "image_url": url, "detail": "low"}]},
        "user parts -> input_text + input_image with detail")
    check(items[1] == {"role": "assistant",
                       "content": "let me look"}, "assistant text stays a string")
    check(items[2] == {"type": "function_call", "call_id": "c1", "name": "read",
                       "arguments": '{"file_path": "a.png"}'}
          and items[3]["type"] == "function_call" and items[3]["arguments"] == "{}",
          "each tool call -> its own function_call item, arguments as JSON")
    check(items[4] == {"type": "function_call_output", "call_id": "c1", "output": [
        {"type": "input_text", "text": "before "},
        {"type": "input_image", "image_url": url, "detail": "auto"},
        {"type": "input_text", "text": " after"}]},
        "image tool result -> function_call_output parts, order kept, detail auto")
    check(items[5] == {"type": "function_call_output", "call_id": "c2", "output": "plain result"},
          "text tool result -> string output")
    check(items[6] == {"role": "user", "content": "thanks"} and len(items) == 7,
          "empty assistant message without calls adds nothing")
    instructions, items = _responses_input([
        {"role": "assistant", "content": [{"type": "text", "text": "x"}]}])
    check(instructions is None and items[0]["content"] == [{"type": "output_text", "text": "x"}],
          "no system prompt -> no instructions; assistant parts -> output_text")

    section("parsing: Responses output -> content / tool calls / finish_reason")
    content, calls, finish = _parse_responses_output(response(
        ns(type="reasoning", summary=[]), text_item("a"), text_item("b"),
        call_item("call_9", "read", {"file_path": "x"})))
    check(content == "ab" and calls == [{"id": "call_9", "name": "read",
                                         "arguments": {"file_path": "x"}}]
          and finish == "tool_calls", f"text joined, call parsed, reasoning skipped ({finish})")
    content, calls, finish = _parse_responses_output(response(text_item("done")))
    check((content, calls, finish) == ("done", [], "stop"), "plain answer -> stop")
    _, _, finish = _parse_responses_output(response(
        text_item("cut"), status="incomplete", incomplete="max_output_tokens"))
    check(finish == "length", f"incomplete max_output_tokens -> length ({finish})")
    content, _, _ = _parse_responses_output(response(ns(
        type="message", content=[ns(type="refusal", refusal="no")])))
    check(content == "no", "refusal text is the content")
    try:
        _parse_responses_output(response(status="failed", error=ns(message="boom")))
    except RuntimeError as exc:
        check("boom" in str(exc), "failed status raises with the error message")
    else:
        check(False, "failed status raises")
    try:
        _parse_responses_output(response(call_item("c", "read", [1])))
    except ValueError:
        check(True, "non-object tool arguments are rejected")
    else:
        check(False, "non-object tool arguments are rejected")
    usage = _responses_usage(ns(model_dump=lambda: {"input_tokens": 3, "output_tokens": 4}))
    check(usage == {"input_tokens": 3, "output_tokens": 4,
                    "prompt_tokens": 3, "completion_tokens": 4},
          "usage keeps Responses names and adds prompt/completion_tokens")


# ------------------------------------------------------------ engine

async def collect(adapter, text, **kwargs):
    await adapter.send(text, **kwargs)
    return [event async for event in adapter.stream()]


async def make_adapter(db, wd, sid, *, api_format="responses", tools=(), **config):
    await db.create_session("main-agent", working_dir=str(wd), session_id=sid)
    adapter = CAgentAdapter(db=db, tool_names=list(tools), session_id=sid, working_dir=str(wd),
                            config=config, model="resp-model", api_format=api_format)
    await adapter.start("You are a test agent.")
    return adapter


async def test_engine(db, wd) -> None:
    section("engine: a tool-use turn over the Responses API")
    (wd / "red.png").write_bytes(make_png((255, 0, 0)))

    def scripted(kwargs):
        step = sum(1 for kind, _ in FakeClient.calls if kind == "responses")
        if step == 1:
            return response(text_item("reading"), call_item("call_a", "read", {"file_path": "red.png"}))
        return response(text_item("it is red"), usage=(40, 6))

    reset_client(scripted)
    adapter = await make_adapter(db, wd, "s-resp-1", tools=["read"], vision=True,
                                 model_params={"temperature": 0.2, "max_tokens": 99})
    events = await collect(adapter, "what colour is red.png?")
    types = [e.type.value for e in events]
    calls = [kw for kind, kw in FakeClient.calls if kind == "responses"]
    check(len(calls) == 2 and not any(k == "chat" for k, _ in FakeClient.calls),
          f"two responses.create calls, no chat calls ({[k for k, _ in FakeClient.calls]})")
    first, second = calls
    check(first["model"] == "resp-model" and first["instructions"].startswith("You are a test agent")
          and first["input"][-1]["role"] == "user",
          "model + instructions + the user's message")
    check(first["temperature"] == 0.2 and first["max_tokens"] == 99,
          "model_params merged as they are (max_tokens not renamed)")
    tool = first["tools"][0]
    check(tool["type"] == "function" and tool["name"] == "read" and tool["strict"] is False
          and "parameters" in tool and "function" not in tool and "tool_choice" not in first,
          "flat function tools with strict=False; no tool_choice on agent steps")
    output = next(i for i in second["input"] if i.get("type") == "function_call_output")
    fc = next(i for i in second["input"] if i.get("type") == "function_call")
    parts = output["output"]
    check(fc["call_id"] == "call_a" == output["call_id"] and isinstance(parts, list)
          and any(p["type"] == "input_image" and p["image_url"].startswith("data:image/png")
                  for p in parts)
          and any(p["type"] == "input_text" for p in parts),
          "the image comes back inside function_call_output (input_text + input_image)")
    check(not any(i.get("role") == "user" and isinstance(i.get("content"), list)
                  for i in second["input"][1:]),
          "inline mode: no extra user message for the tool image")
    check("done" in types and "error" not in types, f"turn completes ({types})")
    usage = json.loads(next(e.data for e in events if e.type.value == "usage"))
    check(usage["prompt_tokens"] == 40 and usage["completion_tokens"] == 6
          and usage["context_tokens"] == 46, f"USAGE from input/output_tokens ({usage})")
    rows = await db.get_messages("s-resp-1")
    reply = [m for m in rows if m.role.value == "agent"][-1]
    meta = reply.metadata.get("response") or {}
    check(meta.get("model") == "resp-model" and meta.get("finish_reason") == "stop"
          and meta.get("usage", {}).get("input_tokens") == 40,
          f"reply metadata.response recorded ({meta})")
    await adapter.stop()

    section("engine: tool_image_mode user_message still relocates")
    reset_client(scripted)
    adapter = await make_adapter(db, wd, "s-resp-2", tools=["read"], vision=True,
                                 tool_image_mode="user_message")
    await collect(adapter, "look")
    second = [kw for kind, kw in FakeClient.calls if kind == "responses"][1]
    output = next(i for i in second["input"] if i.get("type") == "function_call_output")
    relocated = [i for i in second["input"]
                 if i.get("role") == "user" and isinstance(i.get("content"), list)
                 and any(p["type"] == "input_image" for p in i["content"])]
    check(isinstance(output["output"], str) and len(relocated) == 1,
          "tool output is text; the image travels in a following user message")
    await adapter.stop()

    section("engine: user images and the summarizer")
    reset_client()
    adapter = await make_adapter(db, wd, "s-resp-3", vision=True)
    from app.core import attachments
    stored = attachments.save_attachment(make_png((0, 0, 255)))
    await collect(adapter, "and this?", images=[{"ref": stored.ref, "detail": "high"}])
    first = FakeClient.calls[0][1]
    content = first["input"][-1]["content"]
    check(content[0] == {"type": "input_text", "text": "and this?"}
          and content[1]["type"] == "input_image" and content[1]["detail"] == "high",
          "user image -> input_image with its detail")
    before = len(FakeClient.calls)
    result = await adapter.compress()
    summary = FakeClient.calls[before:]
    check(result.get("status") == "compressed" and summary
          and all(kind == "responses" for kind, _ in summary)
          and "tools" not in summary[0][1],
          f"manual compression runs over the Responses API ({result.get('status')}, "
          f"{[k for k, _ in summary]})")
    await adapter.stop()

    section("engine: summarizer on its own compression_api_format")
    reset_client()
    adapter = await make_adapter(db, wd, "s-resp-4", tools=["read"],
                                 compression_api_format="chat",
                                 compression_turn_threshold=1)
    await collect(adapter, "first turn with a fairly long message " * 20)
    await collect(adapter, "second turn")
    kinds = [(k, kw.get("tool_choice")) for k, kw in FakeClient.calls]
    check(("chat", "none") in kinds and all(k == "responses" for k, tc in kinds if tc != "none"),
          f"agent steps on responses, summarizer on chat ({kinds})")
    await adapter.stop()
    try:
        await make_adapter(db, wd, "s-resp-bad", api_format="soap")
    except ValueError as exc:
        check("api_format" in str(exc), "unknown api_format fails at start")
    else:
        check(False, "unknown api_format fails at start")
    try:
        await make_adapter(db, wd, "s-resp-bad2", compression_api_format="soap")
    except ValueError as exc:
        check("compression_api_format" in str(exc), "unknown compression_api_format fails at start")
    else:
        check(False, "unknown compression_api_format fails at start")

    section("engine: the chat format is unchanged")
    reset_client()
    adapter = await make_adapter(db, wd, "s-chat-1", api_format=None)
    events = await collect(adapter, "hi")
    check([k for k, _ in FakeClient.calls] == ["chat"]
          and any(e.type.value == "text" and e.data == "ok from chat" for e in events),
          "api_format None -> chat.completions.create")
    await adapter.stop()


# ------------------------------------------------------------ REST

def test_rest() -> None:
    section("REST: agent default, session override, branch inheritance")
    from fastapi.testclient import TestClient
    from app.main import app

    api = "/api/v1/sessions"
    reset_client()
    with TestClient(app) as c:
        base = {"agent_type": "tool_use", "system_prompt": "t", "tool_names": []}
        r = c.post("/api/v1/agents", json={**base, "name": "fmt-bad", "openai_api_format": "soap"})
        check(r.status_code == 400, f"agent with an unknown format -> 400 ({r.status_code})")
        r = c.post("/api/v1/agents", json={**base, "name": "fmt-bad2",
                                           "config": {"compression_api_format": 3}})
        check(r.status_code == 400, f"bad config.compression_api_format -> 400 ({r.status_code})")
        r = c.post("/api/v1/agents", json={**base, "name": "fmt", "openai_model": "m"})
        agent = r.json()
        check(r.status_code == 201 and agent["openai_api_format"] == "chat",
              "agent defaults to chat")
        r = c.put(f"/api/v1/agents/{agent['id']}", json={"openai_api_format": "responses"})
        check(r.status_code == 200 and r.json()["openai_api_format"] == "responses",
              "agent format updated")
        r = c.put(f"/api/v1/agents/{agent['id']}", json={"openai_api_format": "soap"})
        check(r.status_code == 400, "agent update with an unknown format -> 400")

        sid = c.post(api, json={"agent_id": agent["id"]}).json()["id"]

        def turn(session_id, text):
            before = len(FakeClient.calls)
            r = c.post(f"{api}/{session_id}/messages", json={"content": text})
            assert r.status_code == 202, r.text
            deadline = time.time() + 15
            while time.time() < deadline:
                if (len(FakeClient.calls) > before
                        and c.get(f"{api}/{session_id}").json()["status"] == "WAITING_USER"):
                    return FakeClient.calls[-1][0]
                time.sleep(0.05)
            raise AssertionError("turn did not finish")

        view = c.get(f"{api}/{sid}/model").json()
        check(view["api_format"] is None and view["agent_api_format"] == "responses"
              and view["effective_api_format"] == "responses", f"GET follows the agent ({view})")
        check(turn(sid, "one") == "responses", "session uses the agent's format")
        r = c.put(f"{api}/{sid}/model", json={"model": None, "model_params": None,
                                               "api_format": "chat"})
        check(r.status_code == 200 and r.json()["effective_api_format"] == "chat"
              and r.json()["session"]["api_format"] == "chat", "PUT sets the session's format")
        check(turn(sid, "two") == "chat", "next turn uses the session's format")
        r = c.put(f"{api}/{sid}/model", json={"model": "x", "api_format": "soap"})
        check(r.status_code == 400 and c.get(f"{api}/{sid}").json()["api_format"] == "chat",
              "unknown format -> 400, nothing changed")
        user = next(m for m in c.get(f"{api}/{sid}/messages").json() if m["role"] == "user")
        diverge = f"{api}/{sid}/messages/{user['id']}/diverge"
        b = c.post(diverge, json={}).json()
        check(b["api_format"] == "chat", "a branch inherits the source's format")
        b = c.post(diverge, json={"api_format": None}).json()
        check(b["api_format"] is None, "branch with null -> follows the agent")
        b = c.post(diverge, json={"api_format": "responses"}).json()
        check(b["api_format"] == "responses" and turn(b["id"], "in branch") == "responses",
              "branch with its own format uses it")
        r = c.post(diverge, json={"api_format": "soap"})
        check(r.status_code == 400, "branch with an unknown format -> 400")
        r = c.post(f"{api}/{sid}/fork-dispatch", json={"messages": ["a"]})
        ids = [x["session_id"] for x in r.json().get("branches", [])]
        fmts = [c.get(f"{api}/{i}").json().get("api_format") for i in ids]
        check(r.status_code in (200, 201, 202) and ids and all(f == "chat" for f in fmts),
              f"fork-dispatch branches inherit ({r.status_code}, {fmts})")
        deadline = time.time() + 15
        while time.time() < deadline and c.get(f"{api}/{sid}").json().get("pending_joins"):
            time.sleep(0.05)
        r = c.put(f"{api}/{sid}/model", json={"model": None, "model_params": None,
                                               "api_format": None})
        check(r.json()["effective_api_format"] == "responses", "null -> back to the agent's format")


async def main() -> int:
    test_translation()
    original = cagent_module.AsyncOpenAI
    cagent_module.AsyncOpenAI = FakeClient
    try:
        with tempfile.TemporaryDirectory() as tmp:
            await test_engine(MockMemoryDatabase(), Path(tmp))
        await asyncio.to_thread(test_rest)
    finally:
        cagent_module.AsyncOpenAI = original
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        return 1
    print("\nALL RESPONSES API CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
