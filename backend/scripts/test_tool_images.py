"""Offline checks for images in tool results (CAgent engine), driven by `read`.

Isolated: temp SQLite DB + temp AUGENTIA_HOME (attachments), scripted models,
no network. Covers:
  - read tool: image files return ToolOutput (detected by magic bytes),
    text/fake-image files stay text, oversized images are refused
  - CAgent engine: the next model step sees the image inside the tool message,
    TOOL_RESULT events carry refs, tool rows persist metadata.images, later
    turns re-send it from history
  - config: vision off drops images with a note, tool_image_limit trims older
    tool images, tool_image_mode=user_message relocates them, image_detail
  - interleaved output: order seen by the model, stored "at" offsets, limit,
    vision off / rejected images leave markers, user_message relocation
  - legacy engine: ToolOutput degrades to text instead of crashing the loop

Run from backend/:  python scripts/test_tool_images.py
"""
import asyncio
import json
import os
import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import _common  # noqa: F401 — sys.path + logging

_TMP = _common.use_isolated_state("augentia-toolimg-")

from _common import ok, fail, section  # noqa: E402

from app.adapters.base import StreamEventType  # noqa: E402
from app.adapters.cagent_adapter import CAgentAdapter  # noqa: E402
from app.adapters.openai_tool_use import OpenAIToolUseAdapter  # noqa: E402
from app.adapters.tools.base import ToolImage, ToolOutput  # noqa: E402
from app.adapters.tools.registry import load_tools  # noqa: E402
from app.core import attachments  # noqa: E402
from app.db.sqlite import SqliteDatabase  # noqa: E402
from app.models.domain import MessageRole  # noqa: E402

FAILURES: list[str] = []
WORKDIR = _TMP / "work"


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


def image_urls(content) -> list[str]:
    if not isinstance(content, list):
        return []
    return [p["image_url"]["url"] for p in content if p.get("type") == "image_url"]


def reader(*paths, final="done"):
    """Scripted model: one read call per path (one per step), then a final answer."""
    calls: list[list[dict]] = []

    async def model(*, messages, tools, tool_choice=None):
        calls.append(messages)
        step = len(calls)
        if step <= len(paths):
            return {"tool_calls": [{"id": f"read-{step}", "name": "read",
                                    "arguments": {"path": paths[step - 1]}}]}
        return {"content": final}

    return model, calls


# ------------------------------------------------------------ read tool

async def test_read_tool() -> None:
    section("read tool: image files")
    (tool,) = load_tools(["read"])
    tool.working_dir = str(WORKDIR)
    out = await tool.execute(path="red.png")
    check(isinstance(out, ToolOutput) and len(out.images) == 1
          and out.images[0].data == (WORKDIR / "red.png").read_bytes()
          and "image/png" in out.text, "png returns ToolOutput with the file bytes")
    out = await tool.execute(path="disguised.txt")
    check(isinstance(out, ToolOutput), "detection uses magic bytes, not the extension")
    out = await tool.execute(path="fake.png")
    check(isinstance(out, str) and "not really a png" in out, "text file named .png stays text")
    out = await tool.execute(path="notes.txt", offset=2, limit=1)
    check(out == "2\tline two", "text files keep line-numbered output")
    os.environ["AUGENTIA_ATTACHMENT_MAX_BYTES"] = "50"
    try:
        out = await tool.execute(path="red.png")
        check(isinstance(out, str) and "limit" in out, "oversized image refused with an error")
    finally:
        os.environ.pop("AUGENTIA_ATTACHMENT_MAX_BYTES", None)


# ------------------------------------------------------------ engine

class Harness:
    def __init__(self, db):
        self.db = db
        self.counter = 0

    async def adapter(self, model, **config):
        self.counter += 1
        session = await self.db.create_session(
            "main-agent", working_dir=str(WORKDIR), session_id=f"s-toolimg-{self.counter}")
        adapter = CAgentAdapter(db=self.db, tool_names=["read"], session_id=session.id,
                                working_dir=str(WORKDIR), config=config, model_callable=model)
        await adapter.start("You are a test agent.")
        return adapter

    @staticmethod
    async def turn(adapter, text="look"):
        await adapter.send(text)
        return [event async for event in adapter.stream()]


