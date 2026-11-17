# Augentia 插件系统设计（v2）

> 状态：P0–P5 已实现；P6（子进程服务 §6.3、HTTP 路由 §8）尚未实现。第一版（stdio 子进程 + `plugin_instances`）已删除。
> 实现按文末“分步实现”推进；每一步完成后更新本文对应章节的状态。

## 1. 目标与范围

插件是放在 `~/.augentia/plugins/<id>/` 下的一个 Python 包，可以提供以下五种能力的任意组合：

| 能力 | 作用 | 用户在哪里看到 |
|---|---|---|
| 工具 Tools | 给 Agent 增加可调用的工具 | Agent 面板的工具选择框 |
| 会话配置 Session config | 每个会话可以单独开关插件、填写插件参数 | 会话配置中的“插件”面板 |
| 钩子 Hooks | 在收到消息、调用模型、生成回复、调用工具前后介入 Agent 运行 | 无单独界面，效果体现在会话里 |
| 服务 Services | 常驻后台任务：长连接、轮询、定时任务、监听端口 | 插件页面、会话插件面板 |
| HTTP 路由 | 让外部系统调用插件，例如 Webhook | 挂在 `/api/v1/plugins/<id>/http/` 下 |

### 信任模型

插件默认**在后端进程内运行**，由后端直接 import 执行，权限等同于后端本身。这样做是因为 Hook 在每一步模型调用和工具调用时都同步触发，跨进程通信的开销和复杂度都不可接受。

因此**只安装你信任的插件**。需要崩溃隔离或调用阻塞式库的服务，可以选择子进程方式运行（见 6.3）。但 Hook 和工具始终在进程内执行。

### 不在本文范围

- 插件市场、远程安装、签名校验。
- 进程内代码的沙箱隔离。

## 2. 目录与发现

```text
{augentia_home}/                      # 默认 ~/.augentia，环境变量 AUGENTIA_HOME 可改
  plugins/
    wechat/
      plugin.json                     # 清单（manifest）
      plugin.py                       # 入口模块，可以再拆子模块
      README.md
  plugin_settings.json                # 各插件的全局开关和全局配置
  plugin_data/
    wechat/                           # 插件自己的数据目录（ctx.data_dir）
      sessions/<session_id>/          # 会话级数据目录（ctx.session_data_dir）
  sessions/<session_id>/config.json   # 已有的会话配置；新增 plugins 一节
```

- **扫描**：后端启动时扫描 `{augentia_home}/plugins/*/plugin.json`。开发时可以用环境变量 `AUGENTIA_PLUGIN_PATH` 追加更多目录，多个目录用 `os.pathsep` 分隔，按先后顺序扫描。插件 id 重复时，先扫到的生效，后面的标记为错误。
- **代码和数据分开**：重装或升级插件时会覆盖 `plugins/<id>/`，但不会动 `plugin_data/<id>/` 里的登录凭证、缓存等数据。
- **仓库里的示例插件**：`backend/plugins/` 不再自动加载，只作为示例和开发素材，通过 `AUGENTIA_PLUGIN_PATH` 指向它来调试。
- **热加载**：第一阶段不支持热加载代码。`POST /plugins/reload` 会重新扫描目录：加载新出现的插件，重试处于 `error` 状态的插件（比如装好了缺的依赖、改好了清单），卸载目录已经消失的插件。已加载和已禁用的插件保持原样。要让已加载插件的代码改动生效，可以先禁用再启用（会重新 import），或者重启后端。

## 3. 清单 plugin.json

清单只放三类信息：元数据、配置 schema，以及需要事先审查的安全相关声明。工具、Hook、服务的细节都写在代码里，以代码为准，避免清单和代码两头不一致。

```json
{
  "id": "wechat",
  "name": "WeChat Bridge",
  "version": "0.2.0",
  "api_version": 1,
  "description": "把微信消息转发进会话，并把 Agent 的回复发回微信。",
  "author": "augentia",
  "entry": "plugin:WeChatPlugin",
  "python_dependencies": ["wechatbot-sdk"],

  "config": {
    "global": {
      "type": "object",
      "properties": {}
    },
    "session": {
      "type": "object",
      "properties": {
        "command_prefix": { "type": "string", "title": "命令前缀", "default": "\\cmd" },
        "reply_mode": {
          "type": "string", "title": "回复方式",
          "enum": ["final_only", "every_step"], "default": "final_only"
        }
      }
    }
  },

  "session": {
    "default_enabled": false
  },

  "http": {
    "public_routes": ["POST /callback"]
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 必须匹配 `^[a-z][a-z0-9_-]{0,39}$`，且等于目录名。它会成为工具名前缀，所以要短。`catalog`、`instances`、`runs`、`reload`、`stream` 是 `/plugins` 下的固定路径，不能用作 id。清单里出现未知字段会报错，避免拼错的字段被悄悄忽略。 |
| `name` / `version` / `description` / `author` | `name`、`version` 必填 | 用于展示。 |
| `api_version` | 是 | 插件使用的 SDK 版本。宿主只加载自己支持的版本，第一版为 `1`。 |
| `entry` | 是 | 格式为 `模块:类`。模块路径相对于插件目录，用点号分隔。插件目录会作为包 `augentia_plugins.<id>` 导入，所以插件内部可以用相对导入。 |
| `python_dependencies` | 否 | 声明需要的 Python 包。宿主**不会自动安装**，只在加载前检查能否 import；缺包时插件进入 `error` 状态，并列出缺少的包。 |
| `config.global` | 否 | 全局配置的 JSON Schema，存放在 `plugin_settings.json`。 |
| `config.session` | 否 | 会话配置的 JSON Schema。存放在会话 `config.json` 的 `plugins.<id>` 下。写了这一项（或者插件有会话级 Hook 或服务），会话配置里才会出现这个插件。 |
| `session.default_enabled` | 否 | 新会话是否默认启用，默认 `false`。 |
| `http.public_routes` | 否 | 不需要登录就能访问的路由，格式为 `"方法 路径"`。没列出的路由都要求登录。详见第 8 节。 |

### JSON Schema 扩展

前端会按 schema 自动渲染配置表单（见 10.4）。除标准关键字外，还支持以下扩展字段：

- `"x-secret": true`：密钥类字段。API 返回时显示为 `"••••"`；写回时如果仍是 `"••••"`，表示保留原值。第一阶段仍然**明文保存**在本地文件里。
- `"x-widget": "session"`：渲染成会话选择器，值是 session id。
- `"x-widget": "textarea"`：渲染成多行文本框。

## 4. Python SDK

插件只应该从 `app.plugins.sdk` 导入宿主接口。这是宿主承诺保持稳定的唯一接口面，`app` 下的其他模块随时可能变动。

```python
from app.plugins.sdk import (
    Plugin, tool, hook, service, process_service, action,
    Reject, ToolOutput, ToolImage,
    PluginContext, SessionContext, ToolContext,
)

