"""Test the read tool's branches without running the backend.

Calls ReadTool.execute() directly to verify each behavior path:
  - full read (every line, 1-based numbering, tab separator)
  - offset (line numbers reflect real position in the file)
  - limit (caps the number of lines)
  - offset + limit together
  - truncation (output past the token cap gets a [Truncated] marker)
  - missing file (graceful [Error] string, not an exception)

No uvicorn, no model, no network needed.
"""
import asyncio
import os
import sys

import _common  # noqa: F401 — sets sys.path so `app.*` imports resolve

import app.adapters.tools  # noqa: F401 — triggers @register_tool side effects
from app.adapters.tools.registry import load_tools
from _common import ok, fail, info, section

# A small known file to read against — written fresh so line content is stable.
_FIXTURE = os.path.join(os.path.dirname(__file__), "..", "tmp", "read_fixture.txt")


def _write_fixture(n_lines: int) -> None:
    os.makedirs(os.path.dirname(_FIXTURE), exist_ok=True)
    with open(_FIXTURE, "w", encoding="utf-8") as f:
        f.write("\n".join(f"content {i}" for i in range(1, n_lines + 1)))


async def main() -> int:
    section("read tool branches")
    (tool,) = load_tools(["read"])
    failures = 0

    _write_fixture(10)

    # 1. Full read — 1-based numbering with tab separator
    info("--- full read ---")
    result = await tool.execute(file_path=_FIXTURE)
    lines = result.splitlines()
    if lines[0] == "1\tcontent 1" and lines[-1] == "10\tcontent 10":
        ok("full: numbered 1..10 with tab separator")
    else:
        fail(f"full: unexpected first/last line: {lines[0]!r} / {lines[-1]!r}")
        failures += 1

    # 2. Offset — numbering reflects real file position
    info("--- offset ---")
    result = await tool.execute(file_path=_FIXTURE, offset=4)
    lines = result.splitlines()
    if lines[0] == "4\tcontent 4" and len(lines) == 7:
        ok("offset: starts at line 4, keeps real line numbers")
    else:
        fail(f"offset: got first={lines[0]!r}, count={len(lines)}")
        failures += 1

    # 3. Limit — caps line count
    info("--- limit ---")
    result = await tool.execute(file_path=_FIXTURE, limit=3)
    lines = result.splitlines()
    if len(lines) == 3 and lines[0] == "1\tcontent 1" and lines[-1] == "3\tcontent 3":
        ok("limit: returned exactly 3 lines")
    else:
        fail(f"limit: got {len(lines)} lines: {lines}")
        failures += 1

    # 4. Offset + limit
    info("--- offset + limit ---")
    result = await tool.execute(file_path=_FIXTURE, offset=5, limit=2)
    lines = result.splitlines()
    if lines == ["5\tcontent 5", "6\tcontent 6"]:
        ok("offset+limit: lines 5-6 only")
    else:
        fail(f"offset+limit: got {lines}")
        failures += 1

    # 5. Truncation — a big file should trip the token cap
    info("--- truncation ---")
    _write_fixture(200_000)
    result = await tool.execute(file_path=_FIXTURE)
    if "[Truncated:" in result:
        ok("truncation: marker appended for oversized output")
    else:
        fail("truncation: expected [Truncated] marker, none found")
        failures += 1

    # 6. Missing file — graceful error string
    info("--- missing file ---")
    result = await tool.execute(file_path="does/not/exist.txt")
    if result.startswith("[Error]") and "not found" in result:
        ok("missing: returned graceful error string")
    else:
        fail(f"missing: unexpected result: {result!r}")
        failures += 1

    os.remove(_FIXTURE)

    if failures:
        print(f"\n{failures} read branch(es) failed.")
        return 1
    print("\nAll read branches behaved as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
