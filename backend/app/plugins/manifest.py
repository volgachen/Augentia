"""plugin.json model and validation (README §3)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

SUPPORTED_API_VERSIONS = (1,)
PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
# Fixed path segments under /api/v1/plugins; a plugin with one of these ids
# would be shadowed by (or shadow) those routes.
RESERVED_PLUGIN_IDS = frozenset({"reload", "stream"})
_ENTRY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")
_ROUTE_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (/[^\s]*)$")
# Distribution names as pip accepts them, optionally with a version spec we ignore.
_DEPENDENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


class ManifestError(ValueError):
    """plugin.json is missing, unreadable or invalid."""


class PluginConfigSchemas(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    global_: dict[str, Any] | None = Field(default=None, alias="global")
    session: dict[str, Any] | None = None

    @field_validator("global_", "session")
    @classmethod
    def _valid_object_schema(cls, value):
        if value is None:
            return value
        if value.get("type") != "object":
            raise ValueError("config schema must have type 'object'")
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as e:
            raise ValueError(f"not a valid JSON Schema: {e.message}") from e
        return value


class PluginSessionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_enabled: bool = False


class PluginHttpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_routes: list[str] = Field(default_factory=list)

    @field_validator("public_routes")
    @classmethod
    def _valid_routes(cls, routes: list[str]) -> list[str]:
        for route in routes:
            if not _ROUTE_RE.match(route):
                raise ValueError(f"public route {route!r} must look like 'POST /path'")
        return routes


class PluginManifest(BaseModel):
    # Unknown keys are rejected so typos (e.g. "subscriptons") surface instead
    # of being silently ignored, as happened with v1's "dependencies".
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=50)
    api_version: int
    description: str = ""
    author: str = ""
    entry: str
    python_dependencies: list[str] = Field(default_factory=list)
    config: PluginConfigSchemas = Field(default_factory=PluginConfigSchemas)
    session: PluginSessionSettings = Field(default_factory=PluginSessionSettings)
    http: PluginHttpSettings = Field(default_factory=PluginHttpSettings)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not PLUGIN_ID_RE.match(value):
            raise ValueError(f"must match {PLUGIN_ID_RE.pattern}")
        if "__" in value:
            raise ValueError("must not contain '__' (it separates plugin id and tool name)")
        if value in RESERVED_PLUGIN_IDS:
            raise ValueError(f"{value!r} is reserved (it is an API path segment under /plugins)")
        return value

    @field_validator("api_version")
    @classmethod
    def _supported_api(cls, value: int) -> int:
        if value not in SUPPORTED_API_VERSIONS:
            supported = ", ".join(map(str, SUPPORTED_API_VERSIONS))
            raise ValueError(f"api_version {value} is not supported (supported: {supported})")
        return value

    @field_validator("entry")
    @classmethod
    def _valid_entry(cls, value: str) -> str:
        if not _ENTRY_RE.match(value):
            raise ValueError("must look like 'module:ClassName' (module path relative to the plugin dir)")
        return value

    @field_validator("python_dependencies")
    @classmethod
    def _valid_dependencies(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _DEPENDENCY_RE.match(value):
                raise ValueError(f"invalid dependency {value!r}")
        return values

    @property
    def entry_module(self) -> str:
        return self.entry.split(":", 1)[0]

    @property
    def entry_class(self) -> str:
        return self.entry.split(":", 1)[1]


def dependency_name(requirement: str) -> str:
    """Distribution name from a requirement string ("wechatbot>=1.0" -> "wechatbot")."""
    match = _DEPENDENCY_RE.match(requirement)
    return match.group(0) if match else requirement


def read_manifest(plugin_dir: Path) -> PluginManifest:
    path = plugin_dir / "plugin.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ManifestError("plugin.json not found") from e
    except (OSError, UnicodeDecodeError) as e:
        raise ManifestError(f"cannot read plugin.json: {e}") from e
    except json.JSONDecodeError as e:
        raise ManifestError(f"plugin.json is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ManifestError("plugin.json must be a JSON object")
    try:
        manifest = PluginManifest.model_validate(raw)
    except ValidationError as e:
        raise ManifestError(_format_validation_error(e)) from e
    if manifest.id != plugin_dir.name:
        raise ManifestError(f"id {manifest.id!r} must equal the directory name {plugin_dir.name!r}")
    return manifest


def _format_validation_error(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        where = ".".join(str(p) for p in item["loc"]) or "plugin.json"
        message = item["msg"].removeprefix("Value error, ")
        parts.append(f"{where}: {message}")
    return "; ".join(parts)