class WeChatPlugin(Plugin):
    async def on_load(self, ctx: PluginContext) -> None: ...
    async def on_unload(self, ctx: PluginContext) -> None: ...
    async def on_config_changed(self, ctx: PluginContext, old: dict, new: dict) -> None: ...
    async def on_session_enabled(self, ctx: SessionContext) -> None: ...
    async def on_session_disabled(self, ctx: SessionContext) -> None: ...
    def routes(self, router) -> None: ...   # 可选，第 8 节
```

- 每个插件在宿主里**只有一个实例**，所有会话共享这一个实例。
- 会话相关的状态不要放在 `self` 上，应该放进 `ctx.state`（见 5.2）。
- 所有回调都是 `async` 函数。阻塞式调用必须用 `await asyncio.to_thread(...)` 包起来，否则会卡住整个后端。
- `on_config_changed` 在全局配置修改后调用。不覆盖它时，宿主会重启该插件的全部全局服务。
- `on_session_enabled` 在插件对某个会话变为生效时调用：会话里打开开关、插件加载（或后端启动）时会话已开启。之后宿主再启动该会话的 `autostart` 会话服务。典型用法是推送初始的 UI 卡片，比如一个 “连接” 按钮。
- `on_session_disabled` 在插件对某个会话不再生效时调用：会话里关掉开关、会话被删除。此时会话服务已经停止，调用之后宿主会清除这个会话的 UI 卡片。插件本身卸载时不调用。

## 5. ctx 接口

宿主调用插件时都会传入一个 `ctx`，有三层，每一层包含上一层的全部内容：

```text
PluginContext            # 全局：on_load、全局服务、HTTP 路由、action
  └ SessionContext       # 会话：Hook、会话服务、会话级 action
      └ ToolContext      # 单次工具调用
```

### 5.1 PluginContext

| 成员 | 类型 | 说明 |
|---|---|---|
| `plugin_id` | `str` | 插件 id |
| `global_config` | `dict` | 全局配置，已经合并了 schema 中的默认值，只读 |
| `data_dir` | `Path` | `plugin_data/<id>/`，首次访问时自动创建 |
| `log` | `PluginLogger` | `debug/info/warning/error(msg, **fields)`，同时写入 `plugin_logs` 表和后端日志 |
| `sessions` | `SessionsAPI` | 见下文 |
| `events` | `EventsAPI` | 见下文 |
| `ui` | `UIAPI` | 见 5.4 |
| `services` | `ServicesAPI` | `get(name, session_id=None)`，返回服务句柄，可查询状态；子进程服务还能通过它发请求（见 6.3） |

`SessionsAPI`：

```python
async def get(session_id: str) -> SessionInfo           # id、title、agent_id、status、working_dir
async def list() -> list[SessionInfo]
async def send(session_id: str, content: str, *,
               images: list[dict] | None = None,
               label: str | None = None) -> None
