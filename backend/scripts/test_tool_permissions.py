import tempfile
from pathlib import Path

import _common  # noqa: F401 — sets sys.path

from app.adapters.tools.permissions import decide_tool_permission


def assert_decision(action: str, tool: str, args: dict, cwd: str, config: dict) -> None:
    decision = decide_tool_permission(tool, args, cwd, config)
    assert decision is not None
    assert decision.action == action, f"expected {action}, got {decision}"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        config = {
            "tool_permissions": {
                "default": "ask",
                "rules": [
                    {
                        "id": "allow-read-all",
                        "effect": "allow",
                        "tool": "read",
                        "paths": ["./**"],
                    },
                    {
                        "id": "allow-write-edit-docs",
                        "effect": "allow",
                        "tools": ["write", "edit"],
                        "paths": ["./docs/**"],
                    },
                    {
                        "id": "deny-sensitive",
                        "effect": "deny",
                        "tool": "*",
                        "paths": ["./.env", "./secrets/**"],
                    },
                    {
                        "id": "allow-safe-bash",
                        "effect": "allow",
                        "tool": "bash",
                        "commands": ["rg *", {"program": "git", "args": ["status"]}],
                    },
                    {
                        "id": "deny-git-push",
                        "effect": "deny",
                        "tool": "bash",
                        "commands": [{"program": "git", "args": ["push", "*"]}],
                    },
                ],
            }
        }

        assert_decision("allow", "read", {"file_path": "src/a.py"}, str(root), config)
        assert_decision("allow", "write", {"file_path": "docs/new.md"}, str(root), config)
        assert_decision("allow", "edit", {"file_path": "docs/a.md"}, str(root), config)
        assert_decision("deny", "read", {"file_path": ".env"}, str(root), config)
        assert_decision("deny", "edit", {"file_path": "docs/../.env"}, str(root), config)
        assert_decision("ask", "write", {"file_path": "notes/a.md"}, str(root), config)

        assert_decision("allow", "bash", {"command": "git status"}, str(root), config)
        assert_decision("allow", "bash", {"command": "rg foo backend"}, str(root), config)
        assert_decision("deny", "bash", {"command": "git push origin main"}, str(root), config)
        assert_decision("ask", "bash", {"command": "git status && git push"}, str(root), config)

        allow_all_bash_config = {
            "tool_permissions": {
                "default": "ask",
                "rules": [
                    {
                        "id": "allow-all-bash-dangerously",
                        "effect": "allow",
                        "tool": "bash",
                        "shell": "any",
                    }
                ],
            }
        }
        assert_decision("allow", "bash", {"command": "git status && git push"}, str(root), allow_all_bash_config)
        assert_decision("allow", "bash", {"command": "echo hi > out.txt"}, str(root), allow_all_bash_config)

    print("tool permission checks OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
