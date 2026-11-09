<h1 align="center">Augentia</h1>

<p align="center">
  <strong>An Ecosystem of Agents that Augment Human Capability</strong><br />
  <em>让 AI 成就人的价值。</em>
</p>

<p align="center">
  <a href="README.md">中文</a> · <a href="README_EN.md">English</a>
</p>

Augentia 是一个面向多智能体协作的本地优先 AI 工作台。它提供统一的 Agent 网关、实时会话控制台、人工确认机制、跨会话协作能力和可扩展的工具/插件运行环境，让人可以同时调度多个 AI 会话，并在判断、切换、确认和整合时保持主导。

[![查看视频](https://img.youtube.com/vi/UabvaxV5czM/hqdefault.jpg)](https://youtu.be/UabvaxV5czM)

## 项目愿景

AI 不应该只是制造更多自动化、更多标准化流程和更多焦虑。Augentia 希望探索一种更适合人的 AI 工作方式：AI 不是老板，也不是员工，而是人的同行者、能力外延和协作系统。

这个项目的核心目标不是让 Agent 替人完成一切，而是让 Agent 更好地支撑人：承接繁杂任务，扩展信息处理能力，提供并行探索空间，减少对注意力的消耗，同时把最终的判断权、方向感和创造性留给人。

因此，Augentia 关注的不只是“能不能自动完成任务”，而是：

- 人能否同时驾驭多个 AI 会话，而不失去上下文和掌控感；
- 多个 Agent 能否围绕人的目标协作，而不是各自孤立运行；
- 系统能否帮助人放大判断、创造、组织和行动的能力。

Augentia 的长期方向，是成为一个**以人为中心的 Agent 工作生态**。

在实现这个长期方向之前，Augentia 会先专注于**打造一个为我自己量身定做的 AI 工作台**。

<p align="center">
  <em>让 AI 带来助力而非焦虑。</em><br />
  <em>让 AI 成为同行者而非老板或员工。</em><br />
  <em>让 AI 成就人的价值。</em>
</p>

## 当前能力

Augentia 目前包含一个 FastAPI 后端和一个 React 前端控制台，重点支持本地运行和实验性多会话协作。

主要能力包括：

- **Agent 模板管理**：注册并管理不同类型的 Agent，在不同项目中一键启用。
- **多会话控制台**：在浏览器中创建、管理多个 Agent 会话，并在会话间低成本快速切换。
- **实时流式状态**：展示 Agent 回复、工具调用、工具结果和状态变化。对高风险工具调用要求 Human-in-the-loop 确认，让人保留关键操作的决策权。
- **任务列表工具**：为每个会话维护独立任务清单，帮助 Agent 组织多步骤工作。
- **跨会话协作**：支持子会话和会话间消息传递，用于构建多 Agent 并行工作流。
- **插件系统**：支持由后端托管的本地后台插件进程，用于扩展外部集成能力。

更多能力仍在持续开发中。

## 最新动态

- `[Aug 1st, 2026]` 多会话工作视频实录发布：一次沉浸式多会话协作实验，6 个并行任务，69 分钟原始协作记录，人工深度参与。
- `[Jul 29th, 2026]` 本项目的开发工作今日起正式由 Augentia 接手。Thank you, Claude Code!

## 使用方法

### 1. 准备数据库服务

Augentia 需要数据库来保存会话记录和状态信息。在本地开发或试用时，可以使用项目内置的 SQLite 持久化后端。SQLite 通过 Python 标准库工作，不需要安装和启动 MySQL、MariaDB 等额外数据库服务，数据会保存到本地文件。

在 `backend/.env` 中使用：

```dotenv
DB_TYPE="sqlite"
SQLITE_PATH=".local/augentia.db"
```

本项目同样支持使用专门的 MySQL 数据库服务存储这些信息（`DB_TYPE=mysql`）。如果你已经有可用的 MySQL 服务，也可以继续在 `backend/.env` 中配置 MySQL 连接信息：

```dotenv
DB_TYPE="mysql"
MYSQL_HOST="127.0.0.1"
MYSQL_PORT="3306"
MYSQL_USER="root"
MYSQL_PASSWORD=""
MYSQL_DATABASE="augentia"
```

项目也保留了一个可选的本机 MySQL/MariaDB 管理脚本：`python scripts/local_mysql.py start`。不过它仍然要求电脑上已经安装 MySQL 或 MariaDB 服务端程序，所以普通本地试用优先使用 SQLite 即可。临时测试还可以把 `DB_TYPE` 改成 `mock`，但这种内存数据库不会持久保存数据。

### 2. 启动后端服务

启动前，请先在 `backend/.env` 中配置 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 和 `OPENAI_MODEL` 等必要信息。

```bash
cd backend
pip install -r requirements.txt
python run.py
# 也可以运行：uvicorn app.main:app --host 0.0.0.0 --port 12598
# API available at http://<your-ip>:12598
# Docs available at http://<your-ip>:12598/docs
```

### 3. 启动前端服务

前端代码位于 `frontend/` 目录下。安装 Node.js 后，可以按下面的命令安装依赖并启动开发服务。

```bash
cd frontend
npm install
npm run dev
# Dashboard available at http://<your-ip>:12599

# 如需要正式使用，请先编译再部署静态文件
npm run build
npx http-server dist -p 12599 --proxy "http://localhost:12599?"
```

`--proxy` 让找不到对应文件的路径都回退到 `index.html`，`/share/<id>` 分享链接和 `/sessions/<id>` 等页面才能直接打开或刷新；不加的话这些地址会返回 404。端口改了的话，`--proxy` 里的端口也要一起改。

## 后续开发计划

- [ ] 即时通讯接入，例如微信消息桥接
- [ ] 多会话自动管理，适当放权减少干预
- [ ] 浏览器自动控制，以及更多 Skill / Plugin 接入

## 致谢

此项目的许多设计理念借鉴了 Codex、Claude Code 等业内知名产品，同时也感谢 [wechatbot](https://github.com/corespeed-io/wechatbot) 等项目提供的能力支持。

欢迎使用，并一起交流讨论。
