"""Offline checks for image (multimodal) input.

Fully isolated: DB_TYPE=sqlite on a temp file, AUGENTIA_HOME in a temp dir
(attachments + session configs), scripted model — no network, never touches
backend/.env's real database.

Covers:
  - attachment store: format sniffing, size limit, dedupe, id/path guards
  - per-turn validation: engine, config.vision, refs, detail default, count cap
  - CAgent engine turns: text+image, image-only, images re-sent from history,
    metadata persisted on the user row
  - HTTP: upload/download, POST /messages with images (accept + reject),
    retry keeps images, diverge clones them, message listing exposes them

Run from backend/:  python scripts/test_multimodal.py
"""
import asyncio
import os
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="augentia-mm-"))
# Must be set before _common loads .env (load_dotenv does not override).
os.environ["DB_TYPE"] = "sqlite"
os.environ["SQLITE_PATH"] = str(_TMP / "test.db")
os.environ["AUGENTIA_HOME"] = str(_TMP / "home")
os.environ["AUGENTIA_AUTH_ENABLED"] = "false"
os.environ.pop("AUGENTIA_ATTACHMENT_MAX_BYTES", None)

import _common  # noqa: E402,F401 — sys.path + logging
from _common import ok, fail, section  # noqa: E402

from app.adapters.cagent_adapter import CAgentAdapter  # noqa: E402
from app.core import attachments  # noqa: E402
from app.db.mock import MockMemoryDatabase  # noqa: E402
from app.models.domain import AgentTemplate, AgentType, MessageRole  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        ok(label)
    else:
        fail(label)
        FAILURES.append(label)


