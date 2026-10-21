"""Live image-input check against a running gateway and a real vision model.

Uploads a generated solid-colour PNG, sends it with a question over the WS,
and asserts: the USER event echoes the image refs, the model's answer names
the colour, and the persisted user row keeps metadata.images.

    python -m uvicorn app.main:app --port 12608      # in another shell
    AUGENTIA_BASE_URL=http://127.0.0.1:12608/api/v1 python scripts/test_vision_live.py
"""
import asyncio
import json
import os
import struct
import sys
import zlib

import httpx
import websockets

BASE = os.getenv("AUGENTIA_BASE_URL", "http://127.0.0.1:12598/api/v1")
WS = BASE.replace("http://", "ws://").replace("https://", "wss://")

COLOURS = {"red": (230, 20, 20), "blue": (20, 40, 230), "green": (20, 200, 40)}


def make_png(rgb, size=64) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


async def ask(c: httpx.AsyncClient, agent_id: str, colour: str) -> list[str]:
    problems: list[str] = []
    r = await c.post(f"{BASE}/attachments", content=make_png(COLOURS[colour]),
                     headers={"Content-Type": "image/png"})
    r.raise_for_status()
    ref = r.json()["ref"]
    r = await c.post(f"{BASE}/sessions", json={"agent_id": agent_id})
    r.raise_for_status()
    sid = r.json()["id"]

    answer, user_images = "", None
    async with websockets.connect(f"{WS}/sessions/{sid}/stream", max_size=None) as ws:
        await ws.recv()  # session_state
        await ws.send(json.dumps({
            "content": "What single colour fills this image? Answer with one lowercase word.",
            "images": [{"ref": ref, "detail": "low"}],
        }))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
            if frame["type"] == "user":
                user_images = frame.get("images")
            elif frame["type"] == "assistant_message":
                answer = json.loads(frame["data"]).get("content") or answer
            elif frame["type"] == "error":
                problems.append(f"error frame: {frame['data'][:200]}")
                break
            elif frame["type"] == "done":
                break

    if user_images != [{"ref": ref, "detail": "low"}]:
        problems.append(f"USER event images={user_images!r}")
    if colour not in answer.lower():
        problems.append(f"model answered {answer!r}, expected {colour}")
    msgs = (await c.get(f"{BASE}/sessions/{sid}/messages")).json()
    user = next((m for m in msgs if m["role"] == "user"), {})
    if user.get("metadata", {}).get("images") != [{"ref": ref, "detail": "low"}]:
        problems.append(f"persisted metadata={user.get('metadata')!r}")
    print(f"[{colour}] model answered: {answer!r}")
    return problems


async def main() -> int:
    async with httpx.AsyncClient(trust_env=False, timeout=60) as c:
        r = await c.post(f"{BASE}/agents", json={
            "name": "vision-live", "agent_type": "tool_use",
            "system_prompt": "You are a precise visual assistant.",
            "tool_names": [], "config": {"vision": True},
        })
        r.raise_for_status()
        agent_id = r.json()["id"]
        failures = []
        for colour in ("red", "blue"):
            failures += [f"[{colour}] {p}" for p in await ask(c, agent_id, colour)]
    if failures:
        print("FAIL:\n  " + "\n  ".join(failures))
        return 1
    print("PASS: live vision round-trip (upload -> WS images -> model -> persisted refs)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
