# Agent Gateway & Multi-Agent Orchestration System 开发白皮书

## 一、项目愿景与目标

本项目旨在构建一个高扩展性的 **Agent Gateway（智能体网关）**，为多种底层智能体架构提供统一入口，当前以 Tool-use Agent（OpenAI 兼容工具调用循环）为核心。

网关负责智能体模板管理、会话持久化、工具调用、人工确认、流式状态监控，以及前端控制台与底层 Agent Adapter 之间的通信协调。

---

## 二、系统总体架构

系统采用“前端呈现层 - 网关接入层 - 后端逻辑层 - 数据库管理层”的解耦模式，核心层级如下：

| 层级 | 模块名称 | 技术栈选型 | 核心职责 |
| --- | --- | --- | --- |
| 前端呈现层 | Management Dashboard | React 19, Vite, Tailwind CSS | 提供可视化控制台，包括智能体注册、活跃会话监控、实时日志流和插件管理。 |
| 网关接入层 | API & WebSocket Gateway | FastAPI, Uvicorn | 暴露 RESTful API 与 WebSocket 长连接，处理跨域请求，承接前端与后端逻辑层之间的交互。 |
| 后端逻辑层 | Session Manager & Adapter Runtime | 异步 Python, 依赖注入 | 负责会话状态流转、任务列表管理、工具调用确认、底层 Agent Adapter 调度，以及插件子进程管理。 |
| 数据库管理层 | Agent Database | MySQL, In-memory Mock | 通过统一数据库接口保存智能体模板、会话、消息、任务和插件状态。 |

### 项目文件结构总览

```text
backend/app/
├── main.py                 # FastAPI app entry point + lifespan (DB pool)
├── config.py               # Settings from env / .env
├── models/domain.py        # AgentTemplate, Session, Message, Task, Plugin, enums
├── core/runner.py          # SessionRunner — owns an adapter, fans out events
├── core/session_runtime.py # Adapter factory, runner startup, session rehydration
├── core/session_prompt.py  # Effective prompt and runtime-context construction
├── core/workspace.py       # Copy and Git-worktree workspace preparation
├── db/
│   ├── interface.py        # IAgentDatabase abstract interface
│   ├── mysql.py            # MySQL implementation (default)
│   ├── mock.py             # In-memory implementation (fallback/dev)
│   └── deps.py             # FastAPI dependency injection
├── adapters/
│   ├── base.py             # BaseAgentAdapter interface + StreamEvent types
│   ├── openai_tool_use.py  # OpenAI tool-calling loop + confirm gate (legacy engine)
│   ├── cagent_adapter.py   # CAgent-framework engine: event queue + confirm gate
│   ├── cagent_agent.py     # Per-session dynamic BaseAgent subclass (hooks → adapter)
│   ├── cagent_store.py     # CAgent Store protocol over IAgentDatabase (+history repair)
│   ├── registry.py         # session_id → SessionRunner registry
│   └── tools/              # Decorator-registered tools
├── plugins/                # Supervised plugin subprocess runner + log buffer
└── routers/
    ├── agents.py
    ├── sessions.py
    ├── tools.py
    ├── tasks.py
    ├── fs.py
    ├── attachments.py      # Image upload/download (raw bytes, sha256-addressed)
    └── plugins.py

frontend/src/
├── api/                    # Typed fetch + WebSocket client + wire types
├── store/                  # Zustand stores
├── components/             # AgentDetailModal, WorkingDirPicker, TaskListPanel, SubAgentListPanel
└── pages/                  # AgentRegistry, SessionDashboard, LiveConsole, PluginRegistry, PluginConsole
```

---

## 三、数据库服务设计

为了兼顾前期研发效率与后期生产环境的稳定性，数据库层采用 **接口与实现分离** 的设计。业务逻辑依赖 `IAgentDatabase` 抽象接口，具体实现可以是 MySQL，也可以是用于开发和测试的内存 Mock 后端。

### 1. 核心领域模型

- **AgentTemplate（智能体模板）**：定义 Agent 的名称、类型、系统提示词、可用工具、模型配置和附加配置。
- **Session（会话状态）**：管理多轮对话，记录会话所属智能体、工作目录、父会话、附加提示词和状态枚举。
- **Message（交互日志）**：记录系统、用户、工具与 Agent 之间的信息流转。
- **Task（任务项）**：记录单个会话内的任务列表、任务状态、依赖关系、负责人和元数据。
- **Plugin（插件）**：记录插件代码、运行状态、退出码和错误信息。

常见会话状态包括：`INITIALIZING`、`RUNNING`、`WAITING_USER`、`WAITING_CONFIRM`、`COMPLETED`、`ERROR`。

### 2. 数据库接口定义