def make_png(width: int = 8, height: int = 8, rgb=(255, 0, 0)) -> bytes:
    """Minimal valid solid-colour PNG (no external deps)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def agent(config: dict) -> AgentTemplate:
    return AgentTemplate(
        name="mm", description="", agent_type=AgentType.TOOL_USE,
        system_prompt="test", tool_names=[], config=config,
    )


# ---------------------------------------------------------------- store

def test_attachment_store() -> None:
    section("attachment store")
    png = make_png()
    a = attachments.save_attachment(png)
    check(a.mime == "image/png" and a.ref == f"att:{a.id}", "png sniffed and referenced as att:<sha256>")
    check(attachments.save_attachment(png).path == a.path, "identical bytes deduplicate")
    for label, data, mime in (
        ("jpeg", b"\xff\xd8\xff\xe0" + b"0" * 32, "image/jpeg"),
        ("gif", b"GIF89a" + b"0" * 32, "image/gif"),
        ("webp", b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"0" * 32, "image/webp"),
    ):
        check(attachments.save_attachment(data).mime == mime, f"{label} sniffed from magic bytes")
    for label, data in (("text", b"hello, not an image"), ("empty", b""),
                        ("svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")):
        try:
            attachments.save_attachment(data)
            check(False, f"{label} upload rejected")
        except attachments.AttachmentError:
            check(True, f"{label} upload rejected")
    os.environ["AUGENTIA_ATTACHMENT_MAX_BYTES"] = "100"
    try:
        attachments.save_attachment(make_png(64, 64, (1, 2, 3)) + b"\x00" * 200)
        check(False, "oversized upload rejected")
    except attachments.AttachmentError:
        check(True, "oversized upload rejected")
    finally:
        os.environ.pop("AUGENTIA_ATTACHMENT_MAX_BYTES", None)
    for bad in ("../../etc/passwd", "a" * 63, "A" * 64, "x"):
        try:
            attachments.get_attachment(bad)
            check(False, f"invalid id {bad[:12]!r} rejected")
        except KeyError:
            check(True, f"invalid id {bad[:12]!r} rejected")
    url = attachments.data_url(a.ref)
    check(url.startswith("data:image/png;base64,"), "att: ref resolves to a png data URL")


def test_turn_validation() -> None:
    section("per-turn image validation")
    ref = attachments.save_attachment(make_png()).ref
    vision = agent({"vision": True})
    check(attachments.normalize_turn_images(vision, [ref]) == [{"ref": ref}], "cagent + vision accepts att: ref")
    check(attachments.normalize_turn_images(vision, []) == [], "no images -> empty list")
    check(
        attachments.normalize_turn_images(agent({"vision": True, "image_detail": "low"}), [ref])
        == [{"ref": ref, "detail": "low"}],
        "config.image_detail applied as default",
    )
    check(
        attachments.normalize_turn_images(vision, [{"ref": ref, "detail": "high"}])
        == [{"ref": ref, "detail": "high"}],
        "explicit detail kept",
    )
    rejections = [
        (agent({}), [ref], "vision off"),
        (agent({"vision": True, "engine": "legacy"}), [ref], "legacy engine"),
        (vision, ["att:" + "0" * 64], "unknown attachment"),
        (vision, ["https://example.com/a.png"], "non-att reference"),
        (vision, [{"ref": ref, "detail": "huge"}], "bad detail"),
        (vision, [{"ref": ref, "extra": 1}], "unknown field"),
        (vision, [ref] * (attachments.MAX_IMAGES_PER_TURN + 1), "too many images"),
        (vision, ref, "not a list"),
    ]
    for ag, images, label in rejections:
        try:
            attachments.normalize_turn_images(ag, images)
            check(False, f"rejects: {label}")
        except attachments.AttachmentError:
            check(True, f"rejects: {label}")


# ---------------------------------------------------------------- engine

async def test_engine_turns() -> None:
    section("CAgent engine turns with images")
    db = MockMemoryDatabase()
    template = await db.create_agent(agent({"vision": True}))
    session = await db.create_session(template.id, session_id="s-mm")
    ref = attachments.save_attachment(make_png(4, 4, (0, 0, 255))).ref
    seen: list[list[dict]] = []

    async def model(*, messages, tools, tool_choice=None):
        seen.append(messages)
        return {"content": f"reply {len(seen)}"}

    adapter = CAgentAdapter(db=db, tool_names=[], session_id=session.id, config={"vision": True},
                            model_callable=model)
    await adapter.start("You are a test agent.")

    async def turn(text, images=None):
        await adapter.send(text, images=images)
        return [event async for event in adapter.stream()]

    await turn("what colour?", images=[{"ref": ref}])
    user = seen[0][-1]
    parts = user["content"] if isinstance(user["content"], list) else []
    check([p["type"] for p in parts] == ["text", "image_url"], "text then image_url parts sent to the model")
    check(bool(parts) and parts[-1]["image_url"]["url"].startswith("data:image/png;base64,"),
          "image resolved to a data URL before the model call")

    rows = await db.get_messages(session.id)
    user_rows = [r for r in rows if r.role == MessageRole.USER]
    check(user_rows and user_rows[0].metadata == {"images": [{"ref": ref}]},
          "user row persists metadata.images (reference only)")
    check(user_rows and user_rows[0].content == "what colour?", "user row content stays plain text")

    await turn("and now?")
    history_user = [m for m in seen[1] if m["role"] == "user"]
    check(isinstance(history_user[0]["content"], list), "history image re-sent on the next text-only turn")
    check(history_user[-1]["content"] == "and now?", "text-only turn stays a plain string")

    await turn("", images=[{"ref": ref, "detail": "low"}])
    last = seen[2][-1]
    check(isinstance(last["content"], list) and [p["type"] for p in last["content"]] == ["image_url"],
          "image-only turn (empty text) is accepted")
    check(last["content"][0]["image_url"].get("detail") == "low", "detail forwarded to the model")
    await adapter.stop()


# ---------------------------------------------------------------- HTTP

def test_http() -> None:
    section("HTTP: attachments + messages with images")
    import app.adapters.cagent_adapter as cagent_module
    from fastapi.testclient import TestClient
    from app.main import app

    seen: list[list[dict]] = []

    async def model(*, messages, tools, tool_choice=None):
        seen.append(messages)
        return {"content": "I see it"}

    original = cagent_module._make_model
    cagent_module._make_model = lambda client, name: model
    try:
        with TestClient(app) as c:
            png = make_png(6, 6, (0, 255, 0))
            r = c.post("/api/v1/attachments", content=png, headers={"Content-Type": "image/png"})
            check(r.status_code == 201, f"upload -> 201 (got {r.status_code})")
            att = r.json()
            r = c.get(f"/api/v1/attachments/{att['id']}")
            check(r.status_code == 200 and r.content == png
                  and r.headers["content-type"] == "image/png", "download returns the same bytes")
            check(c.get(f"/api/v1/attachments/{'0' * 64}").status_code == 404, "unknown id -> 404")
            r = c.post("/api/v1/attachments", content=b"not an image",
                       headers={"Content-Type": "image/png"})
            check(r.status_code == 400, "non-image upload -> 400")

            def make_agent(config):
                r = c.post("/api/v1/agents", json={
                    "name": f"mm-{len(config)}", "agent_type": "tool_use",
                    "system_prompt": "test", "tool_names": [], "config": config,
                })
                assert r.status_code == 201, r.text
                return r.json()["id"]

            def make_session(agent_id):
                r = c.post("/api/v1/sessions", json={"agent_id": agent_id})
                assert r.status_code == 201, r.text
                return r.json()["id"]

            def wait_idle(sid, agent_rows):
                deadline = time.time() + 15
                while time.time() < deadline:
                    msgs = c.get(f"/api/v1/sessions/{sid}/messages").json()
                    status = c.get(f"/api/v1/sessions/{sid}").json()["status"]
                    if status == "WAITING_USER" and sum(m["role"] == "agent" for m in msgs) >= agent_rows:
                        return msgs
                    time.sleep(0.1)
                raise AssertionError(f"session {sid} did not finish")

            plain_sid = make_session(make_agent({}))
            r = c.post(f"/api/v1/sessions/{plain_sid}/messages",
                       json={"content": "look", "images": [att["ref"]]})
            check(r.status_code == 400 and "vision" in r.json()["detail"], "vision-off agent rejects images -> 400")
            legacy_sid = make_session(make_agent({"vision": True, "engine": "legacy"}))
            r = c.post(f"/api/v1/sessions/{legacy_sid}/messages",
                       json={"content": "look", "images": [att["ref"]]})
            check(r.status_code == 400 and "cagent" in r.json()["detail"], "legacy-engine agent rejects images -> 400")
            r = c.post(f"/api/v1/sessions/{plain_sid}/messages", json={"content": "   "})
            check(r.status_code == 400, "empty text without images -> 400")

            sid = make_session(make_agent({"vision": True}))
            r = c.post(f"/api/v1/sessions/{sid}/messages",
                       json={"content": "what is this?", "images": [att["ref"]]})
            check(r.status_code == 202, f"vision agent accepts images -> 202 (got {r.status_code})")
            msgs = wait_idle(sid, 1)
            user = next(m for m in msgs if m["role"] == "user")
            check(user["metadata"] == {"images": [{"ref": att["ref"]}]}, "message listing exposes metadata.images")
            check(all(m["role"] != "mark" for m in msgs), "mark rows still hidden from clients")
            check(any(isinstance(m["content"], list) for m in seen[-1] if m["role"] == "user"),
                  "model received the image through the HTTP path")

            r = c.post(f"/api/v1/sessions/{sid}/messages/{user['id']}/diverge", json={})
            check(r.status_code == 201, "diverge -> 201")
            branch_msgs = c.get(f"/api/v1/sessions/{r.json()['id']}/messages").json()
            branch_user = next((m for m in branch_msgs if m["role"] == "user"), None)
            # diverge clones history *before* the chosen message; post one more
            # turn first to have something cloned.
            if branch_user is None:
                c.post(f"/api/v1/sessions/{sid}/messages", json={"content": "second"})
                msgs = wait_idle(sid, 2)
                second = [m for m in msgs if m["role"] == "user"][-1]
                r = c.post(f"/api/v1/sessions/{sid}/messages/{second['id']}/diverge", json={})
                branch_msgs = c.get(f"/api/v1/sessions/{r.json()['id']}/messages").json()
                branch_user = next((m for m in branch_msgs if m["role"] == "user"), None)
            check(branch_user is not None and branch_user["metadata"].get("images"),
                  "diverged session keeps the cloned images")

            msgs = c.get(f"/api/v1/sessions/{sid}/messages").json()
            first_user = next(m for m in msgs if m["role"] == "user")
            r = c.post(f"/api/v1/sessions/{sid}/messages/{first_user['id']}/retry",
                       json={"content": "edited question"})
            check(r.status_code == 202, "retry -> 202")
            msgs = wait_idle(sid, 1)
            retried = next(m for m in msgs if m["role"] == "user")
            check(retried["content"] == "edited question"
                  and retried["metadata"] == {"images": [{"ref": att["ref"]}]},
                  "retry with new text keeps the original images")
            r = c.post(f"/api/v1/sessions/{sid}/messages/{retried['id']}/retry",
                       json={"content": "no image now", "images": []})
            msgs = wait_idle(sid, 1)
            dropped = next(m for m in msgs if m["role"] == "user")
            check(r.status_code == 202 and not dropped["metadata"].get("images"),
                  "retry with images=[] drops them")
    finally:
        cagent_module._make_model = original


async def main() -> int:
    test_attachment_store()
    test_turn_validation()
    await test_engine_turns()
    return 0


if __name__ == "__main__":
    asyncio.run(main())
    test_http()  # TestClient runs its own event loop; keep it outside asyncio.run
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        sys.exit(1)
    print("\nALL MULTIMODAL CHECKS PASSED")
