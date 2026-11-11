from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
import fnmatch
import shlex

import app.adapters.tools  # noqa: F401 — triggers tool registration
from app.adapters.tools.registry import list_available

PermissionAction = Literal["allow", "deny", "ask"]

_FILE_PATH_ARG_BY_TOOL = {
    "read": "file_path",
    "write": "file_path",
    "edit": "file_path",
}

_SHELL_FEATURE_TOKENS = ("&&", "||", ";", "|", ">", "<", "$(", "`", "\n")


@dataclass(frozen=True)
class ToolPermissionDecision:
    action: PermissionAction
    reason: str
    rule_id: str | None = None


@dataclass(frozen=True)
class ToolPermissionExplanation:
    action: PermissionAction
    reason: str
    rule_id: str | None = None
    resolved_path: str | None = None


def decide_tool_permission(
    tool_name: str,
    tool_args: dict[str, Any],
    working_dir: str | None,
    config: dict[str, Any] | None,
) -> ToolPermissionDecision | None:
    """Return a policy decision for configured tool permissions.

    This first implementation intentionally handles only filesystem tools
    (read/write/edit). Returning None means "no new-style policy applies" so the
    caller should fall back to the legacy requires_approval/tool_approvals flow.
    """
    explanation = explain_tool_permission(tool_name, tool_args, working_dir, config)
    if explanation is None:
        return None
    return ToolPermissionDecision(explanation.action, explanation.reason, explanation.rule_id)


def explain_tool_permission(
    tool_name: str,
    tool_args: dict[str, Any],
    working_dir: str | None,
    config: dict[str, Any] | None,
) -> ToolPermissionExplanation | None:
    permissions = (config or {}).get("tool_permissions")
    if not isinstance(permissions, dict):
        return None

    if tool_name == "bash":
        return _explain_bash_permission(tool_args, permissions)

    path_arg = _FILE_PATH_ARG_BY_TOOL.get(tool_name)
    if path_arg is None:
        return None

    raw_path = tool_args.get(path_arg)
    if not isinstance(raw_path, str) or not raw_path:
        return ToolPermissionExplanation("ask", f"missing or invalid path argument '{path_arg}'")

    try:
        resolved_path = _resolve_tool_path(raw_path, working_dir)
    except OSError as e:
        return ToolPermissionExplanation("ask", f"could not resolve path: {e}")

    rules = permissions.get("rules", [])
    matching_allow: ToolPermissionExplanation | None = None
    if isinstance(rules, list):
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            if not _tool_matches(rule, tool_name):
                continue
            paths = rule.get("paths")
            if not isinstance(paths, list):
                continue
            if not _path_matches_any(resolved_path, paths, working_dir):
                continue

            effect = rule.get("effect")
            rule_id = rule.get("id") if isinstance(rule.get("id"), str) else None
            label = rule_id or f"rule_index={index}"
            if effect == "deny":
                return ToolPermissionExplanation("deny", f"matched deny rule {label}", rule_id, str(resolved_path))
            if effect == "allow" and matching_allow is None:
                matching_allow = ToolPermissionExplanation("allow", f"matched allow rule {label}", rule_id, str(resolved_path))

    if matching_allow is not None:
        return matching_allow

    default = permissions.get("default", "ask")
    if default in ("allow", "deny", "ask"):
        return ToolPermissionExplanation(default, "no matching rule; using tool_permissions.default", resolved_path=str(resolved_path))
    return ToolPermissionExplanation("ask", "invalid tool_permissions.default; using ask", resolved_path=str(resolved_path))