async def test_engine(h: Harness) -> None:
    section("CAgent engine: read returns an image")
    model, calls = reader("red.png", final="it is red")
    adapter = await h.adapter(model, vision=True)
    events = await h.turn(adapter)
    tool_msg = [m for m in calls[1] if m["role"] == "tool"][0]
    urls = image_urls(tool_msg["content"])
    check(len(urls) == 1 and urls[0].startswith("data:image/png;base64,"),
          "next model step sees the image inside the tool message")
    check(isinstance(tool_msg["content"], list) and tool_msg["content"][0]["type"] == "text",
          "tool text comes first, image after")
    result_event = next(e for e in events if e.type == StreamEventType.TOOL_RESULT)
    refs = result_event.images or []
    check(len(refs) == 1 and refs[0]["ref"].startswith("att:"), "TOOL_RESULT event carries the image ref")
    check(attachments.attachment_from_ref(refs[0]["ref"]).path.read_bytes()
          == (WORKDIR / "red.png").read_bytes(), "image bytes stored in the attachment store")
    rows = await h.db.get_messages(adapter.session_id)
    tool_row = next(r for r in rows if r.role == MessageRole.TOOL)
    check(tool_row.metadata.get("images") == refs, "tool row persists metadata.images (refs only)")
    check(json.loads(tool_row.content)["role"] == "tool", "tool row content stays OpenAI-native JSON")
    check(any(e.type == StreamEventType.DONE for e in events), "turn completes")

    async def followup(*, messages, tools, tool_choice=None):
        calls.append(messages)
        return {"content": "still red"}

    adapter._model = followup
    await h.turn(adapter, "and now?")
    tool_msg = [m for m in calls[-1] if m["role"] == "tool"][0]
    check(len(image_urls(tool_msg["content"])) == 1, "next turn re-sends the tool image from history")
    await adapter.stop()

    section("CAgent engine: vision off")
    model, calls = reader("red.png")
    adapter = await h.adapter(model)
    events = await h.turn(adapter)
    tool_msg = [m for m in calls[1] if m["role"] == "tool"][0]
    check(isinstance(tool_msg["content"], str) and "1 image(s) omitted" in tool_msg["content"]
          and "vision" in tool_msg["content"], "image dropped with a note when vision is off")
    result_event = next(e for e in events if e.type == StreamEventType.TOOL_RESULT)
    check(not result_event.images, "no image refs on the event")
    rows = await h.db.get_messages(adapter.session_id)
    check(all(not r.metadata.get("images") for r in rows), "nothing persisted as an image")
    await adapter.stop()

    section("CAgent engine: tool_image_limit")
    model, calls = reader("red.png", "blue.png", "green.png")
    adapter = await h.adapter(model, vision=True, tool_image_limit=1)
    await h.turn(adapter)
    tools = [m for m in calls[3] if m["role"] == "tool"]
    check([len(image_urls(m["content"])) for m in tools] == [0, 0, 1], "only the newest tool image is sent")
    check(all("1 earlier tool image(s) omitted" in m["content"] for m in tools[:2]),
          "older tool images replaced by a note")
    rows = await h.db.get_messages(adapter.session_id)
    check(sum(len(r.metadata.get("images") or []) for r in rows) == 3, "all refs stay in storage")
    await adapter.stop()

    model, calls = reader("red.png", "blue.png", "green.png", "red.png")
    adapter = await h.adapter(model, vision=True)
    await h.turn(adapter)
    tools = [m for m in calls[4] if m["role"] == "tool"]
    check([len(image_urls(m["content"])) for m in tools] == [0, 1, 1, 1], "default limit keeps the newest 3")
    await adapter.stop()

    section("CAgent engine: tool_image_mode=user_message + image_detail")
    model, calls = reader("red.png")
    adapter = await h.adapter(model, vision=True, tool_image_mode="user_message", image_detail="low")
    await h.turn(adapter)
    sent = calls[1]
    tool_msg = [m for m in sent if m["role"] == "tool"][0]
    check(isinstance(tool_msg["content"], str) and "attached in the following message" in tool_msg["content"],
          "tool message becomes text only")
    after = sent[sent.index(tool_msg) + 1]
    check(after["role"] == "user" and len(image_urls(after["content"])) == 1
          and "tool output" in after["content"][0]["text"], "image moved to a labelled user message")
    image_part = next(p for p in after["content"] if p["type"] == "image_url")
    check(image_part["image_url"].get("detail") == "low", "config.image_detail applied to tool images")
    await adapter.stop()

    section("CAgent engine: invalid image config is rejected at start")
    for config in ({"tool_image_limit": -1}, {"tool_image_limit": "3"}, {"tool_image_mode": "sideways"}):
        try:
            CAgentAdapter(db=h.db, tool_names=["read"], session_id="x", config=config)
            check(False, f"rejects {config}")
        except ValueError:
            check(True, f"rejects {config}")


