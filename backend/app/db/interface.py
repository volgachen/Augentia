from abc import ABC, abstractmethod
from typing import Any, List
from app.models.domain import (
    AgentTemplate, Session, SessionStatus, Message, MessageRole,
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
    ) -> Session: pass

    @abstractmethod
    async def get_session(self, session_id: str) -> Session: pass

    @abstractmethod
    async def update_session_system_prompt_snapshot(
        self,
        session_id: str,
        system_prompt_snapshot: str,
    ) -> Session: pass

    @abstractmethod
    async def update_session_title(self, session_id: str, title: str) -> Session: pass

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
    ) -> Message: pass

    @abstractmethod
    async def get_messages(self, session_id: str) -> List[Message]: pass

    @abstractmethod
    async def clone_messages(
        self,
        source_session_id: str,
        target_session_id: str,
        *,
        before_message_id: str | None = None,
    ) -> List[Message]: pass

    @abstractmethod
    async def soft_delete_messages_from(self, session_id: str, message_id: str) -> Message: pass

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

