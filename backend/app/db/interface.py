from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, List
from app.core.borrowed import BORROWED_FROM, as_of, aware
from app.models.domain import (
    AgentTemplate, Session, SessionStatus, Message, MessageRole, SystemPrompt,
    PluginInstance, PluginLog, PluginRun, PluginStatus, Task, TaskStatus,
)

# Sentinel for update_task(owner=...) so callers can distinguish "leave owner
# unchanged" (the default) from "clear the owner" (explicitly passing None).
_UNSET: Any = object()


class IAgentDatabase(ABC):
    @abstractmethod
    async def list_agents(self) -> List[AgentTemplate]: pass

    @abstractmethod
    async def get_agent(self, agent_id: str) -> AgentTemplate: pass

    @abstractmethod
    async def create_agent(self, template: AgentTemplate) -> AgentTemplate: pass

    @abstractmethod
    async def update_agent(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        # A changed prompt is stored as a new system_prompts version; an
        # identical one leaves the current version in place.
        system_prompt: str | None = None,
        tool_names: list[str] | None = None,
        config: dict | None = None,
        openai_model: str | None = None,
        openai_base_url: str | None = None,
    ) -> AgentTemplate: pass

    @abstractmethod
    async def delete_agent(self, agent_id: str) -> None: pass

    @abstractmethod
    async def create_session(
        self,
        agent_id: str,
        working_dir: str | None = None,
        *,
        session_id: str | None = None,
        title: str | None = None,
        parent_session_id: str | None = None,
        additional_prompt: str | None = None,
        additional_prompt_path: str | None = None,
        system_prompt_snapshot: str | None = None,
        system_prompt_id: str | None = None,
        model: str | None = None,
        model_params: dict | None = None,
        branched_from_session_id: str | None = None,
        branched_from_message_id: str | None = None,
        branched_cutoff_at: datetime | None = None,
        join_into_session_id: str | None = None,
    ) -> Session:
        """system_prompt_snapshot stores a new first version; system_prompt_id
        instead shares an existing version (diverge). Neither: no prompt yet.
        model / model_params: the session's model override (None: the agent's)."""

    @abstractmethod
    async def get_session(self, session_id: str, *, include_deleted: bool = False) -> Session:
        """KeyError when unknown; a soft-deleted session only with include_deleted
        (a branch still reads the history it borrowed from a deleted source)."""

    @abstractmethod
    async def update_session_system_prompt_snapshot(
        self,
        session_id: str,
        system_prompt_snapshot: str,
    ) -> Session:
        """Make `system_prompt_snapshot` the session's prompt: a new version
        chained to the current one (no-op if the text is unchanged)."""

    @abstractmethod
    async def get_system_prompt(self, prompt_id: str) -> SystemPrompt:
        """One version (raises KeyError)."""

    async def system_prompt_history(self, prompt_id: str) -> List[SystemPrompt]:
        """`prompt_id` and every earlier version, newest first (previous_id chain)."""
        history: List[SystemPrompt] = []
        seen: set[str] = set()
        current: str | None = prompt_id
        while current and current not in seen:
            seen.add(current)
            version = await self.get_system_prompt(current)
            history.append(version)
            current = version.previous_id
        return history

    @abstractmethod
    async def update_session_title(self, session_id: str, title: str) -> Session: pass

    @abstractmethod
    async def update_session_working_dir(self, session_id: str, working_dir: str | None) -> Session: pass

    @abstractmethod
    async def update_session_model(
        self, session_id: str, model: str | None, model_params: dict | None,
    ) -> Session:
        """Set both override fields (None clears one); KeyError if unknown."""

    @abstractmethod
    async def update_session_dispatch_mode(self, session_id: str, enabled: bool) -> Session:
        """Turn dispatch mode on or off; KeyError if the session is unknown."""

    @abstractmethod
    async def pending_joins(self) -> dict[str, int]:
        """{session id: live fork-dispatch branches not joined back into it yet}.

        Deleted branches don't count: deleting one gives up on its result
        (DELETE /sessions/{id} joins whatever it already finished first).
        """

    @abstractmethod
    async def set_joined_message(self, session_id: str, message_id: str) -> Session:
        """Record that this fork-dispatch branch was joined back (row id in its target)."""

    @abstractmethod
    async def update_session_parent(self, session_id: str, parent_session_id: str | None) -> Session:
        """Set or clear (None) the session's parent; KeyError if the session is unknown."""

    @abstractmethod
    async def list_sessions(self) -> List[Session]: pass

    @abstractmethod
    async def update_session_status(self, session_id: str, status: SessionStatus) -> Session: pass

    @abstractmethod
    async def soft_delete_session(self, session_id: str) -> None: pass

    @abstractmethod
    async def add_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        *,
        from_session_id: str | None = None,
        metadata: dict | None = None,
        created_at: datetime | None = None,
    ) -> Message:
        """created_at: place the row at this time instead of now (a summary
        written later for an earlier span); last_message_at only moves forward."""

    @abstractmethod
    async def get_message(self, message_id: str) -> Message:
        """One row by id, from any session, deleted or not; KeyError if unknown."""

    @abstractmethod
    async def get_own_messages(
        self, session_id: str, *, include_deleted: bool = False,
        before: datetime | None = None,
    ) -> List[Message]:
        """The session's own rows ordered by (created_at, id), without borrowed
        history and without checking the session is live; only rows created
        before `before` when given. Soft-deleted rows only with include_deleted."""

    async def get_messages(
        self, session_id: str, *, include_deleted: bool = False,
        include_deleted_session: bool = False,
    ) -> List[Message]:
        """The session's history ordered by (created_at, id): for a branch, the
        rows it borrows from its source (recursively) first, then its own.

        Borrowed rows are the source's rows created before the branch's
        cutoff, as they were at the cutoff (as_of): a row the source deleted
        or compressed afterwards is live here, one deleted before stays
        deleted. They keep their ids and carry metadata.borrowed_from = the
        session that owns them; they are read-only from the branch.
        Soft-deleted rows only with include_deleted; a soft-deleted session
        only with include_deleted_session (e.g. expanding a joined result of
        a branch deleted since), else KeyError like an unknown one.
        """
        session = await self.get_session(session_id, include_deleted=include_deleted_session)
        own = await self.get_own_messages(session_id, include_deleted=include_deleted)
        borrowed = await self._borrowed_messages(session, include_deleted, seen={session_id})
        return borrowed + own

    async def _borrowed_messages(
        self, session: Session, include_deleted: bool, *, seen: set[str],
        cutoff: datetime | None = None,
    ) -> List[Message]:
        source_id = session.branched_from_session_id
        own_cutoff = aware(session.branched_cutoff_at)
        if not source_id or own_cutoff is None or source_id in seen:
            return []
        # Down a chain of branches, each level is cut at the tighter time; its
        # rows are rebuilt as of when the session borrowing them was made.
        cutoff = own_cutoff if cutoff is None else min(cutoff, own_cutoff)
        try:
            source = await self.get_session(source_id, include_deleted=True)
        except KeyError:
            return []
        rows = as_of(await self.get_own_messages(source_id, include_deleted=True, before=cutoff),
                     session.created_at)
        if not include_deleted:
            rows = [m for m in rows if m.deleted_at is None]
        for m in rows:
            m.metadata = {**(m.metadata or {}), BORROWED_FROM: source_id}
        earlier = await self._borrowed_messages(
            source, include_deleted, seen=seen | {source_id}, cutoff=cutoff)
        return earlier + rows

    @abstractmethod
    async def soft_delete_messages_from(self, session_id: str, message_id: str) -> Message: pass

    @abstractmethod
    async def soft_delete_messages(
        self, session_id: str, message_ids: list[str], *, metadata: dict | None = None,
    ) -> int:
        """Soft-delete exactly these live rows of the session; return how many changed.

        metadata is merged into each row's metadata, recording why it was
        deleted: {"compressed_by": <summary id>} for rows a summary replaced,
        {"deleted_by": "user"} for the delete button.
        """

    # ------- Plugin instances/runs/logs -------
    @abstractmethod
    async def list_plugin_instances(self) -> List[PluginInstance]: pass

    @abstractmethod
    async def get_plugin_instance(self, instance_id: str) -> PluginInstance: pass

    @abstractmethod
    async def create_plugin_instance(
        self,
        plugin_id: str,
        display_name: str,
        *,
        config: dict | None = None,
        auto_start: bool = False,
    ) -> PluginInstance: pass

    @abstractmethod
    async def update_plugin_instance(
        self,
        instance_id: str,
        *,
        display_name: str | None = None,
        config: dict | None = None,
        auto_start: bool | None = None,
        status: PluginStatus | None = None,
        current_run_id: str | None = None,
    ) -> PluginInstance: pass

    @abstractmethod
    async def delete_plugin_instance(self, instance_id: str) -> None: pass

    @abstractmethod
    async def create_plugin_run(
        self,
        plugin_instance_id: str,
        plugin_id: str,
        *,
        config_snapshot: dict | None = None,
    ) -> PluginRun: pass

    @abstractmethod
    async def get_plugin_run(self, run_id: str) -> PluginRun: pass

    @abstractmethod
    async def list_plugin_runs(self, plugin_instance_id: str) -> List[PluginRun]: pass

    @abstractmethod
    async def update_plugin_run(
        self,
        run_id: str,
        *,
        status: PluginStatus | None = None,
        running_at: Any = _UNSET,
        exited_at: Any = _UNSET,
        exit_code: Any = _UNSET,
        error: Any = _UNSET,
    ) -> PluginRun: pass

    @abstractmethod
    async def add_plugin_log(
        self,
        plugin_instance_id: str,
        plugin_run_id: str,
        stream: str,
        line: str,
        *,
        level: str | None = None,
        session_id: str | None = None,
    ) -> PluginLog: pass

    @abstractmethod
    async def list_plugin_logs(
        self,
        *,
        plugin_instance_id: str | None = None,
        plugin_run_id: str | None = None,
        session_id: str | None = None,
        limit: int = 500,
    ) -> List[PluginLog]: pass

    # ------- Tasks -------
    @abstractmethod
    async def create_task(
        self,
        task_list_id: str,
        subject: str,
        description: str,
        *,
        active_form: str | None = None,
        metadata: dict | None = None,
    ) -> Task: pass

    @abstractmethod
    async def get_task(self, task_list_id: str, task_id: str) -> Task | None: pass

    @abstractmethod
    async def list_tasks(self, task_list_id: str) -> List[Task]: pass

    @abstractmethod
    async def update_task(
        self,
        task_list_id: str,
        task_id: str,
        *,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        status: TaskStatus | None = None,
        owner: str | None = _UNSET,
        metadata: dict | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
    ) -> Task | None: pass

    @abstractmethod
    async def delete_task(self, task_list_id: str, task_id: str) -> bool: pass