# ------------------------------------------------------------ interleaved

def interleaving_read(red: bytes, blue: bytes):
    """A read.execute replacement returning text and two images interleaved."""

    async def execute(self, path, offset=None, limit=None):
        return ToolOutput.interleaved(["Page 1:", ToolImage(red), "Page 2:", ToolImage(blue, detail="high"), "end"])

    return execute


def part_kinds(content) -> list[str]:
    return [p["text"] if p["type"] == "text" else "IMG" for p in content]


async def test_interleaved_unit() -> None:
    section("ToolOutput: interleaved construction")
    out = ToolOutput.interleaved(["A", ToolImage(b"1"), "B", ToolImage(b"2")])
    check(out.text == "AB" and [i.at for i in out.images] == [1, 2], "text concatenated, images get offsets")
    check([s if isinstance(s, str) else "IMG" for s in out.segments()] == ["A", "IMG", "B", "IMG"],
          "segments() rebuilds the order")
    check(out.is_interleaved and not ToolOutput("x", [ToolImage(b"1")]).is_interleaved, "is_interleaved")
    check(out.text_without_images("why") == "A[image omitted]B[image omitted]\n[2 image(s) omitted: why]",
          "text_without_images marks each image position")
    for bad in (lambda: ToolOutput("ab", [ToolImage(b"1", at=2), ToolImage(b"2", at=1)]),
                lambda: ToolOutput("ab", [ToolImage(b"1", at=5)])):
        try:
            bad()
            check(False, "rejects bad offsets")
        except ValueError:
            check(True, "rejects bad offsets")


async def test_interleaved_engine(h: Harness) -> None:
    from app.adapters.tools.read import ReadTool

    red, blue = (WORKDIR / "red.png").read_bytes(), (WORKDIR / "blue.png").read_bytes()
    original = ReadTool.execute
    ReadTool.execute = interleaving_read(red, blue)
    try:
        section("CAgent engine: interleaved tool output")
        model, calls = reader("x", final="ok")
        adapter = await h.adapter(model, vision=True)
        events = await h.turn(adapter)
        tool_msg = [m for m in calls[1] if m["role"] == "tool"][0]
        check(part_kinds(tool_msg["content"]) == ["Page 1:", "IMG", "Page 2:", "IMG", "end"],
              "model sees text and images in the tool's order")
        check(tool_msg["content"][3]["image_url"].get("detail") == "high", "per-image detail kept")
        result_event = next(e for e in events if e.type == StreamEventType.TOOL_RESULT)
        check([i.get("at") for i in result_event.images or []] == [7, 14], "TOOL_RESULT images carry at offsets")
        check(json.loads(result_event.data)["result"] == "Page 1:Page 2:end", "event result is the joined text")
        rows = await h.db.get_messages(adapter.session_id)
        tool_row = next(r for r in rows if r.role == MessageRole.TOOL)
        check([i.get("at") for i in tool_row.metadata.get("images") or []] == [7, 14], "tool row persists at")

        async def followup(*, messages, tools, tool_choice=None):
            calls.append(messages)
            return {"content": "again"}

        adapter._model = followup
        await h.turn(adapter, "again?")
        tool_msg = [m for m in calls[-1] if m["role"] == "tool"][0]
        check(part_kinds(tool_msg["content"]) == ["Page 1:", "IMG", "Page 2:", "IMG", "end"],
              "order preserved when re-sent from history")
        await adapter.stop()

        section("CAgent engine: interleaved + tool_image_limit=1")
        model, calls = reader("x")
        adapter = await h.adapter(model, vision=True, tool_image_limit=1)
        await h.turn(adapter)
        tool_msg = [m for m in calls[1] if m["role"] == "tool"][0]
        kinds = part_kinds(tool_msg["content"])
        check(kinds.count("IMG") == 1 and kinds[0].startswith("Page 1:Page 2:"),
              "older image of the same result dropped, newest kept in place")
        await adapter.stop()

        section("CAgent engine: interleaved, vision off")
        model, calls = reader("x")
        adapter = await h.adapter(model)
        await h.turn(adapter)
        tool_msg = [m for m in calls[1] if m["role"] == "tool"][0]
        check(isinstance(tool_msg["content"], str)
              and tool_msg["content"].startswith("Page 1:[image omitted]Page 2:[image omitted]end")
              and "2 image(s) omitted" in tool_msg["content"], "markers left where images were")
        await adapter.stop()

        section("CAgent engine: interleaved, one image rejected by the store")
        ReadTool.execute = interleaving_read(b"not an image", blue)
        model, calls = reader("x")
        adapter = await h.adapter(model, vision=True)
        await h.turn(adapter)
        tool_msg = [m for m in calls[1] if m["role"] == "tool"][0]
        kinds = part_kinds(tool_msg["content"])
        check(kinds[0] == "Page 1:[image omitted]Page 2:" and kinds[1] == "IMG"
              and "1 image(s) dropped" in kinds[2], "rejected image marked in place, the other still sent")
        await adapter.stop()
        ReadTool.execute = interleaving_read(red, blue)

        section("CAgent engine: interleaved + tool_image_mode=user_message")
        model, calls = reader("x")
        adapter = await h.adapter(model, vision=True, tool_image_mode="user_message")
        await h.turn(adapter)
        sent = calls[1]
        tool_msg = [m for m in sent if m["role"] == "tool"][0]
        check(isinstance(tool_msg["content"], str) and tool_msg["content"].startswith("Page 1:Page 2:end"),
              "tool message keeps the text only")
        after = sent[sent.index(tool_msg) + 1]
        check(after["role"] == "user" and len(image_urls(after["content"])) == 2, "both images relocated")
        await adapter.stop()
    finally:
        ReadTool.execute = original


