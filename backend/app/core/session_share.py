"""Opt-in public read-only sharing of a session's transcript.

The flag lives in its own file next to the session config
(``<augentia_home>/sessions/<id>/share.json``) rather than inside config.json,
so a config editor writing back a stale copy cannot silently flip it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.core.session_config import session_config_dir


def _share_path(session_id: str):
    return session_config_dir(session_id) / "share.json"


def read_share(session_id: str) -> dict[str, Any]:
    try:
        with _share_path(session_id).open("r", encoding="utf-8") as f:
            value = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"enabled": False, "updated_at": None}
    if not isinstance(value, dict):
        return {"enabled": False, "updated_at": None}
    return {"enabled": value.get("enabled") is True, "updated_at": value.get("updated_at")}


def is_shared(session_id: str) -> bool:
    return read_share(session_id)["enabled"]


def write_share(session_id: str, enabled: bool) -> dict[str, Any]:
    path = _share_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"enabled": bool(enabled), "updated_at": datetime.now(timezone.utc).isoformat()}
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)
    return value
