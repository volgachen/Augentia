import uuid
from datetime import datetime, timezone
from typing import Dict, List
from app.core.compressed import plain_copy_fields
from app.db.interface import IAgentDatabase, _UNSET
from app.db.seed_agents import seed_agent_templates
from app.models.domain import (
    AgentTemplate, Session, SessionStatus,
    Message, MessageRole,
    PluginInstance, PluginLog, PluginRun, PluginStatus,
    Task, TaskStatus,
)
from app.plugins.events import PluginEvent, get_plugin_event_bus



class MockMemoryDatabase(IAgentDatabase):
    def __init__(self) -> None:
        self._agents: Dict[str, AgentTemplate] = {a.id: a for a in seed_agent_templates()}
        self._sessions: Dict[str, Session] = {}
        self._messages: Dict[str, List[Message]] = {}
        self._plugin_instances: Dict[str, PluginInstance] = {}
        self._plugin_runs: Dict[str, PluginRun] = {}
        self._plugin_logs: Dict[int, PluginLog] = {}
        self._plugin_log_counter = 0
        # tasks keyed by task_list_id -> task_id -> Task
        self._tasks: Dict[str, Dict[str, Task]] = {}
        # monotonic per-list id counter; survives deletes (ids never reused)
        self._task_counters: Dict[str, int] = {}

    async def list_agents(self) -> List[AgentTemplate]:
        return list(self._agents.values())

    async def get_agent(self, agent_id: str) -> AgentTemplate:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise KeyError(f"Agent '{agent_id}' not found")
        return agent

    async def create_agent(self, template: AgentTemplate) -> AgentTemplate:
        self._agents[template.id] = template
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
            agent.system_prompt = system_prompt
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
            system_prompt_snapshot=system_prompt_snapshot,
        )
        if not session.title:
            session.title = f"{agent.name} · {session.created_at:%H:%M}"
        self._sessions[session.id] = session
        self._messages[session.id] = []
        return session

    async def get_session(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None or session.deleted_at is not None:
            raise KeyError(f"Session '{session_id}' not found")
        return session

    async def update_session_system_prompt_snapshot(
        self,
        session_id: str,
        system_prompt_snapshot: str,
    ) -> Session:
        session = await self.get_session(session_id)
        session.system_prompt_snapshot = system_prompt_snapshot
        return session

    async def update_session_title(self, session_id: str, title: str) -> Session:
        session = await self.get_session(session_id)
        session.title = title
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
    ) -> Message:
        session = await self.get_session(session_id)  # validate session exists
        message = Message(
            session_id=session_id,
            role=role,
            content=content,
            from_session_id=from_session_id,
            metadata=dict(metadata or {}),
        )
        self._messages[session_id].append(message)
        session.last_message_at = message.created_at
        await get_plugin_event_bus().publish(PluginEvent(
            type="message.created",
            source=from_session_id or "augentia",
            data=message.model_dump(mode="json"),
        ))
        return message

    async def get_messages(self, session_id: str, *, include_deleted: bool = False) -> List[Message]:
        await self.get_session(session_id)
        return [m for m in self._messages[session_id] if include_deleted or m.deleted_at is None]

    async def clone_messages(
        self,
        source_session_id: str,
        target_session_id: str,
        *,
        before_message_id: str | None = None,
    ) -> List[Message]:
        await self.get_session(source_session_id)
        await self.get_session(target_session_id)
        source = sorted(
            (m for m in self._messages[source_session_id] if m.deleted_at is None),
            key=lambda message: (message.created_at, message.id),
        )
        if before_message_id is not None:
            index = next((i for i, m in enumerate(source) if m.id == before_message_id), None)
            if index is None:
                raise KeyError(f"Message '{before_message_id}' not found")
            source = source[:index]
        cloned = []
        for original in source:
            role, content, metadata = plain_copy_fields(original)
            message = Message(
                session_id=target_session_id,
                role=role,
                content=content,
                from_session_id=original.from_session_id,
                metadata=metadata,
            )
            message.created_at = original.created_at
            self._messages[target_session_id].append(message)
            cloned.append(message)
        if cloned:
            self._sessions[target_session_id].last_message_at = cloned[-1].created_at
        return cloned

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
        self._sessions[session_id].last_message_at = visible[-1].created_at if visible else None
        return target

    async def soft_delete_messages(
        self, session_id: str, message_ids: list[str], *, compressed_by: str | None = None,
    ) -> int:
        await self.get_session(session_id)
        wanted = set(message_ids)
        now = datetime.now(timezone.utc)
        changed = 0
        for message in self._messages[session_id]:
            if message.deleted_at is None and message.id in wanted:
                message.deleted_at = now
                if compressed_by is not None:
                    message.metadata = {**message.metadata, "compressed_by": compressed_by}
                changed += 1
        visible = [m for m in self._messages[session_id] if m.deleted_at is None]
        self._sessions[session_id].last_message_at = visible[-1].created_at if visible else None
        return changed

    # ------- Plugin instances/runs/logs -------

    async def list_plugin_instances(self) -> List[PluginInstance]:
        return sorted(self._plugin_instances.values(), key=lambda p: p.created_at)

    async def get_plugin_instance(self, instance_id: str) -> PluginInstance:
        instance = self._plugin_instances.get(instance_id)
        if instance is None:
            raise KeyError(f"Plugin instance '{instance_id}' not found")
        return instance

    async def create_plugin_instance(
        self,
        plugin_id: str,
        display_name: str,
        *,
        config: dict | None = None,
        auto_start: bool = False,
    ) -> PluginInstance:
        instance = PluginInstance(
            plugin_id=plugin_id,
            display_name=display_name,
            config=config,
            auto_start=auto_start,
        )
        self._plugin_instances[instance.id] = instance
        return instance

    async def update_plugin_instance(
        self,
        instance_id: str,
        *,
        display_name: str | None = None,
        config: dict | None = None,
        auto_start: bool | None = None,
        status: PluginStatus | None = None,
        current_run_id: str | None = None,
    ) -> PluginInstance:
        instance = await self.get_plugin_instance(instance_id)
        if display_name is not None:
            instance.display_name = display_name
        if config is not None:
            instance.config = config
        if auto_start is not None:
            instance.auto_start = auto_start
        if status is not None:
            instance.status = status
        if current_run_id is not None:
            instance.current_run_id = current_run_id
        instance.updated_at = datetime.now(timezone.utc)
        return instance

    async def delete_plugin_instance(self, instance_id: str) -> None:
        await self.get_plugin_instance(instance_id)
        del self._plugin_instances[instance_id]
        run_ids = [r.id for r in self._plugin_runs.values() if r.plugin_instance_id == instance_id]
        for run_id in run_ids:
            del self._plugin_runs[run_id]
        for log_id, log in list(self._plugin_logs.items()):
            if log.plugin_instance_id == instance_id:
                del self._plugin_logs[log_id]

    async def create_plugin_run(
        self,
        plugin_instance_id: str,
        plugin_id: str,
        *,
        config_snapshot: dict | None = None,
    ) -> PluginRun:
        await self.get_plugin_instance(plugin_instance_id)
        now = datetime.now(timezone.utc)
        run = PluginRun(
            plugin_instance_id=plugin_instance_id,
            plugin_id=plugin_id,
            config_snapshot=config_snapshot,
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        self._plugin_runs[run.id] = run
        return run

    async def get_plugin_run(self, run_id: str) -> PluginRun:
        run = self._plugin_runs.get(run_id)
        if run is None:
            raise KeyError(f"Plugin run '{run_id}' not found")
        return run

    async def list_plugin_runs(self, plugin_instance_id: str) -> List[PluginRun]:
        await self.get_plugin_instance(plugin_instance_id)
        runs = [r for r in self._plugin_runs.values() if r.plugin_instance_id == plugin_instance_id]
        return sorted(runs, key=lambda r: r.created_at, reverse=True)

    async def update_plugin_run(
        self,
        run_id: str,
        *,
        status: PluginStatus | None = None,
        running_at = _UNSET,
        exited_at = _UNSET,
        exit_code = _UNSET,
        error = _UNSET,
    ) -> PluginRun:
        run = await self.get_plugin_run(run_id)
        if status is not None:
            run.status = status
        if running_at is not _UNSET:
            run.running_at = running_at
        if exited_at is not _UNSET:
            run.exited_at = exited_at
        if exit_code is not _UNSET:
            run.exit_code = exit_code
        if error is not _UNSET:
            run.error = error
        run.updated_at = datetime.now(timezone.utc)
        return run

    async def add_plugin_log(
        self,
        plugin_instance_id: str,
        plugin_run_id: str,
        stream: str,
        line: str,
        *,
        level: str | None = None,
        session_id: str | None = None,
    ) -> PluginLog:
        await self.get_plugin_instance(plugin_instance_id)
        await self.get_plugin_run(plugin_run_id)
        self._plugin_log_counter += 1
        log = PluginLog(
            id=self._plugin_log_counter,
            plugin_instance_id=plugin_instance_id,
            plugin_run_id=plugin_run_id,
            session_id=session_id,
            stream=stream,
            level=level,
            line=line,
        )
        self._plugin_logs[log.id] = log
        return log

    async def list_plugin_logs(
        self,
        *,
        plugin_instance_id: str | None = None,
        plugin_run_id: str | None = None,
        session_id: str | None = None,
        limit: int = 500,
    ) -> List[PluginLog]:
        logs = list(self._plugin_logs.values())
        if plugin_instance_id is not None:
            logs = [l for l in logs if l.plugin_instance_id == plugin_instance_id]
        if plugin_run_id is not None:
            logs = [l for l in logs if l.plugin_run_id == plugin_run_id]
        if session_id is not None:
            logs = [l for l in logs if l.session_id == session_id]
        logs.sort(key=lambda l: (l.ts, l.id or 0))
        return logs[-limit:]

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