`IAgentDatabase` 统一封装 Agent、Session、Message、Plugin 和 Task 的读写能力。业务层只依赖接口，不直接绑定具体数据库实现。

核心接口包括：

```python
class IAgentDatabase(ABC):
    async def list_agents(self) -> list[AgentTemplate]: ...
    async def get_agent(self, agent_id: str) -> AgentTemplate: ...
    async def create_agent(self, template: AgentTemplate) -> AgentTemplate: ...
    async def update_agent(self, agent_id: str, **kwargs) -> AgentTemplate: ...
    async def delete_agent(self, agent_id: str) -> None: ...

    async def create_session(self, agent_id: str, working_dir: str | None = None, **kwargs) -> Session: ...
    async def get_session(self, session_id: str) -> Session: ...
    async def update_session_title(self, session_id: str, title: str) -> Session: ...
    async def list_sessions(self) -> list[Session]: ...
    async def update_session_status(self, session_id: str, status: SessionStatus) -> Session: ...

    async def add_message(self, session_id: str, role: MessageRole, content: str, **kwargs) -> Message: ...
    async def get_messages(self, session_id: str) -> list[Message]: ...

    async def list_plugins(self) -> list[Plugin]: ...
    async def get_plugin(self, plugin_id: str) -> Plugin: ...
    async def create_plugin(self, name: str, code: str) -> Plugin: ...
    async def update_plugin(self, plugin_id: str, **kwargs) -> Plugin: ...
    async def delete_plugin(self, plugin_id: str) -> None: ...
    async def set_plugin_status(self, plugin_id: str, status: PluginStatus, **kwargs) -> Plugin: ...

    async def create_task(self, task_list_id: str, subject: str, description: str, **kwargs) -> Task: ...
    async def get_task(self, task_list_id: str, task_id: str) -> Task | None: ...
    async def list_tasks(self, task_list_id: str) -> list[Task]: ...
    async def update_task(self, task_list_id: str, task_id: str, **kwargs) -> Task | None: ...
    async def delete_task(self, task_list_id: str, task_id: str) -> bool: ...
```

---

## 四、后端网关服务规范

后端主要充当“调度中心”和“数据总线”。FastAPI 应用在启动时初始化数据库连接，并挂载 Agent、Session、Tool、Task、Filesystem 和 Plugin 相关路由。

### 1. 核心 API 路由

| Method | Endpoint | Description |
| --- | --- | --- |
| GET / POST | `/api/v1/agents` | List / create agent templates |
| GET / PUT / DELETE | `/api/v1/agents/{agent_id}` | Get, update, or delete an agent template |
| GET | `/api/v1/tools` | List registered tool names |
| POST | `/api/v1/sessions` | Create and start a session |
| GET | `/api/v1/sessions` | List sessions |
| GET | `/api/v1/sessions/{session_id}` | Get session status |
| PATCH | `/api/v1/sessions/{session_id}` | Rename a session |
| GET | `/api/v1/sessions/{session_id}/messages` | Get message history |
| POST | `/api/v1/sessions/{session_id}/messages` | Send a message into a session |
| GET | `/api/v1/sessions/{session_id}/tasks` | Get the session's task list |
| DELETE | `/api/v1/sessions/{session_id}` | Terminate a session |
| WS | `/api/v1/sessions/{session_id}/stream` | Real-time session event stream |
| GET / POST | `/api/v1/plugins` | List / create plugins |
| GET / PUT / DELETE | `/api/v1/plugins/{plugin_id}` | Get, update, or delete a plugin |
| POST | `/api/v1/plugins/{plugin_id}/start` | Start a plugin subprocess |
| POST | `/api/v1/plugins/{plugin_id}/stop` | Stop a plugin subprocess |
| POST | `/api/v1/plugins/{plugin_id}/restart` | Restart a plugin subprocess |
| GET | `/api/v1/plugins/{plugin_id}/logs` | Read buffered plugin logs |
| POST | `/api/v1/plugins/{plugin_id}/logs/clear` | Clear buffered plugin logs |
| WS | `/api/v1/plugins/{plugin_id}/stream` | Stream plugin status and logs |
| GET | `/api/v1/fs/browse` | Browse directories for pickers |
| GET | `/api/v1/fs/templates` | Browse template directories |
| GET | `/api/v1/fs/home` | Get home, project root and templates root |
| POST | `/api/v1/attachments` | Upload an image (raw bytes body); returns `{id, ref, mime, size}` |
| GET | `/api/v1/attachments/{id}` | Download a stored image |

### 2. WebSocket 事件

