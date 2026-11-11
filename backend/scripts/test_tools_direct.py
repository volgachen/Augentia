"""Test the tool implementations without running the backend.

Imports the tool registry in-process and calls .execute() directly. Useful for
isolating tool bugs (HTTP, parsing) from adapter/model bugs. No uvicorn needed.
"""
import asyncio
import sys

import _common  # noqa: F401 — sets sys.path so `app.*` imports resolve

import app.adapters.tools  # noqa: F401 — triggers @register_tool side effects
from app.adapters.tools.registry import list_available, load_tools
from _common import ok, fail, info, section

QUERIES = {
    "web_search": {"query": "Python asyncio"},
    "arxiv_search": {"query": "transformer efficiency", "max_results": 2},
    "bash": {"command": "echo hello from bash"},
    "read": {"file_path": "scripts/_common.py", "limit": 5},
    "write": {"file_path": "tmp/smoke_write.txt", "content": "hello\nworld\n"},
    "edit": {
        "file_path": "tmp/smoke_write.txt",
        "old_string": "world",
        "new_string": "there",
    },
}


async def main() -> int:
    section("Direct tool execution")
    info(f"Registered tools: {list_available()}")

    failures = 0
    for name, kwargs in QUERIES.items():
        try:
            (tool,) = load_tools([name])
        except ValueError as e:
            fail(f"{name}: {e}")
            failures += 1
            continue

        info(f"--- {name}({kwargs}) ---")
        try:
            result = await tool.execute(**kwargs)
        except Exception as e:
            fail(f"{name} raised {type(e).__name__}: {e}")
            failures += 1
            continue

        if not isinstance(result, str) or not result.strip():
            fail(f"{name} returned empty/non-string result: {result!r}")
            failures += 1
            continue

        ok(f"{name} -> {len(result)} chars")
        info(result[:300].replace("\n", " | "))

    if failures:
        print(f"\n{failures} tool(s) failed.")
        return 1
    print("\nAll tools returned results.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
