"""Offline checks for branches that borrow their history (no copies).

A branch made by diverge records branched_from_session_id and
branched_cutoff_at and reads the source's rows before the cutoff, as they
were when the branch was made. Drives real CAgent sessions on the mock
database with a scripted model, through the router functions.

Covers:
  - diverge writes no rows; the branch's history is the source's (same ids,
    metadata.borrowed_from), its next turn sends that history to the model
  - later changes in the source (delete, rewrite, compression, deleting the
    session) don't change what the branch sees
  - borrowed rows are read-only in the branch: delete / retry -> 400; its own
    rows can still be deleted
  - compression in a branch never covers borrowed rows: whole-history is
    skipped, range compression only takes the branch's own turns
  - a branch of a branch borrows through both levels

Run from backend/:  python scripts/test_branch_borrow.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import _common  # noqa: F401 — sets sys.path + logging

from fastapi import HTTPException

import app.adapters.cagent_adapter as cagent_adapter_module
from app.adapters.registry import AdapterRegistry
from app.core.compressed import is_summary
from app.core.session_runtime import build_runner
from app.db.mock import MockMemoryDatabase
from app.models.domain import AgentTemplate, AgentType, MessageRole
from app.routers.sessions import (
    CompressRequest, DeriveSessionRequest, RetryMessageRequest, compress_session,
    delete_message, diverge_from_message, retry_from_message,
)

from _common import fail, ok, section
from test_cagent_adapter import collect_until_done, summary_instruction

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        ok(label)
    else:
        fail(label)
        FAILURES.append(label)


def user_texts(messages) -> list[str]:
    """User/assistant text the model received, in order (no system)."""
    out = []
    for m in messages:
        if m["role"] in ("user", "assistant") and isinstance(m.get("content"), str):
            out.append(f"{m['role'][0]}:{m['content']}")
    return out


async def main() -> int:
    db = MockMemoryDatabase()
    seen: list[list[dict]] = []

    async def model(*, messages, tools, tool_choice=None):
        # The summarizer's request ends with its instruction (CAgent default
        # instructions start "Summarize"); ordinary turns end with user text.
        if summary_instruction(messages).startswith("Summarize"):
            return {"content": "SUMMARY"}
        seen.append(messages)
        last = messages[-1]
        return {"content": f"re:{last.get('content')}"}

    original = cagent_adapter_module._make_model
    cagent_adapter_module._make_model = lambda client, name: model
    registry = AdapterRegistry()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            agent = await db.create_agent(AgentTemplate(
                name="branch-test", description="", agent_type=AgentType.TOOL_USE,
                system_prompt="You are a test agent.", tool_names=[],
                config={}, openai_model="test-model",
            ))

            async def new_session(sid):
                s = await db.create_session(agent.id, working_dir=tmp, session_id=sid)
                await build_runner(s, agent, db, registry)
                return s

            async def turn(sid, text):
                runner = registry.get(sid)

                async def consume():
                    async with runner.subscribe() as events:
                        return await collect_until_done(events)
                await runner.submit(text)
                await asyncio.wait_for(consume(), 15)
                for _ in range(100):
                    if not runner.is_busy:
                        break
                    await asyncio.sleep(0.01)

            async def rows(sid):
                return await db.get_messages(sid)

            def texts(ms):
                return [m.content for m in ms if m.role == MessageRole.USER]

            # ---------------------------------------------------------- setup
            source = await new_session("src")
            for t in ("one", "two", "three"):
                await turn("src", t)
            src_rows = await rows("src")
            two = next(m for m in src_rows if m.content == "two")

            section("diverge borrows, copies nothing")
            branch = await diverge_from_message("src", two.id, DeriveSessionRequest(),
                                                db=db, registry=registry)
            own = await db.get_own_messages(branch.id)
            b_rows = await rows(branch.id)
            check(own == [], f"no rows written for the branch ({len(own)})")
            check(texts(b_rows) == ["one"], f"branch sees the history before 'two' ({texts(b_rows)})")
            check({m.id for m in b_rows} <= {m.id for m in src_rows}
                  and all(m.metadata.get("borrowed_from") == "src" for m in b_rows),
                  "borrowed rows keep their ids and carry borrowed_from")
            check(branch.branched_cutoff_at == two.created_at, "cutoff = the divergence point's time")

            await turn(branch.id, "b-one")
            sent = user_texts(seen[-1])
            check(sent == ["u:one", "a:re:one", "u:b-one"],
                  f"branch's next turn sends the borrowed history ({sent})")

            section("source changes don't reach the branch")
            before = [(m.id, m.content) for m in await rows(branch.id)]
            one = next(m for m in src_rows if m.content == "one")
            await delete_message("src", one.id, db=db, registry=registry)
            check([(m.id, m.content) for m in await rows(branch.id)] == before,
                  "deleting a borrowed row in the source: branch unchanged")
            await turn("src", "four")
            r = await compress_session("src", CompressRequest(), db=db, registry=registry)
            check(r.get("status") == "compressed", f"source compressed ({r})")
            check([(m.id, m.content) for m in await rows(branch.id)] == before,
                  "compressing the source: branch unchanged")
            await turn(branch.id, "b-two")
            sent = user_texts(seen[-1])
            check(sent[:2] == ["u:one", "a:re:one"] and "SUMMARY" not in " ".join(sent),
                  f"model still gets the original borrowed rows ({sent[:4]})")

            section("borrowed rows are read-only in the branch")
            borrowed_user = next(m for m in await rows(branch.id) if m.metadata.get("borrowed_from"))
            for label, call in (
                ("delete", lambda: delete_message(branch.id, borrowed_user.id, db=db, registry=registry)),
                ("retry", lambda: retry_from_message(
                    branch.id, borrowed_user.id, RetryMessageRequest(content="x"),
                    db=db, registry=registry)),
            ):
                try:
                    await call()
                except HTTPException as e:
                    check(e.status_code == 400 and "borrow" in e.detail, f"{label} borrowed -> 400")
                else:
                    check(False, f"{label} borrowed was accepted")
            src_one = next(m for m in await db.get_own_messages("src", include_deleted=True)
                           if m.id == one.id)
            check(src_one.metadata.get("deleted_by") == "user" and "borrowed_from" not in src_one.metadata,
                  "the source row stayed as the source left it")
            b_own_user = next(m for m in await db.get_own_messages(branch.id) if m.content == "b-one")
            r = await delete_message(branch.id, b_own_user.id, db=db, registry=registry)
            check(r["deleted_ids"] == [b_own_user.id], "the branch's own row can be deleted")

            section("compression in a branch skips borrowed rows")
            r = await compress_session(branch.id, CompressRequest(), db=db, registry=registry)
            check(r.get("status") == "skipped" and "borrows" in r.get("reason", ""),
                  f"whole-history compression skipped ({r})")
            r = await compress_session(branch.id, CompressRequest(hours=1000), db=db, registry=registry)
            check(r.get("status") == "compressed" and r.get("kind") == "range",
                  f"range compression of the branch's own turns ({r})")
            b_rows = await rows(branch.id)
            summary = next((m for m in b_rows if is_summary(m)), None)
            replaced = set((summary.metadata if summary else {}).get("replaced_message_ids") or [])
            borrowed = {m.id for m in b_rows if m.metadata.get("borrowed_from")}
            check(summary is not None and replaced and not (replaced & borrowed)
                  and summary.session_id == branch.id,
                  "the range summary covers only the branch's own rows")
            check(texts([m for m in b_rows if m.metadata.get("borrowed_from")]) == ["one"],
                  "borrowed rows still live in the branch")

            section("branch of a branch")
            b_two = next(m for m in await db.get_own_messages(branch.id, include_deleted=True)
                         if m.content == "b-two")
            await turn(branch.id, "b-three")
            b_three = next(m for m in await db.get_own_messages(branch.id) if m.content == "b-three")
            nested = await diverge_from_message(branch.id, b_three.id, DeriveSessionRequest(),
                                                db=db, registry=registry)
            n_rows = await rows(nested.id)
            owners = {m.metadata.get("borrowed_from") for m in n_rows}
            check(owners == {"src", branch.id} and texts(n_rows)[0] == "one",
                  f"nested branch borrows through both levels ({texts(n_rows)}, {owners})")
            check(b_two.id not in {m.id for m in n_rows} and any(is_summary(m) for m in n_rows),
                  "the branch's range summary is borrowed in place of the turns it replaced")

            section("deleting the source keeps the branch's history")
            await registry.remove("src")
            from app.routers.sessions import delete_session
            await delete_session("src", db=db, registry=registry)
            check(texts([m for m in await rows(branch.id) if m.metadata.get("borrowed_from")]) == ["one"],
                  "branch still reads the deleted source's rows")
            await turn(branch.id, "after delete")
            check(user_texts(seen[-1])[:2] == ["u:one", "a:re:one"], "and still sends them to the model")
    finally:
        cagent_adapter_module._make_model = original
        for sid in list(getattr(registry, "_runners", {})):
            await registry.remove(sid)
    if FAILURES:
        print(f"\n{len(FAILURES)} CHECK(S) FAILED")
        return 1
    print("\nALL BRANCH BORROW CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
