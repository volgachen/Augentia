import logging
from typing import TYPE_CHECKING

from app.adapters.cagent_adapter import CAgentAdapter
from app.adapters.openai_tool_use import OpenAIToolUseAdapter
from app.adapters.registry import AdapterRegistry
from app.core.attachments import agent_engine
from app.core.runner import SessionRunner
from app.core.session_config import ensure_session_config
from app.core.session_prompt import effective_system_prompt
from app.db.interface import IAgentDatabase
from app.models.domain import AgentTemplate, AgentType, Session, SessionStatus

if TYPE_CHECKING:
    from app.adapters.base import BaseAgentAdapter

logger = logging.getLogger("augentia.session_runtime")


async def ensure_system_prompt_snapshot(
    session: Session,
    agent: AgentTemplate,
    db: IAgentDatabase,
    additional_prompt: str | None = None,
    additional_prompt_path: str | None = None,
) -> Session:
    if session.system_prompt_snapshot:
        return session
    prompt = effective_system_prompt(agent, session, additional_prompt, additional_prompt_path)
    return await db.update_session_system_prompt_snapshot(session.id, prompt)


def _with_agent_keys(session_config: dict, agent: AgentTemplate, keys) -> dict:
    """Session config plus the given agent-level config keys (agent wins)."""
    config = dict(session_config)
    for key in keys:
        if key in (agent.config or {}):
            config[key] = agent.config[key]
    return config


def build_adapter(session: Session, agent: AgentTemplate, db: IAgentDatabase | None = None) -> "BaseAgentAdapter":
    session_config = ensure_session_config(session.id, agent.config)
    if agent.agent_type == AgentType.TOOL_USE:
        # config.engine selects the agent loop: "cagent" (the default) runs the
        # CAgent framework loop (persist-first history, paired tool results,
        # turn-end marks, image input); "legacy" runs the original in-adapter
        # loop. Both read and write the same persisted history format.
        engine = agent_engine(agent)
        if engine == "cagent":
            config = _with_agent_keys(session_config, agent, (
                "max_steps", "compression_token_threshold", "compression_turn_threshold",
                "turn_compression_tool_call_threshold", "turn_compression_instruction",
                "compression_instruction", "compression_model", "compression_send_tools",
                "model_params", "compression_model_params",
                "vision", "image_detail", "tool_image_limit", "tool_image_mode"))
            return CAgentAdapter(
                db=db,
                tool_names=agent.tool_names,
                model=agent.openai_model,
                base_url=agent.openai_base_url,
                session_id=session.id,
                working_dir=session.working_dir,
                config=config,
            )
        return OpenAIToolUseAdapter(
            tool_names=agent.tool_names,
            model=agent.openai_model,
            base_url=agent.openai_base_url,
            session_id=session.id,
            working_dir=session.working_dir,
            config=_with_agent_keys(session_config, agent, ("model_params",)),
        )
    raise RuntimeError(f"unsupported agent_type: {agent.agent_type}")


async def build_runner(
    session: Session,
    agent: AgentTemplate,
    db: IAgentDatabase,
    registry: AdapterRegistry,
    additional_prompt: str | None = None,
    additional_prompt_path: str | None = None,
) -> SessionRunner:
    """Start a runner and restore persisted history when rehydrating a session."""
    session = await ensure_system_prompt_snapshot(
        session, agent, db, additional_prompt, additional_prompt_path
    )
    adapter = build_adapter(session, agent, db)
    await adapter.start(session.system_prompt_snapshot or "")
    history = await db.get_messages(session.id)
    if history:
        await adapter.restore_history(history)
    runner = SessionRunner(session.id, adapter, db)
    await runner.start()
    registry.register(session.id, runner)
    return runner


async def get_or_rehydrate(
    session: Session,
    db: IAgentDatabase,
    registry: AdapterRegistry,
) -> SessionRunner | None:
    """Return a live runner or rebuild a persisted tool-use session."""
    try:
        return registry.get(session.id)
    except KeyError:
        pass

    agent = await db.get_agent(session.agent_id)
    if agent.agent_type != AgentType.TOOL_USE:
        return None
    logger.info("rehydrating runner for session=%s", session.id)
    try:
        return await build_runner(
            session,
            agent,
            db,
            registry,
            additional_prompt=session.additional_prompt,
            additional_prompt_path=session.additional_prompt_path,
        )
    except (ValueError, RuntimeError):
        logger.exception("rehydrate failed session=%s", session.id)
        await db.update_session_status(session.id, SessionStatus.ERROR)
        return None
