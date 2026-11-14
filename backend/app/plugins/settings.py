"""Plugin settings storage (README §9).

Global settings live in ``{augentia_home}/plugin_settings.json``::

    {"wechat": {"enabled": true, "config": {...}}}

The file is written atomically and only by the host. Session-level plugin
config (``sessions/<id>/config.json`` → ``plugins``) is added in P2.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from app.config import get_settings

logger = logging.getLogger("augentia.plugins.settings")

SECRET_MASK = "••••"
_lock = threading.Lock()


def augentia_home() -> Path:
    return Path(get_settings().augentia_home).expanduser()


def settings_path() -> Path:
    return augentia_home() / "plugin_settings.json"


def _read_all() -> dict[str, Any]:
    path = settings_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        # A broken file must not take every plugin down with it; start from
        # defaults and keep the file untouched until the next save.
        logger.error("cannot read %s, using defaults: %s", path, e)
        return {}
    if not isinstance(value, dict):
        logger.error("%s must be a JSON object, using defaults", path)
        return {}
    return value


def _write_all(data: dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _entry(data: dict[str, Any], plugin_id: str) -> dict[str, Any]:
    entry = data.get(plugin_id)
    return entry if isinstance(entry, dict) else {}


def is_enabled(plugin_id: str) -> bool:
    """Newly discovered plugins are enabled by default."""
    with _lock:
        value = _entry(_read_all(), plugin_id).get("enabled", True)
    return value if isinstance(value, bool) else True


def set_enabled(plugin_id: str, enabled: bool) -> None:
    with _lock:
        data = _read_all()
        entry = dict(_entry(data, plugin_id))
        entry["enabled"] = enabled
        data[plugin_id] = entry
        _write_all(data)


def stored_global_config(plugin_id: str) -> dict[str, Any]:
    """The saved global config as written, without defaults."""
    with _lock:
        value = _entry(_read_all(), plugin_id).get("config", {})
    return value if isinstance(value, dict) else {}


def write_global_config(plugin_id: str, config: dict[str, Any]) -> None:
    with _lock:
        data = _read_all()
        entry = dict(_entry(data, plugin_id))
        entry["config"] = config
        data[plugin_id] = entry
        _write_all(data)


# ---- schema helpers ----


def apply_defaults(schema: dict | None, value: dict[str, Any]) -> dict[str, Any]:
    """Copy of value with missing properties filled from the schema's defaults.

    Recurses into nested object properties. Only fills keys that are absent,
    so an explicit null stays null.
    """
    result = copy.deepcopy(value)
    if not schema:
        return result
    for key, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        if key not in result:
            if "default" in prop:
                result[key] = copy.deepcopy(prop["default"])
            elif prop.get("type") == "object" and prop.get("properties"):
                nested = apply_defaults(prop, {})
                if nested:
                    result[key] = nested
        elif prop.get("type") == "object" and isinstance(result[key], dict):
            result[key] = apply_defaults(prop, result[key])
    return result


def validation_errors(schema: dict | None, value: dict[str, Any]) -> list[str]:
    """Readable messages for every place value violates schema."""
    if not schema:
        return []
    validator = Draft202012Validator(schema)
    errors = []
    for error in sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path)):
        where = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"{where}: {error.message}")
    return errors


def _secret_paths(schema: dict | None, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = []
    for key, prop in ((schema or {}).get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        if prop.get("x-secret"):
            paths.append((*prefix, key))
        elif prop.get("type") == "object":
            paths.extend(_secret_paths(prop, (*prefix, key)))
    return paths


def _get_path(value: dict, path: tuple[str, ...]):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None, False
        value = value[key]
    return value, True


def _set_path(value: dict, path: tuple[str, ...], item) -> None:
    for key in path[:-1]:
        value = value.setdefault(key, {})
    value[path[-1]] = item


def mask_secrets(schema: dict | None, value: dict[str, Any]) -> dict[str, Any]:
    """Copy of value with every non-empty x-secret field replaced by SECRET_MASK."""
    result = copy.deepcopy(value)
    for path in _secret_paths(schema):
        current, present = _get_path(result, path)
        if present and current not in (None, ""):
            _set_path(result, path, SECRET_MASK)
    return result


def restore_secrets(schema: dict | None, new: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """Copy of new where x-secret fields still equal SECRET_MASK keep the old value.

    The client got masked values from mask_secrets; sending one back unchanged
    means "keep what is stored". A mask with nothing stored becomes absent.
    """
    result = copy.deepcopy(new)
    for path in _secret_paths(schema):
        current, present = _get_path(result, path)
        if not present or current != SECRET_MASK:
            continue
        previous, had = _get_path(old, path)
        if had:
            _set_path(result, path, copy.deepcopy(previous))
            continue
        *parent_path, key = path
        parent, _ = _get_path(result, tuple(parent_path))
        if isinstance(parent, dict):
            parent.pop(key, None)
    return result