```

`send` 往会话投递一条用户消息（已实现）。插件写什么，模型就看到什么：与会话间转发不同，内容前面不会加 `[from-session:...]` 前缀，来源只记录在消息的 `from_session_id` 上。

- 消息来源固定标记为 `plugin:<plugin_id>`，**插件不能伪造来源**。可选的 `label`（例如 `"wechat:wx-user-1"`）会写入消息的 `metadata.plugin_label`，用于展示。
- 如果会话的运行器不在运行，宿主会先把它恢复起来（调用 `get_or_rehydrate`），再提交消息。这修复了第一版“会话不在运行时消息只写入数据库、Agent 不会回复”的问题。
- 会话不存在时抛出 `KeyError`。

`EventsAPI`：

```python
def subscribe(event_type: str, handler: Callable[[Event], Awaitable[None]]) -> Callable[[], None]
```

- 返回值是取消订阅的函数。插件卸载时，宿主会自动取消它的所有订阅。
- 事件是**通知型**的：插件不能修改事件，也不会阻塞事件的发布方。每个订阅者有自己的有界队列，最多 1000 条，队列满时丢弃最旧的事件并记一条 warning 日志。
- 第一阶段的事件类型：`message.created`、`session.created`（data 为会话对象）、`session.deleted`（data 为 `{session_id}`）。会话删除时，宿主还会先停止这个会话的所有会话服务、清掉 `ctx.state`。

`Event` 的结构为 `{id, type, created_at, source, data}`，和第一版的 `PluginEvent` 相同。

**事件和 Hook 的区别**：事件覆盖所有会话，适合全局服务做监听。Hook 只在启用了该插件的会话里触发，可以修改正在进行的这一轮对话。

### 5.2 SessionContext

> 已实现：`session_id`、`working_dir`、`config`、`state`、`session_data_dir`、`log`。`session` 快照随 P3 的 `ctx.sessions` 加入，`ui` 在 P4 加入。

| 成员 | 类型 | 说明 |
|---|---|---|
| `session_id` | `str` | |
| `session` | `SessionInfo` | 只读快照 |
| `config` | `dict` | 本插件在这个会话的配置，已经合并了默认值，只读（合并规则见第 9 节） |
| `state` | `dict` | 本插件在这个会话的**内存状态**，由服务、Hook、工具、action 共享。后端重启或插件卸载后丢失；需要持久保存的数据写到 `session_data_dir` |
| `session_data_dir` | `Path` | `plugin_data/<id>/sessions/<session_id>/` |
| `working_dir` | `str \| None` | 会话的工作目录 |
| `log` | `PluginLogger` | 日志自动带上 `session_id`，会话插件面板能看到 |
| `ui` | `UIAPI` | 绑定到当前会话 |

### 5.3 ToolContext

在 `SessionContext` 的基础上增加 `call: ToolCallInfo`，包含 `tool_call_id`、`message_id`（发起调用的那条 assistant 消息）和 `name`。

### 5.4 UI 状态与 action

插件需要和用户交互时（扫码登录、连接或断开、显示当前状态），**不要**输出原始日志让用户自己找，而是推送一个声明式的状态，由前端统一渲染：

```python
await ctx.ui.update({
    "status": "waiting",              # idle | waiting | ok | error
    "message": "请用微信扫码登录",
    "link": "https://...",            # 可选，渲染为链接
    "qr": "https://...",              # 可选，渲染为二维码
    "actions": [                      # 可选，渲染为按钮
        {"id": "disconnect", "label": "断开", "style": "danger"}
    ]
})
```

- `ctx.ui` 来自 `SessionContext` 时，状态显示在该会话的插件面板里；来自 `PluginContext` 时，显示在插件页面上。
- 字段会被校验：`status` 只能是 `idle / waiting / ok / error`，按钮的 `style` 只能是 `default / primary / danger`，未知字段报错。`ctx.ui.clear()` 等于推送一个空的 `idle` 状态（前端隐藏）。
- 插件禁用或卸载时，它的 UI 状态一并清除。
- 每次 `update` 会**整体替换**之前的状态。宿主在内存中保存最新的一份，前端连上时立刻推送。

用户点击按钮后，宿主调用插件里同 id 的 action：

```python
@action("connect")
async def connect(self, ctx: SessionContext, data: dict) -> dict | None: ...
```

- action 的返回值原样返回给前端（`{"result": ...}`；不能 JSON 序列化的值转成 `repr`）。
- 抛出异常时返回 422，前端显示错误信息；超过 30 秒未返回也按出错处理。
- action 接收到的 `ctx` 是什么类型，取决于前端调用时是否带上 `session_id`。

这套机制替代第一版的 `has_session_dialog` 和 `session_dialog.status/input` 命令。

## 6. 服务

### 6.1 声明

```python
@service("bridge", scope="session", restart="on-failure")
async def bridge(self, ctx: SessionContext) -> None:
    async with connect(...) as conn:
        ctx.state["conn"] = conn
        async for msg in conn:
            await ctx.sessions.send(ctx.session_id, msg.text, label=f"wechat:{msg.user_id}")
