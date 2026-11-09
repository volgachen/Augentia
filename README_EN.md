<h1 align="center">Augentia</h1>

<p align="center">
  <strong>An Ecosystem of Agents that Augment Human Capability</strong><br />
  <em>AI that helps human value come to life.</em>
</p>

<p align="center">
  <a href="README.md">中文</a> · <a href="README_EN.md">English</a>
</p>

[![Watch the video](https://img.youtube.com/vi/UabvaxV5czM/hqdefault.jpg)](https://youtu.be/UabvaxV5czM)

Augentia is a local-first AI workspace for multi-agent collaboration. It provides a unified agent gateway, a real-time session console, human confirmation gates, cross-session collaboration, and an extensible tool/plugin runtime, allowing people to orchestrate multiple AI sessions at once while staying in control of judgment, switching, confirmation, and synthesis.

## Vision

AI should not simply create more automation, more standardized workflows, and more anxiety. Augentia explores a more human-centered way of working with AI: AI as a companion, an extension of human capability, and a collaborative system, not as a boss or an employee.

The goal of this project is not to make agents do everything on behalf of people. Instead, Augentia aims to help agents support people better: handling complex or tedious work, extending information-processing capacity, creating room for parallel exploration, reducing attention drain, and keeping final judgment, direction, and creativity in human hands.

Therefore, Augentia cares about more than whether a task can be fully automated. It asks:

- Can people orchestrate multiple AI sessions without losing context or control?
- Can multiple agents collaborate around human goals instead of running in isolation?
- Can the system help people amplify their judgment, creativity, organization, and ability to act?

Augentia's long-term direction is to become a **human-centered agentic work ecosystem**.

Before reaching that long-term direction, Augentia will first focus on **building an AI workspace tailored to my own way of working**.

<p align="center">
  <em>AI that helps instead of causing anxiety.</em><br />
  <em>AI as a companion, not a boss or an employee.</em><br />
  <em>AI that helps human value come to life.</em>
</p>

## Current Capabilities

Augentia currently consists of a FastAPI backend and a React frontend console, with a focus on local operation and experimental multi-session collaboration.

Current capabilities include:

- **Agent template management**: Register and manage different types of agents, then enable them across different projects with one click.
- **Multi-session console**: Create and manage multiple agent sessions in the browser, and switch between them with low overhead.
- **Real-time streaming state**: Display agent replies, tool calls, tool results, and status changes. High-risk tool calls require human-in-the-loop confirmation, keeping people in control of critical actions.
- **Task list tools**: Maintain an independent task list for each session, helping agents organize multi-step work.
- **Cross-session collaboration**: Support child sessions and session-to-session messaging for multi-agent parallel workflows.
- **Plugin system**: Run backend-hosted local background plugin processes to extend external integrations.

More capabilities are under active development.

## Latest Updates

- `[Aug 1st, 2026]` Multi-session work recording released: an immersive multi-session collaboration experiment with 6 concurrent tasks, 69 minutes of raw collaboration time, and deep human participation.
- `[Jul 29th, 2026]` Development of this project is now officially handled by Augentia. Thank you, Claude Code!

## Usage

### 1. Prepare a database service

Augentia needs a database to store session records and state. For local development or trial use, you can use the built-in SQLite persistence backend. SQLite works through the Python standard library, so you do not need to install or start MySQL, MariaDB, or any other external database service. Data is stored in a local file.

Use the following in `backend/.env`:

```dotenv
DB_TYPE="sqlite"
SQLITE_PATH=".local/augentia.db"
```

Augentia also supports a dedicated MySQL database service (`DB_TYPE=mysql`). If you already have a MySQL service available, configure the connection in `backend/.env`:

```dotenv
DB_TYPE="mysql"
MYSQL_HOST="127.0.0.1"
MYSQL_PORT="3306"
MYSQL_USER="root"
MYSQL_PASSWORD=""
MYSQL_DATABASE="augentia"
```

The project also keeps an optional local MySQL/MariaDB management script: `python scripts/local_mysql.py start`. It still requires MySQL or MariaDB server binaries to be installed on your machine, so SQLite is recommended for ordinary local trials. For temporary tests, you can also set `DB_TYPE` to `mock`, but this in-memory backend does not persist data.

### 2. Start the backend service

Before starting the backend, configure `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, and other required values in `backend/.env`.

```bash
cd backend
pip install -r requirements.txt
python run.py
# You can also run: uvicorn app.main:app --host 0.0.0.0 --port 12598
# API available at http://<your-ip>:12598
# Docs available at http://<your-ip>:12598/docs
```

### 3. Start the frontend service

The frontend code is located in `frontend/`. After installing Node.js, use the following commands to install dependencies and start the development server.

```bash
cd frontend
npm install
npm run dev
# Dashboard available at http://<your-ip>:12599

# For production use, build first and then serve the static files
npm run build
npx http-server dist -p 12599 --proxy "http://localhost:12599?"
```

`--proxy` sends any path without a matching file back to `index.html`, so `/share/<id>` links and pages like `/sessions/<id>` can be opened or refreshed directly; without it those URLs return 404. If you change the port, change it in `--proxy` too.

## Roadmap

- [ ] Instant messaging integration, such as WeChat message bridging
- [ ] Automatic multi-session management, with appropriate delegation to reduce intervention
- [ ] Browser automation, plus more Skill / Plugin integrations

## Acknowledgements

Many design ideas in this project are inspired by industry products such as Codex and Claude Code. Thanks also to projects such as [wechatbot](https://github.com/corespeed-io/wechatbot) for providing useful capabilities.

You are welcome to use Augentia and join the discussion.
