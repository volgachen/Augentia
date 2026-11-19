import uuid
from datetime import datetime, timezone
from typing import Dict, List
from app.db.interface import IAgentDatabase, _UNSET
from app.db.seed_agents import seed_agent_templates
from app.models.domain import (
    AgentTemplate, Session, SessionStatus, SystemPrompt,
    Message, MessageRole,
    PluginServiceLog, PluginServiceRun,
    Task, TaskStatus,
)
from app.plugins.events import PluginEvent, get_plugin_event_bus



class MockMemoryDatabase(IAgentDatabase):
    def __init__(self) -> None:
        # Immutable prompt versions; agents/sessions hold the current id and
        # (for convenience) its text, kept in step by _point_at.
        self._prompts: Dict[str, SystemPrompt] = {}
        self._agents: Dict[str, AgentTemplate] = {}
        for agent in seed_agent_templates():
            self._agents[agent.id] = self._point_at(agent, agent.system_prompt, new_chain=True)
        self._sessions: Dict[str, Session] = {}
        self._messages: Dict[str, List[Message]] = {}
        self._service_runs: Dict[str, PluginServiceRun] = {}
        self._service_logs: List[PluginServiceLog] = []
        self._service_log_counter = 0
        # tasks keyed by task_list_id -> task_id -> Task
        self._tasks: Dict[str, Dict[str, Task]] = {}
        # monotonic per-list id counter; survives deletes (ids never reused)
        self._task_counters: Dict[str, int] = {}

    def _point_at(self, owner, content: str, *, new_chain: bool = False):
        """Give an agent/session a new current version (chained to its
        current one unless new_chain); unchanged text keeps the version."""
        current = None if new_chain else getattr(owner, "system_prompt_id", None)
        text_attr = "system_prompt" if isinstance(owner, AgentTemplate) else "system_prompt_snapshot"
        if current and self._prompts[current].content == content:
            return owner
        version = SystemPrompt(content=content, previous_id=current)
        self._prompts[version.id] = version
        owner.system_prompt_id = version.id
        setattr(owner, text_attr, content)
        return owner

    async def get_system_prompt(self, prompt_id: str) -> SystemPrompt:
        version = self._prompts.get(prompt_id)
        if version is None:
            raise KeyError(f"System prompt '{prompt_id}' not found")
        return version

    async def list_agents(self) -> List[AgentTemplate]:
        return list(self._agents.values())

    async def get_agent(self, agent_id: str) -> AgentTemplate:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise KeyError(f"Agent '{agent_id}' not found")
        return agent

    async def create_agent(self, template: AgentTemplate) -> AgentTemplate:
        self._agents[template.id] = self._point_at(template, template.system_prompt, new_chain=True)
        return template

    async def update_agent(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        system_prompt: str | None = None,
        tool_names: list[str] | None = None,
        config: dict | None = None,
        openai_model: str | None = None,
        openai_base_url: str | None = None,
    ) -> AgentTemplate:
        agent = await self.get_agent(agent_id)
        if name is not None:
            agent.name = name
        if description is not None:
            agent.description = description
        if system_prompt is not None:
            self._point_at(agent, system_prompt)
        if tool_names is not None:
            agent.tool_names = tool_names
        if config is not None:
            agent.config = config
        if openai_model is not None:
            agent.openai_model = openai_model
        if openai_base_url is not None:
            agent.openai_base_url = openai_base_url
        return agent

    async def delete_agent(self, agent_id: str) -> None:
        await self.get_agent(agent_id)
        del self._agents[agent_id]

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
        agent = await self.get_agent(agent_id)  # validate agent exists
        session = Session(
            id=session_id or str(uuid.uuid4()),
            agent_id=agent_id,
            title=title,
            working_dir=working_dir,
            parent_session_id=parent_session_id,
            additional_prompt=additional_prompt,
            additional_prompt_path=additional_prompt_path,
            model=model,
            model_params=dict(model_params) if model_params is not None else None,
            branched_from_session_id=branched_from_session_id,
            branched_from_message_id=branched_from_message_id,
            branched_cutoff_at=branched_cutoff_at,
            join_into_session_id=join_into_session_id,
        )
        if system_prompt_snapshot is not None:
            self._point_at(session, system_prompt_snapshot, new_chain=True)
        elif system_prompt_id is not None:
            shared = await self.get_system_prompt(system_prompt_id)
            session.system_prompt_id = shared.id
            session.system_prompt_snapshot = shared.content
        if not session.title:
            session.title = f"{agent.name} · {session.created_at:%H:%M}"
        self._sessions[session.id] = session
        self._messages[session.id] = []
        return session

    async def get_session(self, session_id: str, *, include_deleted: bool = False) -> Session:
        session = self._sessions.get(session_id)
        if session is None or (session.deleted_at is not None and not include_deleted):
            raise KeyError(f"Session '{session_id}' not found")
        return session

    async def update_session_system_prompt_snapshot(
        self,
        session_id: str,
        system_prompt_snapshot: str,
    ) -> Session:
        session = await self.get_session(session_id)
        self._point_at(session, system_prompt_snapshot)
        session.updated_at = datetime.now(timezone.utc)
        return session

    async def update_session_title(self, session_id: str, title: str) -> Session:
        session = await self.get_session(session_id)
        session.title = title
        session.updated_at = datetime.now(timezone.utc)
        return session

    async def update_session_working_dir(self, session_id: str, working_dir: str | None) -> Session:
        session = await self.get_session(session_id)
        session.working_dir = working_dir
        session.updated_at = datetime.now(timezone.utc)
        return session

    async def update_session_model(
        self, session_id: str, model: str | None, model_params: dict | None,
    ) -> Session:
        session = await self.get_session(session_id)
        session.model = model
        session.model_params = dict(model_params) if model_params is not None else None
        session.updated_at = datetime.now(timezone.utc)
        return session

    async def update_session_dispatch_mode(self, session_id: str, enabled: bool) -> Session:
        session = await self.get_session(session_id)
        session.dispatch_mode = enabled
        session.updated_at = datetime.now(timezone.utc)
        return session

    async def pending_joins(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self._sessions.values():
            if s.join_into_session_id and not s.joined_message_id and s.deleted_at is None:
                counts[s.join_into_session_id] = counts.get(s.join_into_session_id, 0) + 1
        return counts

    async def set_joined_message(self, session_id: str, message_id: str,
                                 outcome: str | None = None) -> Session:
        session = await self.get_session(session_id, include_deleted=True)
        session.joined_message_id = message_id
        if outcome is not None:
            session.join_outcome = outcome
        return session

    async def failed_joins(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self._sessions.values():
            if s.join_into_session_id and s.join_outcome == "failed" and s.deleted_at is None:
                counts[s.join_into_session_id] = counts.get(s.join_into_session_id, 0) + 1
        return counts

    async def ack_failed_joins(self, session_id: str) -> int:
        n = 0
        for s in self._sessions.values():
            if s.join_into_session_id == session_id and s.join_outcome == "failed":
                s.join_outcome = "failed_acked"
                n += 1
        return n

    async def update_session_parent(self, session_id: str, parent_session_id: str | None) -> Session:
        session = await self.get_session(session_id)
        session.parent_session_id = parent_session_id
        session.updated_at = datetime.now(timezone.utc)
        return session

    async def list_sessions(self) -> List[Session]:
        return [s for s in self._sessions.values() if s.deleted_at is None]

    async def update_session_status(self, session_id: str, status: SessionStatus) -> Session:
        session = await self.get_session(session_id)
        session.status = status
        session.updated_at = datetime.now(timezone.utc)
        return session

    async def soft_delete_session(self, session_id: str) -> None:
        session = await self.get_session(session_id)
        now = datetime.now(timezone.utc)
        session.deleted_at = now
        session.updated_at = now
        for message in self._messages.get(session_id, []):
            if message.deleted_at is None:
                message.deleted_at = now

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
        session = await self.get_session(session_id)  # validate session exists
        message = Message(
            session_id=session_id,
            role=role,
            content=content,
            from_session_id=from_session_id,
            metadata=dict(metadata or {}),
        )
        if created_at is not None:
            message.created_at = created_at
        self._messages[session_id].append(message)
        if session.last_message_at is None or message.created_at > session.last_message_at:
            session.last_message_at = message.created_at
        await get_plugin_event_bus().publish(PluginEvent(
            type="message.created",
            source=from_session_id or "augentia",
            data=message.model_dump(mode="json"),
        ))
        return message

    async def get_message(self, message_id: str) -> Message:
        for rows in self._messages.values():
            for m in rows:
                if m.id == message_id:
                    return m.model_copy(deep=True)
        raise KeyError(f"Message '{message_id}' not found")

    async def get_own_messages(
        self, session_id: str, *, include_deleted: bool = False,
        before: datetime | None = None,
    ) -> List[Message]:
        rows = sorted(self._messages.get(session_id, []), key=lambda m: (m.created_at, m.id))
        # Copies: callers (borrowed history) adjust the rows they get.
        return [m.model_copy(deep=True) for m in rows
                if (include_deleted or m.deleted_at is None)
                and (before is None or m.created_at < before)]

    async def soft_delete_messages_from(self, session_id: str, message_id: str) -> Message:
        await self.get_session(session_id)
        messages = self._messages[session_id]
        target = next((m for m in messages if m.id == message_id and m.deleted_at is None), None)
        if target is None:
            raise KeyError(f"Message '{message_id}' not found")
        now = datetime.now(timezone.utc)
        for message in messages:
            if message.deleted_at is None and message.created_at >= target.created_at:
                message.deleted_at = now
        visible = [m for m in messages if m.deleted_at is None]
        self._sessions[session_id].last_message_at = (
            max(m.created_at for m in visible) if visible else None)
        return target

    async def soft_delete_messages(
        self, session_id: str, message_ids: list[str], *, metadata: dict | None = None,
    ) -> int:
        await self.get_session(session_id)
        wanted = set(message_ids)
        now = datetime.now(timezone.utc)
        changed = 0
        for message in self._messages[session_id]:
            if message.deleted_at is None and message.id in wanted:
                message.deleted_at = now
                if metadata:
                    message.metadata = {**message.metadata, **metadata}
                changed += 1
        visible = [m for m in self._messages[session_id] if m.deleted_at is None]
        self._sessions[session_id].last_message_at = (
            max(m.created_at for m in visible) if visible else None)
        return changed

    # ------- v2 plugin service runs / logs -------

    async def create_service_run(self, plugin_id, service, *, session_id=None, restart_count=0):
        run = PluginServiceRun(plugin_id=plugin_id, service=service, session_id=session_id,
                               restart_count=restart_count)
        self._service_runs[run.id] = run
        return run.model_copy()

    async def update_service_run(self, run_id, *, status=None, running_at=_UNSET, exited_at=_UNSET,
                                 exit_code=_UNSET, error=_UNSET):
        run = self._service_runs.get(run_id)
        if run is None:
            raise KeyError(f"Service run '{run_id}' not found")
        if status is not None:
            run.status = status
        for key, value in (("running_at", running_at), ("exited_at", exited_at),
                           ("exit_code", exit_code), ("error", error)):
            if value is not _UNSET:
                setattr(run, key, value)
        return run.model_copy()

    async def list_service_runs(self, plugin_id, *, service=None, session_id=None, limit=100):
        runs = [r for r in self._service_runs.values() if r.plugin_id == plugin_id
                and (service is None or r.service == service)
                and (session_id is None or r.session_id == session_id)]
        runs.sort(key=lambda r: r.started_at, reverse=True)
        return [r.model_copy() for r in runs[:limit]]

    async def interrupt_active_service_runs(self, error):
        count = 0
        for run in self._service_runs.values():
            if run.status in ("starting", "running", "stopping"):
                run.status, run.error, run.exited_at = "errored", error, datetime.now(timezone.utc)
                count += 1
        return count

    async def add_service_log(self, plugin_id, line, *, level="info", run_id=None, session_id=None):
        self._service_log_counter += 1
        log = PluginServiceLog(id=self._service_log_counter, plugin_id=plugin_id, run_id=run_id,
                               session_id=session_id, level=level, line=line)
        self._service_logs.append(log)
        return log

    async def list_service_logs(self, plugin_id, *, run_id=None, session_id=None, limit=500):
        logs = [l for l in self._service_logs if l.plugin_id == plugin_id
                and (run_id is None or l.run_id == run_id)
                and (session_id is None or l.session_id == session_id)]
        return logs[-limit:]

    async def prune_service_logs(self, plugin_id, keep):
        mine = [l.id for l in self._service_logs if l.plugin_id == plugin_id]
        drop = set(mine[:-keep] if keep > 0 else mine)
        self._service_logs = [l for l in self._service_logs if l.id not in drop]
        return len(drop)

    # ------- Tasks -------

    async def create_task(
        self,
        task_list_id: str,
        subject: str,
        description: str,
        *,
        active_form: str | None = None,
        metadata: dict | None = None,
    ) -> Task:
        next_id = self._task_counters.get(task_list_id, 0) + 1
        self._task_counters[task_list_id] = next_id
        task = Task(
            id=str(next_id),
            task_list_id=task_list_id,
            subject=subject,
            description=description,
            active_form=active_form,
            metadata=metadata,
        )
        self._tasks.setdefault(task_list_id, {})[task.id] = task
        return task

    async def get_task(self, task_list_id: str, task_id: str) -> Task | None:
        return self._tasks.get(task_list_id, {}).get(task_id)

    async def list_tasks(self, task_list_id: str) -> List[Task]:
        tasks = self._tasks.get(task_list_id, {})
        return sorted(tasks.values(), key=lambda t: int(t.id))

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
    ) -> Task | None:
        task = self._tasks.get(task_list_id, {}).get(task_id)
        if task is None:
            return None

        if subject is not None:
            task.subject = subject
        if description is not None:
            task.description = description
        if active_form is not None:
            task.active_form = active_form
        if status is not None:
            task.status = status
        if owner is not _UNSET:
            task.owner = owner
        if metadata is not None:
            merged = dict(task.metadata or {})
            for k, v in metadata.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            task.metadata = merged or None

        # Reciprocal dependency wiring: A blocks B  <=>  B blockedBy A.
        for other_id in add_blocks or []:
            other = self._tasks.get(task_list_id, {}).get(other_id)
            if other is None:
                continue
            if other_id not in task.blocks:
                task.blocks.append(other_id)
            if task_id not in other.blocked_by:
                other.blocked_by.append(task_id)
        for other_id in add_blocked_by or []:
            other = self._tasks.get(task_list_id, {}).get(other_id)
            if other is None:
                continue
            if other_id not in task.blocked_by:
                task.blocked_by.append(other_id)
            if task_id not in other.blocks:
                other.blocks.append(task_id)

        task.updated_at = datetime.now(timezone.utc)
        return task

    async def delete_task(self, task_list_id: str, task_id: str) -> bool:
        tasks = self._tasks.get(task_list_id, {})
        if task_id not in tasks:
            return False
        del tasks[task_id]
        # Cascade: strip dangling references from every other task.
        for other in tasks.values():
            if task_id in other.blocks:
                other.blocks.remove(task_id)
            if task_id in other.blocked_by:
                other.blocked_by.remove(task_id)
        return True
