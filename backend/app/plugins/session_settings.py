"""Per-session plugin settings (app/plugins/README.md §9).

A session's ``config.json`` holds a ``plugins`` section::

    "plugins": {"wechat": {"enabled": true, "command_prefix": "\\\\cmd"}}

``enabled`` switches the plugin's hooks and session services on for that
session; every other key is the plugin's session config, validated against
the manifest's ``config.session`` schema.

Effective values, later wins:
1. ``config.session`` schema defaults; ``session.default_enabled`` for ``enabled``.
2. The session's ``plugins.<id>`` section. A new session starts with a copy of
   the agent's ``config.plugins`` (core/session_config.default_session_config).

Validation only covers sections that changed in a write, so a section that
became invalid after a plugin upgrade does not block unrelated edits (tool
permissions, another plugin). Sections of plugins that are not installed are
kept untouched.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from app.plugins import settings
from app.plugins.manifest import PluginManifest

if TYPE_CHECKING:
    from app.plugins.host import PluginHost

ENABLED_KEY = "enabled"


def plugins_section(config: dict[str, Any] | None) -> dict[str, Any]:
    section = (config or {}).get("plugins")
    return section if isinstance(section, dict) else {}


def split_entry(entry: Any) -> tuple[bool | None, dict[str, Any]]:
    """(stored enabled flag or None, config keys) of one plugins.<id> entry."""
    if not isinstance(entry, dict):
        return None, {}
    enabled = entry.get(ENABLED_KEY)
    rest = {k: v for k, v in entry.items() if k != ENABLED_KEY}
    return (enabled if isinstance(enabled, bool) else None), rest


def effective(manifest: PluginManifest | None, entry: Any) -> tuple[bool, dict[str, Any]]:
    """(enabled, config) for one plugin in one session."""
    stored_enabled, rest = split_entry(entry)
    default_enabled = manifest.session.default_enabled if manifest else False
    schema = manifest.config.session if manifest else None
    enabled = default_enabled if stored_enabled is None else stored_enabled
    return enabled, settings.apply_defaults(schema, rest)


def entry_errors(manifest: PluginManifest, entry: Any) -> list[str]:
    """Problems with one plugins.<id> entry, as "<key>: message" strings."""
    if not isinstance(entry, dict):
        return ["(root): must be an object"]
    errors = []
    if ENABLED_KEY in entry and not isinstance(entry[ENABLED_KEY], bool):
        errors.append(f"{ENABLED_KEY}: must be a boolean")
    _, rest = split_entry(entry)
    schema = manifest.config.session
    if schema is None:
        if rest:
            errors.append(f"(root): plugin has no session config; unexpected keys {sorted(rest)}")
        return errors
    return errors + settings.validation_errors(schema, settings.apply_defaults(schema, rest))


class SessionPluginsError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def prepare_for_write(
    host: "PluginHost",
    new: Any,
    old: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate a plugins section about to be saved; returns it with secrets restored.

    ``new`` is what the client sent (possibly carrying masked secrets),
    ``old`` the stored section. Only entries that differ from ``old`` after
    restoring secrets are validated. Raises SessionPluginsError.
    """
    if new is None:
        return {}
    if not isinstance(new, dict):
        raise SessionPluginsError(["plugins: must be an object"])
    old = old or {}
    result: dict[str, Any] = {}
    errors: list[str] = []
    for plugin_id, entry in new.items():
        manifest = host.manifest_of(plugin_id)
        if manifest is None:
            # Not installed (or invalid manifest): keep as is, nothing to check against.
            result[plugin_id] = copy.deepcopy(entry)
            continue
        if isinstance(entry, dict):
            previous = old.get(plugin_id)
            old_entry: dict[str, Any] = previous if isinstance(previous, dict) else {}
            entry = settings.restore_secrets(manifest.config.session, entry, old_entry)
        if entry != old.get(plugin_id):
            errors.extend(f"plugins.{plugin_id}.{e}" for e in entry_errors(manifest, entry))
        result[plugin_id] = entry
    if errors:
        raise SessionPluginsError(errors)
    return result


def mask(host: "PluginHost", section: dict[str, Any]) -> dict[str, Any]:
    """Copy of a plugins section with x-secret values masked."""
    result: dict[str, Any] = {}
    for plugin_id, entry in section.items():
        manifest = host.manifest_of(plugin_id)
        if manifest is not None and isinstance(entry, dict):
            result[plugin_id] = settings.mask_secrets(manifest.config.session, entry)
        else:
            result[plugin_id] = copy.deepcopy(entry)
    return result


def mask_config(host: "PluginHost", config: dict[str, Any]) -> dict[str, Any]:
    """A whole session config with its plugins section masked (for GET /config)."""
    if "plugins" not in config or not isinstance(config["plugins"], dict):
        return config
    return {**config, "plugins": mask(host, config["plugins"])}
