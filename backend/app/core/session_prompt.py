import logging
import os
import platform
import shutil
from pathlib import Path

from app.models.domain import AgentTemplate, Session

logger = logging.getLogger("augentia.session_prompt")


def _display_path(path: str) -> str:
    """Render local paths in the most shell-portable form for this host."""
    if os.name == "nt":
        return path.replace("\\", "/")
    return path


def runtime_context_prompt(session: Session) -> str:
    """Build session-specific runtime context appended to the system prompt."""
    os_name = platform.platform()
    working_dir = _display_path(session.working_dir or os.getcwd())
    shell_note = ""
    if os.name == "nt":
        bash_path = os.environ.get("AGENT_BASH_PATH") or shutil.which("bash")
        if bash_path:
            shell_note = (
                "\n- The bash tool resolves to Git Bash-compatible execution on this "
                "Windows host. POSIX shell syntax is supported."
                "\n- Use Windows drive-slash paths such as E:/Projects/AgentZoo "
                "for shell-compatible local paths."
                f"\n- Resolved bash executable: {_display_path(bash_path)}"
            )
        else:
            shell_note = (
                "\n- The bash tool could not resolve a bash executable from "
                "AGENT_BASH_PATH or PATH, so it will fall back to the platform "
                "default shell. POSIX shell syntax may not be supported."
            )

    return (
        "# Runtime context\n"
        f"- Operating system: {os_name}\n"
        f"- Current working directory: {working_dir}\n"
        f"- Session start time: {session.created_at.isoformat()}"
        f"{shell_note}"
    )


def append_runtime_context(agent: AgentTemplate) -> bool:
    """Whether sessions of this agent append the runtime context block.

    Defaults to True (the historical behavior). Agents that want a clean
    system prompt — e.g. benchmarking against a fixed template — can set
    config.append_runtime_context = false on the agent template.
    """
    value = (agent.config or {}).get("append_runtime_context", True)
    return bool(value)


def effective_system_prompt(
    agent: AgentTemplate,
    session: Session,
    additional_prompt: str | None = None,
    additional_prompt_path: str | None = None,
) -> str:
    system_prompt = agent.system_prompt
    if additional_prompt:
        system_prompt = system_prompt + "\n\n" + additional_prompt
    if additional_prompt_path:
        try:
            extra_content = Path(additional_prompt_path).read_text(encoding="utf-8")
            system_prompt = system_prompt + "\n\n" + extra_content
        except (OSError, UnicodeDecodeError) as e:
            logger.exception("failed to read additional_prompt_path=%s", additional_prompt_path)
            raise ValueError(
                f"failed to read additional system prompt from {additional_prompt_path}: {e}"
            )
    if not append_runtime_context(agent):
        return system_prompt
    return system_prompt + "\n\n" + runtime_context_prompt(session)