def _explain_bash_permission(
    tool_args: dict[str, Any],
    permissions: dict[str, Any],
) -> ToolPermissionExplanation:
    command = tool_args.get("command")
    if not isinstance(command, str) or not command.strip():
        return ToolPermissionExplanation("ask", "missing or invalid bash command")
    shell_any_allow = _match_bash_shell_any_rule(permissions)
    if any(token in command for token in _SHELL_FEATURE_TOKENS):
        if shell_any_allow is not None:
            return shell_any_allow
        return ToolPermissionExplanation("ask", "bash command uses shell features; asking for confirmation")
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return ToolPermissionExplanation("ask", f"could not parse bash command: {e}")
    if not argv:
        return ToolPermissionExplanation("ask", "empty bash command")

    rules = permissions.get("rules", [])
    matching_allow: ToolPermissionExplanation | None = None
    if isinstance(rules, list):
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            if not _tool_matches(rule, "bash"):
                continue
            commands = rule.get("commands")
            if not isinstance(commands, list):
                continue
            if not _command_matches_any(argv, commands):
                continue
            effect = rule.get("effect")
            rule_id = rule.get("id") if isinstance(rule.get("id"), str) else None
            label = rule_id or f"rule_index={index}"
            if effect == "deny":
                return ToolPermissionExplanation("deny", f"matched deny rule {label}", rule_id)
            if effect == "allow" and matching_allow is None:
                matching_allow = ToolPermissionExplanation("allow", f"matched allow rule {label}", rule_id)

    if matching_allow is not None:
        return matching_allow
    if shell_any_allow is not None:
        return shell_any_allow
    default = permissions.get("default", "ask")
    if default in ("allow", "deny", "ask"):
        return ToolPermissionExplanation(default, "no matching rule; using tool_permissions.default")
    return ToolPermissionExplanation("ask", "invalid tool_permissions.default; using ask")


def _match_bash_shell_any_rule(permissions: dict[str, Any]) -> ToolPermissionExplanation | None:
    rules = permissions.get("rules", [])
    matching_allow: ToolPermissionExplanation | None = None
    if not isinstance(rules, list):
        return None
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        if not _tool_matches(rule, "bash"):
            continue
        if rule.get("shell") != "any":
            continue
        effect = rule.get("effect")
        rule_id = rule.get("id") if isinstance(rule.get("id"), str) else None
        label = rule_id or f"rule_index={index}"
        if effect == "deny":
            return ToolPermissionExplanation("deny", f"matched deny shell-any rule {label}", rule_id)
        if effect == "allow" and matching_allow is None:
            matching_allow = ToolPermissionExplanation("allow", f"matched allow shell-any rule {label}", rule_id)
    return matching_allow


def _command_matches_any(argv: list[str], patterns: list[Any]) -> bool:
    return any(_command_matches(argv, pattern) for pattern in patterns)


def _command_matches(argv: list[str], pattern: Any) -> bool:
    if isinstance(pattern, str):
        try:
            pattern_argv = shlex.split(pattern)
        except ValueError:
            return False
        return _argv_matches(argv, pattern_argv)
    if not isinstance(pattern, dict):
        return False
    program = pattern.get("program")
    args = pattern.get("args", [])
    if not isinstance(program, str) or argv[0] != program:
        return False
    if args == "any":
        return True
    if not isinstance(args, list):
        return False
    return _argv_matches(argv[1:], args)


def _argv_matches(argv: list[str], pattern: list[Any]) -> bool:
    if pattern and pattern[-1] == "*":
        prefix = pattern[:-1]
        return len(argv) >= len(prefix) and all(
            isinstance(expected, str) and actual == expected
            for actual, expected in zip(argv[: len(prefix)], prefix)
        )
    return len(argv) == len(pattern) and all(
        isinstance(expected, str) and actual == expected
        for actual, expected in zip(argv, pattern)
    )


def _tool_matches(rule: dict[str, Any], tool_name: str) -> bool:
    rule_tool = rule.get("tool")
    rule_tools = rule.get("tools")
    if rule_tool == "*" or rule_tool == tool_name:
        return True
    if isinstance(rule_tools, list):
        return "*" in rule_tools or tool_name in rule_tools
    return False


def _resolve_tool_path(path: str, working_dir: str | None) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute() and working_dir:
        p = Path(working_dir).expanduser() / p
    return p.resolve(strict=False)


def _path_matches_any(path: Path, patterns: list[Any], working_dir: str | None) -> bool:
    return any(
        isinstance(pattern, str) and _path_matches(path, pattern, working_dir)
        for pattern in patterns
    )


def _path_matches(path: Path, pattern: str, working_dir: str | None) -> bool:
    # Treat relative patterns as workspace-relative when a working_dir exists.
    # Absolute tool-call paths are still normalized before matching, so ../ cannot
    # escape a narrower allow pattern such as ./docs/**.
    pattern_path = Path(pattern).expanduser()
    if pattern_path.is_absolute() or working_dir:
        if not pattern_path.is_absolute():
            pattern_path = Path(working_dir or ".").expanduser() / pattern_path
        normalized_pattern = _normalize_glob_pattern(pattern_path)
        return fnmatch.fnmatch(str(path), normalized_pattern)

    # Legacy no-working-dir case: match normalized relative-looking strings.
    return fnmatch.fnmatch(str(path), pattern)


