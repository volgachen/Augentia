"""Live check: images returned by a tool reach a real vision model.

Against a running gateway: a `read`-only agent opens a solid-colour PNG in its
working dir and must name the colour. Runs both tool_image_mode values, plus a
vision-off agent whose tool result must carry no image.

    python -m uvicorn app.main:app --port 12608      # throwaway instance
    AUGENTIA_BASE_URL=http://127.0.0.1:12608/api/v1 python scripts/test_tool_images_live.py
"""
import asyncio
import json
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

import httpx
import websockets

BASE = os.getenv("AUGENTIA_BASE_URL", "http://127.0.0.1:12598/api/v1")
WS = BASE.replace("http://", "ws://").replace("https://", "wss://")
COLOURS = {"red": (230, 20, 20), "blue": (20, 40, 230), "green": (20, 200, 40)}
PROMPT = ("Use the read tool to open screen.png, then tell me which single colour fills it. "
          "Answer with one lowercase word.")


def make_png(rgb, size=64) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


async def run(c: httpx.AsyncClient, label: str, config: dict, colour: str) -> tuple[str, list | None, str]:
    wd = Path(tempfile.mkdtemp(prefix="augentia-toolimg-live-"))
    (wd / "screen.png").write_bytes(make_png(COLOURS[colour]))
    r = await c.post(f"{BASE}/agents", json={
        "name": f"toolimg-{label}", "agent_type": "tool_use",
        "system_prompt": "You are a precise visual assistant. Use tools when asked.",
        "tool_names": ["read"], "config": config,
    })
    r.raise_for_status()
    r = await c.post(f"{BASE}/sessions", json={"agent_id": r.json()["id"], "source_dir": str(wd)})
    r.raise_for_status()
    sid = r.json()["id"]
    answer, result_images, result_text = "", None, ""
    async with websockets.connect(f"{WS}/sessions/{sid}/stream", max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"content": PROMPT}))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=180))
            if frame["type"] == "tool_confirm":
                await ws.send(json.dumps({"decision": "approve", "call_id": json.loads(frame["data"])["call_id"]}))
            elif frame["type"] == "tool_result":
                result_images = frame.get("images")
                result_text = json.loads(frame["data"]).get("result", "")
            elif frame["type"] == "assistant_message":
                answer = json.loads(frame["data"]).get("content") or answer
            elif frame["type"] in ("error", "done"):
                if frame["type"] == "error":
                    answer = f"ERROR {frame['data'][:200]}"
                break
    return answer, result_images, result_text


async def main() -> int:
    failures = []
    async with httpx.AsyncClient(trust_env=False, timeout=60) as c:
        for label, config, colour in (
            ("inline", {"vision": True}, "red"),
            ("inline", {"vision": True}, "blue"),
            ("user_message", {"vision": True, "tool_image_mode": "user_message"}, "green"),
            ("user_message", {"vision": True, "tool_image_mode": "user_message"}, "red"),
        ):
            answer, images, _ = await run(c, label, config, colour)
            print(f"[{label}] actual {colour}: model answered {answer!r}; tool images={images}")
            if not images or not images[0]["ref"].startswith("att:"):
                failures.append(f"[{label}/{colour}] TOOL_RESULT carried no image refs")
            if colour not in answer.lower():
                failures.append(f"[{label}/{colour}] model answered {answer!r}")
        answer, images, text = await run(c, "vision-off", {}, "blue")
        print(f"[vision-off] tool result text={text!r}; images={images}; model answered {answer!r}")
        if images or "omitted" not in text:
            failures.append("[vision-off] image was not dropped with a note")
    if failures:
        print("FAIL:\n  " + "\n  ".join(failures))
        return 1
    print("PASS: tool-result images reach the real model (inline + user_message); vision-off drops them")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
