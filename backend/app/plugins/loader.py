"""Discover plugin directories and import plugin code (README §2, §3)."""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.metadata
import importlib.util
import logging
import sys
import traceback
import types
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.plugins import settings
from app.plugins.manifest import PluginManifest, dependency_name
from app.plugins.sdk import Capabilities, Plugin, collect_capabilities

logger = logging.getLogger("augentia.plugins.loader")

PACKAGE_ROOT = "augentia_plugins"


@dataclass(frozen=True)
class DiscoveredPlugin:
    dir_name: str
    path: Path


def plugin_roots() -> list[Path]:
    """{augentia_home}/plugins first, then AUGENTIA_PLUGIN_PATH entries in order."""
    roots = [settings.augentia_home() / "plugins"]
    for raw in get_settings().plugin_path:
        path = Path(raw).expanduser()
        if path not in roots:
            roots.append(path)
    return roots


def discover() -> list[DiscoveredPlugin]:
    """Every directory holding a plugin.json, in scan order (roots, then name)."""
    found: list[DiscoveredPlugin] = []
    for root in plugin_roots():
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if child.is_dir() and not child.name.startswith((".", "_")) and (child / "plugin.json").is_file():
                found.append(DiscoveredPlugin(child.name, child.resolve()))
    return found


def missing_dependencies(manifest: PluginManifest) -> list[str]:
    missing = []
    for requirement in manifest.python_dependencies:
        try:
            importlib.metadata.distribution(dependency_name(requirement))
        except importlib.metadata.PackageNotFoundError:
            missing.append(requirement)
    return missing


class PluginImportError(Exception):
    """The plugin's code could not be imported or does not define a valid Plugin."""


@dataclass
class ImportedPlugin:
    instance: Plugin
    capabilities: Capabilities


def package_name(plugin_id: str) -> str:
    return f"{PACKAGE_ROOT}.{plugin_id}"


def _ensure_root_package() -> None:
    if PACKAGE_ROOT not in sys.modules:
        root = types.ModuleType(PACKAGE_ROOT)
        root.__path__ = []  # namespace-like: children are registered explicitly
        sys.modules[PACKAGE_ROOT] = root


def import_plugin(manifest: PluginManifest, plugin_dir: Path) -> ImportedPlugin:
    """Import the entry module as augentia_plugins.<id>.<module> and instantiate the class.

    The plugin directory becomes a package, so plugin code can use relative
    imports between its own modules. Any previous import of the same plugin
    is purged first, so disable + enable picks up code changes.
    """
    unload_modules(manifest.id)
    # Directory listings are cached per path finder; without this, modules
    # added to the plugin dir since the last import are not found.
    importlib.invalidate_caches()
    _ensure_root_package()
    name = package_name(manifest.id)
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    package = importlib.util.module_from_spec(spec)
    package.__path__ = [str(plugin_dir)]
    sys.modules[name] = package
    try:
        module = importlib.import_module(f"{name}.{manifest.entry_module}")
    except BaseException as e:
        unload_modules(manifest.id)
        if not isinstance(e, Exception):
            raise
        raise PluginImportError(_describe_exception(e, plugin_dir)) from e

    cls = getattr(module, manifest.entry_class, None)
    if cls is None:
        unload_modules(manifest.id)
        raise PluginImportError(f"entry {manifest.entry!r}: module has no attribute {manifest.entry_class!r}")
    if not isinstance(cls, type) or not issubclass(cls, Plugin):
        unload_modules(manifest.id)
        raise PluginImportError(f"entry {manifest.entry!r} must be a subclass of app.plugins.sdk.Plugin")
    try:
        capabilities = collect_capabilities(cls, manifest.id)
        instance = cls()
    except Exception as e:
        unload_modules(manifest.id)
        raise PluginImportError(_describe_exception(e, plugin_dir)) from e
    return ImportedPlugin(instance, capabilities)


def unload_modules(plugin_id: str) -> None:
    name = package_name(plugin_id)
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]


def _describe_exception(error: BaseException, plugin_dir: Path) -> str:
    """One-line error plus the innermost frame inside the plugin directory."""
    message = f"{type(error).__name__}: {error}"
    plugin_root = str(plugin_dir)
    frames = [f for f in traceback.extract_tb(error.__traceback__) if f.filename.startswith(plugin_root)]
    if isinstance(error, SyntaxError) and error.filename and error.filename.startswith(plugin_root):
        rel = Path(error.filename).relative_to(plugin_dir)
        return f"SyntaxError: {error.msg} ({rel}:{error.lineno})"
    if frames:
        frame = frames[-1]
        rel = Path(frame.filename).relative_to(plugin_dir)
        return f"{message} ({rel}:{frame.lineno})"
    return message