# ------------------------------------------------------------ legacy

class _FakeCompletions:
    """Minimal AsyncOpenAI stand-in: one read call, then a final answer."""

    def __init__(self):
        self.requests: list[list[dict]] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs["messages"])
        if len(self.requests) == 1:
            call = SimpleNamespace(id="legacy-read", type="function",
                                   function=SimpleNamespace(name="read", arguments=json.dumps({"path": "red.png"})))
            msg = SimpleNamespace(content=None, tool_calls=[call])
            msg.model_dump = lambda **_: {"role": "assistant", "content": None, "tool_calls": [{
                "id": "legacy-read", "type": "function",
                "function": {"name": "read", "arguments": json.dumps({"path": "red.png"})}}]}
        else:
            msg = SimpleNamespace(content="done", tool_calls=None)
            msg.model_dump = lambda **_: {"role": "assistant", "content": "done"}
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)


async def test_legacy() -> None:
    section("legacy engine: ToolOutput degrades to text")
    adapter = OpenAIToolUseAdapter(tool_names=["read"], session_id="legacy", working_dir=str(WORKDIR),
                                   config={"tool_approvals": {"read": False}})
    await adapter.start("test")
    fake = _FakeCompletions()
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    await adapter.send("read it")
    events = [e async for e in adapter.stream()]
    check(not any(e.type == StreamEventType.ERROR for e in events), "no error in the legacy loop")
    tool_msg = [m for m in fake.requests[1] if m.get("role") == "tool"][0]
    check(isinstance(tool_msg["content"], str) and "legacy engine" in tool_msg["content"],
          "image replaced by a text note")
    await adapter.stop()


async def main() -> int:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    (WORKDIR / "red.png").write_bytes(make_png((230, 20, 20)))
    (WORKDIR / "blue.png").write_bytes(make_png((20, 40, 230)))
    (WORKDIR / "green.png").write_bytes(make_png((20, 200, 40)))
    (WORKDIR / "disguised.txt").write_bytes(make_png((1, 2, 3)))
    (WORKDIR / "fake.png").write_text("not really a png\n", encoding="utf-8")
    (WORKDIR / "notes.txt").write_text("line one\nline two\nline three\n", encoding="utf-8")

    await test_read_tool()
    db = SqliteDatabase(_TMP / "engine.db")
    await db.connect()
    harness = Harness(db)
    await test_engine(harness)
    await test_interleaved_unit()
    await test_interleaved_engine(harness)
    await test_legacy()
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        return 1
    print("\nALL TOOL IMAGE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
