"""One-off migration: soft-delete rows that existing summaries replaced.

Before compressed_by, saving a summary left the rows it replaced live (the
engine hid them by reading the summary). Now those rows are soft-deleted with
metadata.compressed_by = <summary id> as soon as the summary is saved. This
script brings older sessions in line: for every live summary, oldest first,
the live rows it covers are soft-deleted and tagged — the same rule the store
applies to new summaries (app.core.compressed.covered_ids).

Processing oldest first makes nesting come out right: an earlier summary
claims its originals, and a later whole-history summary then covers the
earlier summary row (now its child) plus anything else before its boundary.

Summaries that diverge copied from another session name ids of the source
session, so they cover nothing here and are left alone; CAgent already sends
such a copy as plain context.

Uses the database configured in backend/.env (DB_TYPE, SQLITE_PATH / MySQL).
Stop the backend first. Dry run by default; pass --apply to write.

Run from backend/:  python scripts/migrate_compressed_by.py [--apply] [--session ID]
"""
import argparse
import asyncio
import sys

import _common  # noqa: F401 — sets sys.path, loads .env, logging

from app.core.compressed import covered_ids, is_summary
from app.db.deps import close_db, get_db, init_db


async def migrate(db, session_ids: list[str], apply: bool) -> tuple[int, int]:
    summaries_total = rows_total = 0
    for session_id in session_ids:
        live = await db.get_messages(session_id)
        summaries = [m for m in live if is_summary(m)]
        if not summaries:
            continue
        session_rows = 0
        for summary in summaries:
            live = await db.get_messages(session_id) if apply else live
            ids = covered_ids(live, summary)
            if not ids:
                continue
            summaries_total += 1
            session_rows += len(ids)
            if apply:
                await db.soft_delete_messages(session_id, ids, compressed_by=summary.id)
            else:
                # Dry run: simulate the deletion so later summaries see it.
                gone = set(ids)
                live = [m for m in live if m.id not in gone]
        if session_rows:
            rows_total += session_rows
            print(f"{session_id}: {session_rows} row(s) under {len(summaries)} summary(ies)")
    return summaries_total, rows_total


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--session", action="append", help="limit to these session ids")
    args = parser.parse_args()

    await init_db()
    db = get_db()
    try:
        session_ids = args.session or [s.id for s in await db.list_sessions()]
        summaries, rows = await migrate(db, session_ids, args.apply)
    finally:
        await close_db()
    verb = "compressed" if args.apply else "would compress"
    print(f"{verb} {rows} row(s) under {summaries} summary(ies) "
          f"in {len(session_ids)} session(s) checked")
    if not args.apply:
        print("dry run — re-run with --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