```

- 服务就是一个长期运行的协程。协程正常返回，表示服务退出。
- 停止服务时，宿主会**取消**这个任务。插件应该在 `finally` 里断开连接、清理资源。取消后宽限 5 秒，超时就放弃等待并记录日志。
- `scope="global"` 时，`ctx` 是 `PluginContext`；`scope="session"` 时，`ctx` 是 `SessionContext`，每个启用了该插件的会话各有一个实例。

| 参数 | 取值 | 默认值 |
|---|---|---|
| `scope` | `global` / `session` | `global` |
| `restart` | `never` / `on-failure` / `always` | `on-failure` |
| `autostart` | `True` / `False`。设为 `False` 时只能手动启动，或由 action 调用 `ctx.services.get(...).start()` 启动 | `True` |

### 6.2 生命周期

**全局服务**
- 插件加载完成且全局启用时启动。
- 插件被禁用、卸载或后端关闭时停止。
- 全局配置变更后，默认会重启（见 `on_config_changed`）。

**会话服务**
- 会话配置中 `plugins.<id>.enabled` 从 false 变为 true 时启动。
- 后端启动时，会扫描 `sessions/*/config.json`，为所有已启用该插件的会话启动服务。
- 以下情况停止：会话配置里关闭插件、会话被删除（`session.deleted` 事件）、插件被禁用、后端关闭。
- **会话服务不跟着会话运行器走。** 换模型、改系统提示词、改工作目录时，运行器会被销毁重建（`registry.remove`），但会话服务不受影响。这样微信登录这类状态不会因为换模型而断掉。

**重启策略**
- 服务异常退出、且策略允许重启时，宿主按 1、2、4、8 秒……最长 60 秒的间隔退避重启。
- 10 分钟内重启超过 5 次，服务停止重试，进入 `errored` 状态，等待用户手动重启。
- 服务连续正常运行 10 分钟后，重启计数清零。

**状态**：`stopped → starting → running → stopping → exited / errored`，等待重启时为 `backoff`（带 `next_restart_at`）。每次启动（包括自动重启）都写一条 `plugin_service_runs` 记录（见 11.3）。

**手动启停**：插件页面和会话面板可以启动、停止、重启任意服务实例。手动启动的会话服务即使插件没在这个会话里启用也会运行；之后只要这个会话的插件开关变化，就按开关重新同步（关闭则停止）。

**服务里调用 `ctx.services.get(自己).stop()` 会报错**：那会等待自己结束。要结束服务，直接从服务函数返回。

### 6.3 子进程服务

阻塞式的库、其他语言写的程序，或者需要崩溃隔离的服务，可以用子进程方式运行：

```python
class MyPlugin(Plugin):
    poller = process_service(
        "poller",
        command=["{python}", "poller.py"],     # {python} 会替换为宿主的解释器路径
        scope="global",
        restart="on-failure",
    )
```

- 子进程的工作目录是插件目录。宿主通过环境变量 `AUGENTIA_PLUGIN_ID`、`AUGENTIA_PLUGIN_DATA_DIR`、`AUGENTIA_PLUGIN_CONFIG`（JSON 格式）传入信息；会话服务还会额外传入 `AUGENTIA_SESSION_ID`。
- 通信沿用第一版的 stdio JSON Lines 协议，并且只认带 `"augentia": 1` 字段的行。这样插件随手打印的 JSON 不会被误当成指令。

  | 方向 | 帧 | 用途 |
  |---|---|---|
  | 子进程 → 宿主 | `{"augentia":1,"type":"session.send","data":{...}}` | 等同于 `ctx.sessions.send` |
  | 子进程 → 宿主 | `{"augentia":1,"type":"ui.update","data":{...}}` | 等同于 `ctx.ui.update` |
  | 子进程 → 宿主 | `{"augentia":1,"type":"log","data":{"level","msg"}}` | 结构化日志 |
  | 子进程 → 宿主 | `{"augentia":1,"type":"response","id":...,"ok":...,"data":...}` | 回复宿主的请求 |
  | 宿主 → 子进程 | `{"augentia":1,"type":"event","event":{...}}` | 只发送进程内部分调用 `subscribe_events([...])` 订阅了的事件 |
  | 宿主 → 子进程 | `{"augentia":1,"type":"request","id":...,"method":...,"data":...}` | 进程内的 Hook、工具、action 通过 `await ctx.services.get("poller").request(method, data)` 调用子进程 |

- 其他 stdout 输出和全部 stderr 都按普通日志处理。
- 停止时会终止**整个进程树**。Windows 上复用 `adapters/node_repl/registry.py` 里已有的进程树清理逻辑。

## 7. 工具与 Hook

### 7.1 工具

```python
@tool(
    "send",
    description="给当前会话绑定的微信联系人发送一条消息。",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    requires_approval=True,
)
async def send(self, ctx: ToolContext, text: str) -> str | ToolOutput:
    ...
```

**命名**
- 工具的对外名称是 `<plugin_id>__<name>`，例如 `wechat__send`。
- 模型接口要求函数名只能包含 `[a-zA-Z0-9_-]`、长度不超过 64，所以用双下划线分隔，不能用点号。
- 名称超长或与其他工具重名时，插件加载失败。

**参数**
- 第一阶段必须显式写出 `parameters` 的 JSON Schema，暂不从类型注解推断。

**参数校验**
- 宿主在调用前按 `parameters` schema 校验模型给的参数，不符合时直接把错误作为工具结果返回给模型，不调用插件代码。

**返回值**
- 与内置工具相同，返回 `str` 或 `ToolOutput`。返回 `None` 视为空字符串，其他类型会转成 `str` 并记一条 warning。
- 抛出的异常不会让这一轮失败，而是以“工具执行失败：异常类型: 信息”的形式作为工具结果交给模型，同时写入后端日志。

**插件中途被禁用**
- 会话启动后插件才被禁用或出错，调用这个工具会得到“不可用”的工具结果。插件重新启用后，正在运行的会话无需重启即可再次使用。

**审批与权限**
- `requires_approval` 是这个工具的默认审批策略。
- Agent 的 `tool_approvals` 和会话的 `tool_permissions` 规则同样适用于插件工具，在规则里写全名即可。

**可用性规则**
- 工具是否可用，只看 **Agent 是否选中了它**，以及**插件是否已加载并全局启用**。会话配置里的 `enabled` 开关不影响工具，只控制 Hook 和会话服务。
- 原因是：在 Agent 面板里选中工具，本身就已经是明确的开启动作。工具在执行时仍然能通过 `ctx.config` 读到会话配置。
- Agent 选中的插件工具不可用时（插件缺失、加载出错或被禁用），会话**照常启动**，只是跳过这个工具。会话启动后的第一轮开头会显示一条状态行说明原因，只显示一次。
- 保存 Agent 时，形如 `<plugin_id>__<tool>` 的工具名即使当前不可用也允许保存；其他未知工具名仍然报错。插件 id 因此不能包含 `__`。

### 7.2 Hook

**触发条件**：插件已加载、已全局启用，并且当前会话配置中 `plugins.<id>.enabled == true`。

```python
@hook("tool.before", priority=100, timeout=10, on_error="skip")
async def guard(self, ctx: SessionContext, call: ToolCall) -> ToolCall | None:
    if call.name == "bash" and "rm -rf" in call.arguments.get("command", ""):
        raise Reject("插件禁止执行 rm -rf")
    return None
```

**通用规则**
- **执行顺序**：同一个 Hook 点上，按 `priority` 从小到大依次执行，`priority` 相同时按插件 id 排序。前一个插件的输出作为后一个的输入。
- **返回值**：返回 `None` 表示不修改；返回同类型的对象表示替换为该对象；抛出 `Reject(reason)` 表示拒绝，具体效果见下表。
- **超时**：`timeout` 默认 10 秒，最长 60 秒。
- **出错处理**：插件抛出 `Reject` 以外的异常，或者超时，按 `on_error` 处理。
  - `skip`（默认）：忽略这个插件，记录日志，并在会话里显示一条警告。
  - `fail`：本轮直接失败。安全类插件应该设为 `fail`，保证出错时是拒绝而不是放行。

**两类 Hook**
- **拦截型**：同步等待，可以修改或拒绝。
- **通知型**：在后台执行，不阻塞对话，返回值会被忽略。

| Hook | 类型 | 参数 | 可修改 | `Reject` 的效果 | 接入位置 |
|---|---|---|---|---|---|
| `message.received` | 拦截 | `IncomingMessage` | `content`、`images` | 本轮不执行，用户消息不入库，会话里显示拒绝原因 | `CAgentAdapter.stream()` 中，`run_turn` 之前 |
| `model.before` | 拦截 | `ModelRequest` | `messages`、`tools`，**只影响这一次请求，不写入历史** | 本轮以失败结束 | `_wrap_model` 中，在 `relocate` / `recording` 之前包一层 |
| `message.generated` | 拦截 | `ModelReply` | `content`；`tool_calls` 只能删除或修改参数，不能新增 | 本轮以失败结束 | `_wrap_model` 中，模型返回之后、CAgent 入库之前 |
| `tool.before` | 拦截 | `ToolCall` | `arguments` | 这次调用不执行，拒绝原因作为工具结果返回给模型 | `gate_tool_call` 开头，在权限判定和人工审批**之前** |
| `tool.after` | 拦截 | `ToolCall`、`ToolResult` | `text`、`images` | 不支持 | `_wrap_tool` 的 handler 中，`execute` 返回之后 |
| `turn.end` | 通知 | `TurnSummary` | 不可修改 | 不支持 | `_run_turn_task` 结尾 |

**数据结构**（均为 dataclass，Hook 拿到的是副本）：

```python
@dataclass
class IncomingMessage:
    content: str
    images: list[dict] | None
    source: str | None            # None 表示用户在前端输入；"session:<id>"；"plugin:<id>"
    turn_overrides: dict | None   # 只读

@dataclass
class ModelRequest:
    step: int                     # 本轮第几次调用模型，从 1 开始
    messages: list[dict]          # CAgent 格式：role/content/tool_calls/tool_call_id
    tools: list[dict]             # {name, description, parameters}

@dataclass
class ModelReply:
    step: int
    content: str
    tool_calls: list[dict]        # {id, name, arguments}
    usage: dict | None
    @property
    def final(self) -> bool: return not self.tool_calls

@dataclass
class ToolCall:
    id: str
    name: str                     # 工具全名
    arguments: dict
    message_id: str | None

@dataclass
class ToolResult:
    text: str
    images: list[ToolImage]

@dataclass
class TurnSummary:
    stop_reason: str              # completed / interrupted / cancelled / failed ...
    final_text: str | None        # 本轮最后一条 assistant 回复的文本
    steps: int
    tool_calls: int
    usage: dict | None
```

**返回值校验**（不符合时按该 Hook 的 `on_error` 处理，默认跳过并在会话里显示警告）：

| Hook | 限制 |
|---|---|
| `message.received` | 只能保留或删除消息自带的图片，不能加新图片（任意引用可能指向任何文件，也绕过了 vision 检查）；改完的消息不能为空 |
| `model.before` | 只能隐藏工具，不能添加（新增的工具没有对应实现） |
| `message.generated` | `tool_calls` 只能删除或改参数，不能新增或改名，id 不能重复 |
| `tool.before` | 只能改 `arguments` |
| `tool.after` | `images` 必须是 `ToolImage` 列表 |

**关键语义**

- **`message.generated` 必须包在模型调用这一层。** CAgent 会先把 assistant 消息写进数据库，再触发 `step_end` 事件，所以挂在 `_on_event` 上只能看到已经入库的结果，没法再改。这个 Hook 对每一次模型调用都会触发；只关心最终回复时，判断 `reply.final`。
- **`tool.before` 改了参数，用户看到的就是改后的参数。** 改写后的参数保存在适配器的 `_arg_overrides[call_id]` 里，后续的权限判定、审批面板的显示、真正执行，用的都是改写后的版本，避免出现“用户批准的是 A，实际执行的是 B”。数据库里 assistant 消息保留模型原始的参数；工具结果的 metadata 中记录 `arguments_modified_by: [plugin_id, ...]`，方便追查。
- **`tool.after` 只处理正常返回的结果。** 工具抛出异常时，不触发这个 Hook，异常照常交给模型。工具已经执行过了，所以这里的 `on_error="fail"` 不会让这一轮失败，而是在结果后面附上 Hook 失败的说明交给模型；`Reject` 在这里不支持，按出错处理。
- **`message.received` 被拒绝时**，这一轮不调用模型、用户消息不入库、不触发 `turn.end`；前端仍会显示用户输入，后面跟一条说明原因的状态行。`source` 为 `None`（用户输入）、`"session:<id>"`（别的会话转来的消息）或 `"plugin:<id>"`；转来的消息内容带有 `[from-session:<id>]` 前缀，Hook 看到的就是模型将看到的内容。
- **`model.before` / `message.generated` 拒绝或 `fail`** 会让这一轮以失败结束（与模型调用出错相同：记录 `turn_end`、显示 ERROR）。被拒绝的回复不入库。
- **`tool.before` 改参数后**，如果新参数和工具的函数签名对不上，这次调用返回参数不匹配的工具错误，并注明是哪个插件改的。
- **`turn.end` 在后台执行**，不阻塞这一轮，也不阻塞下一轮；会话停止时最多等 5 秒让它们结束。轮次被取消时 `stop_reason` 为 `cancelled`。
- **Hook 的警告**以状态行（STATUS）显示在会话里，不入库。
- **插件需要在会话中启用。** 全局禁用的插件、或会话里没有启用的插件，其 Hook 都不会执行。
- **压缩不触发 Hook。** 压缩历史时调用的摘要模型，不触发 `model.before` 和 `message.generated`。
- **重试不重新触发 `message.received`。** `retry` 重跑的是已经入库的用户消息。
- **子会话不继承插件开关。** 子 Agent 会话有自己的会话配置，只遵循 `session.default_enabled`。

## 8. HTTP 路由

```python
def routes(self, router: APIRouter) -> None:
    @router.post("/callback")
    async def callback(request: Request):
        ...   # 校验签名后，调用 self.ctx.sessions.send(...)
```

- 宿主为每个插件创建一个 `APIRouter`，通过统一的转发路由挂在 `/api/v1/plugins/<id>/http/{path:path}` 下。插件被禁用或卸载后，访问返回 404。
- **认证**：默认沿用现有的登录中间件。只有清单中 `http.public_routes` 列出的“方法 + 路径”可以免登录访问，例如 `"POST /callback"` 对应 `POST /api/v1/plugins/wechat/http/callback`。公开路由**必须自行校验**签名或 token。宿主会在插件页面上把公开路由醒目地列出来。
- 插件在 `on_load` 里拿到的 `ctx` 可以保存为 `self.ctx`，供路由处理函数使用。

**自己监听端口**（非 HTTP 协议，例如 WebSocket 服务端、TCP）：
- 在全局服务里自行监听，端口写进 `config.global`。
- 默认只监听 `127.0.0.1`。要监听外网，必须由用户在配置里显式打开。
- 宿主第一阶段不负责端口分配和冲突检测。

## 9. 配置存储与合并

### 全局：`{augentia_home}/plugin_settings.json`

```json
{
  "wechat": { "enabled": true, "config": { } }
}
```

- 新发现的插件默认 `enabled: true`。
- 配置写入前按 `config.global` 校验。

### 会话：`sessions/<id>/config.json` 新增 `plugins` 一节

```json
{
  "version": 1,
  "tool_permissions": { "...": "..." },
  "plugins": {
    "wechat": { "enabled": true, "command_prefix": "\\cmd" }
  }
}
```

插件 X 在会话 S 里生效的配置，按以下优先级合并，后面的覆盖前面的：

1. `config.session` schema 中的 `default` 值；`enabled` 的默认值取清单中的 `session.default_enabled`。
2. 会话配置 `config.json` 中的 `plugins.<id>`。

Agent 模板的 `agent.config.plugins.<id>` 是**新建会话时的初始值**：创建会话时整节复制进会话的 `config.json`，和 `tool_permissions` 的做法一样。之后修改 Agent 的默认值不影响已有会话。

**写入规则**
- `PUT /sessions/{id}/config` 和 `PUT /sessions/{id}/plugins/{plugin_id}` 写入时，用 `jsonschema` 按 schema 校验（已加入 `requirements.txt`），校验失败返回 400，并列出所有错误。`enabled` 必须是布尔值；没有会话 schema 的插件，除 `enabled` 外不接受其他字段。保存 Agent 时，`config.plugins` 按同样规则校验。
- **只校验有变化的条目。** 插件升级后，已存的旧配置可能不再符合新 schema，这时修改工具权限或别的插件不会被它挡住；`GET /sessions/{id}/plugins` 会在 `config_errors` 里列出问题，用户改这一项时才会被校验拦下。
- 没有安装或清单无效的插件，其条目原样保留，不校验也不生效。
- **密钥**：`GET /sessions/{id}/config`、`GET /sessions/{id}/plugins` 和 Agent 接口返回时，`x-secret` 字段显示为 `"••••"`；写回 `"••••"` 表示保留原值。所以工具权限面板整份读出再写回，不会把密钥覆盖成掩码。
- 写入后调用已有的 `runner.reload_config`。插件下一次读 `ctx.config` 就是新值（宿主每次从文件读取，不缓存）。会话服务的启停在 P4 接入。

## 10. 前端改动

### 10.1 Agent 面板（`components/AgentDetailModal.tsx`）

- 工具列表改为分组显示：“内置”一组，每个插件各一组。每个工具显示描述和默认审批策略。
- 不可用的插件工具（插件缺失、出错、被禁用）仍然显示并保持勾选状态，但置灰，并标注原因。
- 新增“插件默认配置”区域（`AgentPluginDefaults`）：每个可配置的插件一个开关加表单，保存到 `agent.config.plugins`，作为新会话的初始值。

### 10.2 会话插件面板（新组件 `components/SessionPluginsPanel.tsx`）

- 放在会话侧栏的 “Plugins” 标签页，与 “Tools”（`ToolPermissionsPanel`）并列。
- 数据来自 `GET /sessions/{id}/plugins`。每个插件一张卡片，包含：
  - 启用开关；
  - 配置表单；
  - UI 状态区（状态文字、链接、二维码、action 按钮）；
  - 会话服务的状态，以及这个会话的插件日志（最近 200 行）。
- 开关和表单保存都调用 `PUT /sessions/{id}/plugins/{plugin_id}`，只改这一个插件，不会覆盖同时在别处编辑的工具权限。
- 用它替换 `PluginSessionConsoleDialog.tsx`，并删除 `SessionDashboard.tsx` 中 `sessionDialogPlugins` 相关的逻辑。

### 10.3 插件页面（`pages/PluginRegistry.tsx` 重写）

- 插件列表：名称、版本、状态（`loaded` / `disabled` / `error`），以及能力摘要（N 个工具、哪些 Hook、哪些服务、公开路由）。加载失败时显示错误信息和缺少的依赖。
- 每个插件：
  - 全局启用开关；
  - 全局配置表单；
  - 服务列表（全局服务实例和各会话的服务实例），带状态、启动、停止、重启按钮，可以查看运行历史和日志（复用 `PluginLogDialog`）；
  - 全局 UI 状态区。
- 顶部提供“重新扫描”按钮，调用 `POST /plugins/reload`。
- 删除“创建实例”的流程，`plugin_instances` 这个概念不再存在。

### 10.4 通用 schema 表单（新组件 `components/SchemaForm.tsx`）

- 支持的类型：`string`、`number` / `integer`、`boolean`、`enum`（渲染为下拉框）、元素为基本类型的 `array`、嵌套一层的 `object`，以及 3 节列出的 `x-*` 扩展。
- 遇到不支持的结构，退回为 JSON 文本编辑框。
- 二维码：前端目前没有二维码库，第一阶段 `qr` 字段先渲染为链接，之后再引入 `qrcode` 包。

### 10.5 数据层

- 重写 `store/plugins.ts`。
- 改为全局一条 WebSocket 连接 `/plugins/stream`，按需带 `?session_id=` 过滤。推送插件状态、服务状态、UI 状态和日志。不再为每个实例单独建连接。
- `api/client.ts` 的 `tools.list` 改用新的返回结构（见第 12 节）。

## 11. 后端改动

### 11.1 模块划分（`app/plugins/`）

| 文件 | 职责 |
|---|---|
| `sdk.py` | 对插件公开的接口：`Plugin`、装饰器、`Reject`、ctx 类型、Hook 数据结构 |
| `manifest.py` | 清单模型和校验（替代 `catalog.py`） |
| `loader.py` | 扫描目录、检查依赖、import 模块、收集装饰器声明的能力 |
| `host.py` | `PluginHost` 单例：保存已加载的插件，负责启用和禁用，对外提供查询接口 |
| `context.py` | ctx 的三层实现，以及 `SessionsAPI`、`UIAPI` |
| `hooks.py` | `HookDispatcher`：按 Hook 点和会话取出启用的处理函数，处理排序、超时和出错策略 |
| `tools.py` | `PluginTool(BaseTool)`：把插件工具包装成内置工具注册进工具注册表 |
| `services.py` | 服务管理：进程内任务、重启策略、会话服务的启停 |
| `process.py` | 子进程服务，由现有的 `runner.py` 改造而来 |
| `events.py` | 保留；`publish` 改为把事件放进各订阅者的队列，不再同步等待订阅者处理 |
| `settings.py` | 读写 `plugin_settings.json`，以及会话插件配置的合并 |
| `http.py` | HTTP 转发路由和公开路由判断 |

需要删除的部分：

- `actions.py`、`registry.py`、`service.py`、`catalog.py`、`log_buffer.py`（`log_buffer.py` 的内存缓冲合并进 `services.py`）。
- `routers/plugins.py` 中实例相关的接口。

### 11.2 现有代码的接入点

| 位置 | 改动 |
|---|---|
| `adapters/tools/registry.py` | `_REGISTRY` 从“名称 → 类”改为“名称 → 工厂函数 + 元数据（来源、描述、默认审批）”。新增 `unregister`。`load_tools` 遇到未知工具时跳过并返回警告列表，不再抛出异常 |
| `adapters/cagent_adapter.py` | 在 `stream()`、`_wrap_model`、`gate_tool_call` 中接入 Hook，新增 `_arg_overrides`；`_run_turn_task` 结尾发出 `turn.end` |
| `adapters/cagent_agent.py` | `_wrap_tool` 的 handler 使用改写后的参数，并接入 `tool.after` |
| `core/session_config.py` | 读取时合并插件默认值，写入时做 schema 校验 |
| `routers/sessions.py` | `PUT /config` 后通知服务管理启停会话服务；新增 `GET /sessions/{id}/plugins`；删除会话时发出 `session.deleted` 事件 |
| `db/*`（mock、sqlite、mysql） | 见下方“数据库”。`message.created` 事件的发布保持不变 |
| `main.py` | 在 `lifespan` 中依次加载插件、启动全局服务、恢复会话服务；关闭时停止全部服务。挂上 HTTP 转发路由，并在认证中间件里接入公开路由判断 |

### 11.3 数据库

- **新表用新名字**，与第一版的 `plugin_instances` / `plugin_runs` / `plugin_logs` 并存，P5 删除旧表时不影响新数据：
  - **`plugin_service_runs`**（一次服务运行）：`id`、`plugin_id`、`service`、`session_id`（可为空）、`status`、`restart_count`、`started_at`、`running_at`、`exited_at`、`exit_code`（只有子进程服务有）、`error`。
  - **`plugin_service_logs`**：`id`、`plugin_id`、`run_id`（可为空，Hook、工具、action 产生的日志没有对应的运行记录）、`session_id`（可为空）、`ts`、`level`、`line`。
- **日志保留**：每个插件保留最近 20000 行；每写入 500 行清理一次更早的。`debug` 级别只进后端日志，不入库。
- **后端重启**：启动时把仍处于 `starting` / `running` / `stopping` 的运行记录标为 `errored`（“backend restarted while the service ran”）。
- **迁移**：第一版的数据只有微信插件的测试数据，P5 直接删除旧表，不做迁移。

## 12. HTTP API

| 方法和路径 | 说明 |
|---|---|
| `GET /plugins` | 插件列表，包含状态、能力摘要和错误信息 |
| `GET /plugins/{id}` | 单个插件详情，包含清单、工具、Hook、服务、公开路由 |
| `POST /plugins/reload` | 重新扫描清单 |
| `PUT /plugins/{id}/enabled` | 请求体 `{enabled}` |
| `GET` / `PUT /plugins/{id}/config` | 全局配置，`x-secret` 字段会被遮蔽 |
| `GET /plugins/{id}/services` | 全部服务实例，包括全局和各会话的 |
| `POST /plugins/{id}/services/{name}/{start\|stop\|restart}` | 会话服务需带 `?session_id=` |
| `GET /plugins/{id}/runs` | 运行历史，支持 `?service=&session_id=` 过滤 |
| `GET /plugins/{id}/logs` | 日志，支持 `?session_id=&run_id=&limit=` |
| `GET /plugins/{id}/ui` | 当前 UI 状态，支持 `?session_id=` |
| `POST /plugins/{id}/actions/{action}` | 请求体 `{session_id?, data}` |
| `WS /plugins/stream` | 状态和日志推送，支持 `?session_id=` |
| `ANY /plugins/{id}/http/{path}` | 插件自己的 HTTP 路由 |
| `GET /sessions/{id}/plugins` | 该会话可配置的插件（有会话 schema、Hook 或会话服务的已加载插件，以及会话里已有条目的插件），包含 schema、合并后的配置（密钥遮蔽）、`config_errors`；UI 状态和服务状态在 P4 加入 |
| `PUT /sessions/{id}/plugins/{plugin_id}` | 请求体 `{enabled?, config?}`，只改这一个插件的条目；`config` 整体替换该插件的配置 |
| `GET /tools` | **返回结构变更**：`[{name, description, source: "builtin" \| "plugin", plugin_id, requires_approval, available, unavailable_reason}]` |

## 13. 示例：微信插件

> 已实现：`backend/plugins/wechat/`（`plugin.py`、`plugin.json`、`README.md`）。下面是最初的草图（0.2 的每会话一个登录）；0.3 已改为：
> - 一个账号级登录：`bridge` 改为全局服务（`autostart=True`，但只有 `state.json` 中 `auto_connect` 为真时才登录，所以连过一次后重启会自动重连），凭据放在 `ctx.data_dir`。
> - 两个会话角色：**收件会话**（微信消息发进去，在会话卡片上设置，持久化）和**镜像会话**（该会话的每条 assistant 消息都转发到微信，默认没有）。镜像通过订阅 `message.created` 事件实现，不用 Hook，所以不依赖会话开关。
> - 不再自动转发回复：去掉 `turn.end` / `message.generated` Hook 和 `reply_mode`，Agent 用 `wechat__send` 回复。
> - 微信命令 `\cmd bind <会话>` 临时把收件和镜像都设为该会话，`\cmd unbind` 恢复原收件会话、取消镜像（只在内存中）。没有收件会话时，在微信里回复提示。
> - `command_prefix`、`message_prefix` 改为全局配置；会话 schema 为空，只用开关决定卡片是否出现。
> - 测试：`scripts/test_plugin_wechat.py`（用假的 `wechatbot` 模拟扫码、收发消息、重启重连）。

```python
# ~/.augentia/plugins/wechat/plugin.py
from app.plugins.sdk import Plugin, SessionContext, action, hook, service, tool


class WeChatPlugin(Plugin):
    @service("bridge", scope="session", autostart=False, restart="on-failure")
    async def bridge(self, ctx: SessionContext) -> None:
        from wechatbot import WeChatBot

        bot = WeChatBot(
            cred_path=str(ctx.session_data_dir / "credentials.json"),
            on_qr_url=lambda url: ctx.ui.update_nowait({
                "status": "waiting", "message": "请用微信扫码登录", "qr": url}),
        )

        @bot.on_message
        async def handle(msg):
            ctx.state["last_user"] = msg.user_id
            await ctx.sessions.send(ctx.session_id, msg.text, label=f"wechat:{msg.user_id}")

        try:
            await bot.login()
            ctx.state["bot"] = bot
            await ctx.ui.update({"status": "ok", "message": "已连接",
                                 "actions": [{"id": "disconnect", "label": "断开"}]})
            await bot.start()
        finally:
            bot.stop()
            ctx.state.pop("bot", None)
            await ctx.ui.update({"status": "idle", "message": "未连接",
                                 "actions": [{"id": "connect", "label": "扫码连接"}]})

    @action("connect")
    async def connect(self, ctx: SessionContext, data: dict):
        await ctx.services.get("bridge", ctx.session_id).start()

    @action("disconnect")
    async def disconnect(self, ctx: SessionContext, data: dict):
        await ctx.services.get("bridge", ctx.session_id).stop()

    @hook("turn.end")
    async def forward_reply(self, ctx: SessionContext, summary) -> None:
        bot, user = ctx.state.get("bot"), ctx.state.get("last_user")
        if bot and user and summary.final_text:
            await bot.send(user, summary.final_text)

    @tool("send", description="给当前会话绑定的微信联系人发送消息。",
          parameters={"type": "object", "properties": {"text": {"type": "string"}},
                      "required": ["text"]})
    async def send(self, ctx, text: str) -> str:
        bot, user = ctx.state.get("bot"), ctx.state.get("last_user")
        if not (bot and user):
            return "微信未连接，或还没有联系人发来过消息"
        await bot.send(user, text)
        return "已发送"
```

和第一版对比，以下内容都不再需要：

- stdin 事件循环；
- `session_dialog` 的命令分派；
- 判断“消息是不是自己发的”的回环检查（`turn.end` 只在本会话触发，拿到的就是 Agent 的回复）；
- 无人使用的 `bindings` 配置。

`ctx.ui.update_nowait` 是给同步回调使用的非 `async` 版本。

## 14. 分步实现

每一步都可以独立合并，并带上对应的测试，测试放在 `backend/scripts/test_plugins_*.py`。

| 步骤 | 内容 | 完成标志 |
|---|---|---|
| **P0 基础** ✅ | `sdk.py` 骨架、`manifest.py`、`loader.py`、`host.py`、`settings.py`；从 `{augentia_home}/plugins` 和 `AUGENTIA_PLUGIN_PATH` 扫描；实现 `GET /plugins`、`/plugins/{id}`、`/enabled`、`/config`、`/reload`（测试：`scripts/test_plugins_host.py`） | 示例插件能被扫描和加载；缺依赖或清单有误时显示 `error` 及原因 |
| **P1 工具** ✅ | 工具注册表改造；`PluginTool`；`GET /tools` 新结构；Agent 面板分组显示；不可用的工具跳过并给出警告（测试：`scripts/test_plugins_tools.py`） | Agent 面板能勾选插件工具，会话里可以调用；卸载插件后会话仍能启动 |
| **P2 会话配置** ✅ | 配置合并与 schema 校验；`GET /sessions/{id}/plugins`；`SchemaForm`；`SessionPluginsPanel` 的开关和表单部分（测试：`scripts/test_plugins_session_config.py`） | 会话里能开关插件、修改参数，校验失败时给出提示 |
| **P3 Hook** ✅ | `hooks.py`；接入 6 个 Hook 点；`_arg_overrides`；超时和出错策略；用注入的假模型（`model_callable`）测试（测试：`scripts/test_plugins_hooks.py`） | 每个 Hook 点都有测试，覆盖修改、拒绝、超时和出错 |
| **P4 服务** ✅ | 进程内服务；全局服务和会话服务的生命周期；重启策略；新的 `plugin_service_runs` / `plugin_service_logs` 表（mock、sqlite、mysql）；UI 状态和 action；`WS /plugins/stream`；重写插件页面；会话面板的 UI 状态区；`events.py` 改为非阻塞（测试：`scripts/test_plugins_services.py`） | 会话服务能随开关启停、换模型时不受影响；服务崩溃后按策略重启 |
| **P5 迁移与清理** ✅ | 按第 13 节改写微信插件；删除第一版的 `plugin_instances`、`actions.py`、`session_dialog` 及相关前端代码（测试：`scripts/test_plugin_wechat.py`） | 微信插件能在新系统里完整跑通；仓库中没有残留的旧接口 |
| **P6 子进程服务与 HTTP** | `process_service`（改造 `runner.py`，Windows 上清理进程树）；HTTP 转发路由和公开路由判断 | 子进程服务能收发协议帧；公开路由免登录可访问，其余路由需要登录 |

## 15. 暂缓与待定

暂缓：

- 代码热加载、插件的安装和卸载 API、依赖自动安装。
- 密钥加密存储（目前 `x-secret` 只是在接口上遮蔽，文件里仍是明文）。
- 事件持久化队列和失败重试（目前事件投递是尽力而为）。
- 检测插件阻塞事件循环的看门狗。
- 端口分配和冲突检测。
- 从函数签名自动推断工具参数的 schema。

待定：

- **插件能否往系统提示词里追加内容？** 合并 Codex Chrome 插件（目前是 `adapters/node_repl` 加上 `seed_browser.py` 里的 SKILL.md 注入）需要这个能力。待定的问题是：追加的内容是否写进 `system_prompt_snapshot`？写进去，系统提示词面板能看到完整内容；不写进去，就和插件开关保持同步。倾向于**不写进快照，在面板里单独展示**。
- **是否需要“会话启用后才能用插件工具”这一选项**，例如清单里加 `tools_require_session_enable`。目前的规则是工具只看 Agent 是否选中。
