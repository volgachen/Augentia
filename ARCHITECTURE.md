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
| 数据库管理层 | Agent Database | MySQL, In-memory Mock | 通过统一数据库接口保存智能体模板、会话、消息、任务，以及插件服务的运行记录和日志。 |

### 项目文件结构总览

```text
backend/app/
├── main.py                 # FastAPI app entry point + lifespan (DB pool)
├── config.py               # Settings from env / .env
├── models/domain.py        # AgentTemplate, Session, Message, Task, PluginServiceRun/Log, enums
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
│   ├── cagent_adapter.py   # CAgent-framework engine: event queue + confirm gate
│   ├── cagent_agent.py     # Per-session dynamic BaseAgent subclass (hooks → adapter)
│   ├── cagent_store.py     # CAgent Store protocol over IAgentDatabase (+history repair)
│   ├── plugin_hooks.py     # Plugin hook payload conversion + return-value checks
│   ├── registry.py         # session_id → SessionRunner registry
│   └── tools/              # Decorator-registered tools
├── plugins/                # Plugin host: loader, SDK, tools, hooks, services (see plugins/README.md)
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
└── pages/                  # AgentRegistry, SessionDashboard, LiveConsole, Plugins
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

常见会话状态包括：`INITIALIZING`、`RUNNING`、`WAITING_USER`、`WAITING_CONFIRM`、`COMPRESSING`、`COMPLETED`、`ERROR`。`COMPRESSING` 表示正在等摘要模型（轮次压缩、按阈值的整段压缩或手动压缩），和 `RUNNING` 一样算忙碌、重启后重置；进入和离开时会推送 `compressing` / `compressing_done` 两个 STATUS 事件。另有只在接口返回里出现、不存库的 `DISPATCHING`（派发模式下空闲）。

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

    # Plugin definitions and settings live in files under ~/.augentia; the
    # database only keeps service runs and plugin log lines.
    async def create_service_run(self, plugin_id: str, service: str, **kwargs) -> PluginServiceRun: ...
    async def update_service_run(self, run_id: str, **kwargs) -> PluginServiceRun: ...
    async def list_service_runs(self, plugin_id: str, **filters) -> list[PluginServiceRun]: ...
    async def add_service_log(self, plugin_id: str, line: str, **kwargs) -> PluginServiceLog: ...
    async def list_service_logs(self, plugin_id: str, **filters) -> list[PluginServiceLog]: ...

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
| GET | `/api/v1/tools` | List selectable tools (built-in and plugin) with description, source and default approval |
| POST | `/api/v1/sessions` | Create and start a session |
| GET | `/api/v1/sessions` | List sessions |
| GET | `/api/v1/sessions/{session_id}` | Get session status |
| PATCH | `/api/v1/sessions/{session_id}` | Rename a session |
| GET | `/api/v1/sessions/{session_id}/locate/{message_id}` | Where a message sits in the transcript (`{expand, role, deleted}`: summaries to open, outermost first), for the console's `?m=` links. Also finds a row of a fork-dispatch branch shown inside a joined result (looked up by id, so only that branch is read); the share page's `/share/{id}/locate/{message_id}` uses the same lookup |
| GET / PUT | `/api/v1/sessions/{session_id}/model` | Get / set the session's own `model`, `model_params` and `api_format` (null: the agent's `openai_model` / `config.model_params` / `openai_api_format`; params replace the agent's as a whole). PUT: 409 while a turn runs; the live runner is rebuilt, history kept. Branches (diverge, fork-dispatch) start with the source's values |
| POST | `/api/v1/sessions/{session_id}/fork-dispatch` | `{messages: [...], titles?, turn_overrides?}`: run the messages concurrently, one branch each (borrowing this session's history). Each finished turn is appended back to this session, in completion order and between its turns, as a `branch_summary` row (the branch's turn summary, else its last reply; worded by agent `config.branch_join_template` with `{task}` / `{result}`) plus a `turn_end` mark. Expanding the row (`.../messages/{id}/compressed`) shows the branch's turn. Branches share the working directory. `pending_joins` on the session says how many results are still to come; outside dispatch mode new messages, retry and another batch are refused (409) until they are in (compression is allowed). Deleting a pending branch joins what it finished. 409 while the session runs a turn |
| POST | `/api/v1/sessions/{session_id}/compress` | Manual compression. Body (all optional): `since` / `hours` compress only the turns from then on into one `range_summary` (earlier history kept); `until` (with `since`) stops at the last turn that ended before it, keeps later turns, and places the summary row right after the turns it covers (for summarizing a past day once the next has started); `instruction` replaces the summarizer instruction. 409 while the session runs a turn |
| GET | `/api/v1/sessions/{session_id}/failed-joins` | Branches dispatched from this session whose turn failed and nobody acknowledged: `[{session_id, message_id, task, error: {type, message, step}, joined_at}]`. A failed branch counts as joined (it no longer holds `pending_joins`) but is kept out of this session's context: its `branch_summary` row is soft-deleted (`metadata.dropped = "failed"`) and gets no `turn_end`. The session reports how many in `failed_joins` |
| POST | `/api/v1/sessions/{session_id}/failed-joins/ack` | Acknowledge them: their `join_outcome` becomes `failed_acked` and `failed_joins` goes back to 0. Returns the session |
| PUT | `/api/v1/sessions/{session_id}/dispatching` | `{enabled}`: dispatch mode. On (needs an idle session): every `POST /messages` runs in a new branch of the session (returns `{status: "dispatched", branch_session_id}`) and its result is joined back as above; messages run concurrently, each borrowing the history as of its arrival (results already joined included). The session runs no turns of its own: typing into it (WS) and retry are 409; compression (`POST /compress`) works and summarizes the joined results like any turns (branches already running keep their history). It reports status `DISPATCHING` while idle; stored as `sessions.dispatch_mode`, so turns and compression never clear it. Off: ordinary turns again once `pending_joins` is 0 |
| DELETE | `/api/v1/sessions/{session_id}/parent` | Unlink the session from its parent (it becomes top-level) |
| POST | `/api/v1/sessions/{session_id}/messages/{message_id}/diverge` | Branch at a message: a new session whose history is the source's before it (through it for a summary), borrowed rather than copied: the branch records `branched_from_session_id` / `branched_from_message_id` / `branched_cutoff_at` and reads the source's rows as they were when it was made (`metadata.borrowed_from` on each). Borrowed rows are read-only in the branch (delete / retry -> 400; whole-history compression skipped, range compression covers only its own turns). Not a sub-agent: `parent_session_id` stays empty |
| GET / PUT | `/api/v1/sessions/{session_id}/system-prompt` | Get / replace the session's effective system prompt (PUT: 409 while a turn runs) |
| GET | `/api/v1/sessions/{session_id}/messages` | Get message history |
| POST | `/api/v1/sessions/{session_id}/messages` | Send a message into a session. With `reply: false` the message is only added to the history as context (a user row, written between turns, no `turn_end`, no model call; returns `{status: "added", message_id}`): the next turn's model sees it right before that turn's input, and turn compression keeps it as written (also in dispatch mode, where it goes into the session itself). Optional `turn_overrides` sets `max_steps`, `step_reminder_at`, `step_reminder`, `tool_limit_at`, `tool_limit_message`, `turn_compression_tool_call_threshold`, `turn_compression_instruction`, `turn_compression_marker` for this turn only (`null` turns `step_reminder_at` / `tool_limit_at` / `turn_compression_tool_call_threshold` off; `turn_compression_marker` `null` or `""` means no marker message, and without a custom instruction the default instruction follows the turn's marker); recorded on the user row as `metadata.turn_overrides` |
| POST | `/api/v1/sessions/{session_id}/messages/{message_id}/retry` | Rewrite from a user message: delete it and everything after, then send the edited text; accepts `turn_overrides` too (not inherited from the edited message) |
| GET | `/api/v1/sessions/{session_id}/tasks` | Get the session's task list |
| DELETE | `/api/v1/sessions/{session_id}` | Terminate a session |
| WS | `/api/v1/sessions/{session_id}/stream` | Real-time session event stream |
| GET / PUT | `/api/v1/sessions/{session_id}/config` | Session config file (tool permissions, `plugins` section; plugin secrets masked) |
| GET | `/api/v1/sessions/{session_id}/plugins` | Plugins configurable for the session, with effective config, UI card and service state |
| PUT | `/api/v1/sessions/{session_id}/plugins/{plugin_id}` | Switch a plugin on/off for the session or change its session config |
| GET | `/api/v1/plugins` | Installed plugins (state, capabilities, services, UI card) — full plugin API in `backend/app/plugins/README.md` §12 |
| WS | `/api/v1/plugins/stream` | Live plugin, service, UI and log updates |
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
- 只有 `config.vision` 为 true 的 agent 接受图片；其余情况 HTTP 返回 400、WS 返回 `error` 帧，且不会开始新的一轮。可选 `config.image_detail`（`auto`/`low`/`high`）作为默认 detail。
- 编辑重发（retry）默认保留原消息的图片；分叉（diverge）复制图片引用。第一版不支持跨会话传图。

#### 工具返回图片

- 工具 `execute` 返回普通 `str`，或在结果带图时返回 `ToolOutput(text, images=[ToolImage(bytes, detail?)])`（`app/adapters/tools/base.py`）。目前 `read` 读到 png/jpeg/webp/gif（按文件头判断）时返回图片。
- 图文交错：`ToolOutput.interleaved(["第 1 页：", ToolImage(...), "第 2 页：", ToolImage(...)])`，每张图记录 `at`（在 `text` 中的字符偏移），经 CAgent 交错 `ToolResult` 交给框架，模型看到的顺序与列表一致；`metadata.images` 与 `TOOL_RESULT` 事件里的引用带 `at`，前端据此把缩略图插在结果文本对应位置。vision 关闭或存储拒收时，该图原位置留下 `[image omitted]`。`tool_image_mode=user_message` 下图片集中移到后续 user 消息，交错顺序不保留。
- CAgent 引擎把图片字节存入附件存储，以 CAgent `ToolResult(images=[...])` 交给框架：tool 行 `metadata.images` 只存引用，`TOOL_RESULT` 事件带 `images` 字段。发给模型时图片在 tool 消息内（`[文本, image_url]`）。
- agent 配置：`vision` 关闭时工具图片被丢弃并在文本中注明；`tool_image_limit`（默认 3，`null` 为全部）只把最近 N 张工具图片发给模型，更早的替换为文字说明（存储不删）；`tool_image_mode` 为 `inline`（默认，图片放在 tool 消息内，Chat Completions 的公开 schema 只允许文本，但很多网关接受；Responses 格式下图片放在 `function_call_output.output` 列表里，这是该接口原生支持的写法）或 `user_message`（图片移到这批工具结果之后的一条 user 消息，适用于严格按 schema 校验的端点）。
- 浏览器截图（node_repl 的 `emitImage`）尚未接入。

### 4. Agent 适配器模式

不同类型的 Agent 通过 `BaseAgentAdapter` 接入系统，并由 `SessionRunner` 统一调度。

- **Tool-use Agent**：通过 OpenAI 兼容接口进行工具调用循环，解析模型返回的 `tool_calls`，并在执行需要确认的工具前通过 `tool_confirm` 事件等待人工批准。循环基于 CAgent 框架的 `run_turn`（落库优先的历史、配对工具结果、协作式打断、轮末 turn_end/compression 记账、图片输入；压缩阈值默认关闭，可通过 `config.compression_token_threshold` / `compression_turn_threshold` / `max_steps` 开启；`config.turn_compression_tool_call_threshold` 开启单轮压缩：一轮工具调用次数达到该值时，本轮消息在模型上下文中被一条 turn_summary 摘要替代；`config.max_steps` 限制一轮调用模型的次数，`config.step_reminder_at` 设置后，从第 N 次调用模型起，每一步最后一个工具结果末尾附上 `config.step_reminder`（默认“你已经工作足够久了，该休息了。”；可写 `{remain_steps_to_limit}`、`{remain_steps_to_max}`、`{step}` 占位符，分别为到 `tool_limit_at` / `max_steps` 前剩余的步数和已调用模型的次数），提示模型在被截停前收尾；`config.tool_limit_at` 设置后，从第 N 次调用模型起仍调用模型，但工具调用一律不执行（也不进入人工确认），每个都返回 `config.tool_limit_message`，模型只能回复收尾，`max_steps` 仍是硬上限）。原先的 `legacy` 引擎（适配器内自研循环）已移除：`config.engine` 只能省略或写 `cagent`，保存 agent 时其他值返回 400；早已存在的配置里若残留其他值，启动会话时忽略并按 cagent 运行。legacy 引擎写下的历史（拆开的 tool_call/tool 行、纯文本 agent 行）仍可读取，CAgent store 读取时会修复配对。`mark` 角色行为框架内部记账，消息接口对客户端过滤。
- **接口格式（api_format）**：模型端点支持两种格式，`chat`（`/chat/completions`，默认）和 `responses`（`/responses`），用哪种取决于网关为该模型挂了什么后端。agent 的 `openai_api_format` 是默认值，会话的 `api_format`（`PUT /sessions/{id}/model`，与 model 一起设置，null 跟随 agent）可覆盖，分支继承；`config.compression_api_format` 可单独指定摘要模型的格式（不设跟随会话）。转换只在 `cagent_adapter._make_responses_model` 一处：system prompt 进 `instructions`，图片为 `input_image`，工具调用 / 结果为 `function_call` / `function_call_output`，工具定义为扁平格式（`strict: false`），usage 的 `input_tokens` / `output_tokens` 另记为 `prompt_tokens` / `completion_tokens`。`model_params` 原样发送、不改名（Responses 下请写 `max_output_tokens`、`reasoning: {effort}` 等）；`input`、`instructions` 为保留键。推理内容不跨步骤保留（不请求 `encrypted_content`）。
- **Runtime Context 开关**：`config.append_runtime_context`（默认 `true`）决定开 session 时是否在 system prompt 末尾拼接 runtime 信息（操作系统、工作目录、session 开始时间、Windows 下 bash 可执行文件等）。设为 `false` 时最终 prompt 只包含 agent 基础 prompt 与 session 附加 prompt，适用于需要干净固定 prompt 的场景。该开关在 session 启动快照（`system_prompt_snapshot`）生成时生效，已有会话不受影响。

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