def _normalize_glob_pattern(path: Path) -> str:
    # Path.resolve() cannot be used directly because glob metacharacters do not
    # need to exist on disk and should not be treated as literal path components.
    parts: list[str] = []
    for part in path.parts:
        if any(ch in part for ch in "*?["):
            parts.append(part)
        else:
            if not parts:
                parts.append(part)
            else:
                current = Path(*parts, part)
                parts = list(current.resolve(strict=False).parts)
    return str(Path(*parts))


def validate_tool_permissions_config(permissions: object) -> None:
    if permissions is None:
        return
    if not isinstance(permissions, dict):
        raise ValueError("config.tool_permissions must be an object")

    default = permissions.get("default", "ask")
    if default not in ("allow", "deny", "ask"):
        raise ValueError("config.tool_permissions.default must be one of: allow, deny, ask")

    rules = permissions.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("config.tool_permissions.rules must be an array")

    available = set(list_available())
    supported_tools = set(_FILE_PATH_ARG_BY_TOOL) | {"bash"}
    for index, rule in enumerate(rules):
        prefix = f"config.tool_permissions.rules[{index}]"
        if not isinstance(rule, dict):
            raise ValueError(f"{prefix} must be an object")
        effect = rule.get("effect")
        if effect not in ("allow", "deny"):
            raise ValueError(f"{prefix}.effect must be allow or deny")
        tool = rule.get("tool")
        tools = rule.get("tools")
        if tool is None and tools is None:
            raise ValueError(f"{prefix} must include tool or tools")
        selected_tools: set[str] = set()
        if tool is not None:
            selected_tools.update(_validate_tool_selector_item(tool, available, supported_tools, f"{prefix}.tool"))
        if tools is not None:
            if not isinstance(tools, list) or not tools:
                raise ValueError(f"{prefix}.tools must be a non-empty array")
            for tool_index, item in enumerate(tools):
                selected_tools.update(_validate_tool_selector_item(item, available, supported_tools, f"{prefix}.tools[{tool_index}]"))
        if "*" in (tool, *(tools if isinstance(tools, list) else [])):
            if "paths" in rule and "commands" not in rule and "shell" not in rule:
                selected_tools &= set(_FILE_PATH_ARG_BY_TOOL)
            elif ("commands" in rule or "shell" in rule) and "paths" not in rule:
                selected_tools &= {"bash"}
        if selected_tools & set(_FILE_PATH_ARG_BY_TOOL):
            paths = rule.get("paths")
            if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
                raise ValueError(f"{prefix}.paths must be a non-empty string array")
        if "bash" in selected_tools:
            shell = rule.get("shell")
            if shell is not None and shell != "any":
                raise ValueError(f"{prefix}.shell must be 'any' when present")
            commands = rule.get("commands")
            if shell != "any":
                if not isinstance(commands, list) or not commands:
                    raise ValueError(f"{prefix}.commands must be a non-empty array for bash rules")
                for command_index, command in enumerate(commands):
                    _validate_command_pattern(command, f"{prefix}.commands[{command_index}]")
            elif commands is not None:
                raise ValueError(f"{prefix} cannot combine shell: 'any' with commands")
        rule_id = rule.get("id")
        if rule_id is not None and not isinstance(rule_id, str):
            raise ValueError(f"{prefix}.id must be a string when present")


def _validate_tool_selector_item(
    tool: object,
    available: set[str],
    supported: set[str],
    field: str,
) -> set[str]:
    if tool == "*":
        return set(supported)
    if tool not in available:
        raise ValueError(f"{field} is unknown: {tool}")
    if tool not in supported:
        raise ValueError(f"{field} is not supported by tool_permissions yet: {tool}")
    return {str(tool)}


def _validate_command_pattern(command: object, field: str) -> None:
    if isinstance(command, str):
        try:
            parts = shlex.split(command)
        except ValueError as e:
            raise ValueError(f"{field} is invalid: {e}") from e
        if not parts:
            raise ValueError(f"{field} must not be empty")
        return
    if not isinstance(command, dict):
        raise ValueError(f"{field} must be a string or object")
    program = command.get("program")
    args = command.get("args", [])
    if not isinstance(program, str) or not program:
        raise ValueError(f"{field}.program must be a non-empty string")
    if args != "any" and not (
        isinstance(args, list) and all(isinstance(arg, str) for arg in args)
    ):
        raise ValueError(f"{field}.args must be 'any' or a string array")