Session WebSocket 使用 JSON 消息通信。服务端事件通常包含 `type` 和 `data` 字段，常见类型包括：`text`、`tool_call`、`tool_confirm`、`tool_result`、`status`、`error`、`done`、`user`、`session_state`。`user` 事件可额外带 `images` 字段（本轮附带的图片引用）。

客户端发送到 Session WebSocket 的消息分为两类：

```json
{ "content": "user message", "images": [{ "ref": "att:<sha256>", "detail": "low" }] }
```

`images` 可选；有图时 `content` 可为空。

或用于确认工具调用：

```json
{ "decision": "approve", "call_id": "...", "message": "optional supplementary message" }
```

### 3. 图片输入

- 图片字节存放在 `AUGENTIA_HOME/attachments/`，按 sha256 命名（相同图片自动去重）；格式按文件头判定，仅接受 png/jpeg/webp/gif，大小上限默认 10MB（`AUGENTIA_ATTACHMENT_MAX_BYTES`）。
- 消息历史只存引用：user 行的 `metadata.images = [{"ref": "att:<sha256>", "detail"?}]`，文本 `content` 保持纯文本。每次调用模型前，CAgent 引擎通过 `resolve_image` 把引用解析为 data URL（同一轮内缓存）；历史中的图片在后续轮次仍会发送，直到压缩边界之后。
- 只有 `config.engine` 为 `cagent` 且 `config.vision` 为 true 的 agent 接受图片；其余情况 HTTP 返回 400、WS 返回 `error` 帧，且不会开始新的一轮。可选 `config.image_detail`（`auto`/`low`/`high`）作为默认 detail。
- 编辑重发（retry）默认保留原消息的图片；分叉（diverge）复制图片引用。第一版不支持跨会话传图。

#### 工具返回图片

- 工具 `execute` 返回普通 `str`，或在结果带图时返回 `ToolOutput(text, images=[ToolImage(bytes, detail?)])`（`app/adapters/tools/base.py`）。目前 `read` 读到 png/jpeg/webp/gif（按文件头判断）时返回图片。
- CAgent 引擎把图片字节存入附件存储，以 CAgent `ToolResult(images=[...])` 交给框架：tool 行 `metadata.images` 只存引用，`TOOL_RESULT` 事件带 `images` 字段。发给模型时图片在 tool 消息内（`[文本, image_url]`）。
- agent 配置：`vision` 关闭时工具图片被丢弃并在文本中注明；`tool_image_limit`（默认 3，`null` 为全部）只把最近 N 张工具图片发给模型，更早的替换为文字说明（存储不删）；`tool_image_mode` 为 `inline`（默认，图片放在 tool 消息内，Chat Completions 的公开 schema 只允许文本，但很多网关接受）或 `user_message`（图片移到这批工具结果之后的一条 user 消息，适用于严格按 schema 校验的端点）。
- legacy 引擎只保留文本并注明图片被省略。浏览器截图（node_repl 的 `emitImage`）尚未接入。

### 4. Agent 适配器模式

不同类型的 Agent 通过 `BaseAgentAdapter` 接入系统，并由 `SessionRunner` 统一调度。

- **Tool-use Agent**：通过 OpenAI 兼容接口进行工具调用循环，解析模型返回的 `tool_calls`，并在执行需要确认的工具前通过 `tool_confirm` 事件等待人工批准。`config.engine` 选择循环实现：`cagent`（默认，基于 CAgent 框架的 `run_turn`：落库优先的历史、配对工具结果、协作式打断、轮末 turn_end/compression 记账、图片输入；压缩阈值默认关闭，可通过 `config.compression_token_threshold` / `compression_turn_threshold` / `max_steps` 开启）或 `legacy`（适配器内自研循环，不支持图片）。两种引擎读写同一套消息行格式，可在同一部署中共存与切换；`mark` 角色行为框架内部记账，消息接口对客户端过滤。

---

## 五、前端控制台规范

前端提供“驾驶舱”式的管理体验，面向 Agent 模板、会话、实时控制台和插件管理。

### 1. 页面模块规划

- **智能体大厅（Agent Registry）**：卡片式展示系统支持的 Agent，支持配置初始 Prompt、工具和模型信息，并一键启动会话。
- **会话看板（Session Dashboard）**：展示活跃会话及其运行状态，便于进入具体会话控制台。
- **实时控制台（Live Console）**：展示会话流式输出、工具调用、工具确认、任务列表和用户输入区。
- **插件管理（Plugin Registry / Plugin Console）**：管理插件代码、插件运行状态和插件日志。

### 2. 关键技术点

- 使用 WebSocket 长连接接收会话流式事件和插件日志。
- 使用 Zustand 管理前端会话、插件等状态。
- 通过类型化 API 客户端与后端 RESTful API 通信。
- 通过文件系统浏览接口选择工作目录和模板目录。
