# Backend test scripts

Standalone scripts for verifying backend behavior. Run from `backend/`:

```bash
python scripts/<name>.py
```

Each exits 0 on success, nonzero on failure — so they compose in CI or a shell loop.

## Needs `uvicorn app.main:app` running

| Script | What it checks | External deps |
|---|---|---|
| `test_rest_api.py` | REST round-trip: health, list agents, create/get/delete session, messages | backend only |
| `test_ws_tool_use.py` | Full WS round-trip with tool-use adapter; asserts `TOOL_CALL` + `TEXT` events | backend + `OPENAI_*` in `.env` |

## No backend needed

| Script | What it checks | External deps |
|---|---|---|
| `test_tools_direct.py` | Each registered tool's `.execute()` returns non-empty output | network (arxiv, duckduckgo) |
| `test_tool_bash.py` | `bash` tool branches: normal, timeout, truncation, background | none |
| `test_tool_read.py` | `read` tool branches: full, offset, limit, truncation, missing file | none |
| `test_tool_edit.py` | `write` + `edit` branches: create/overwrite, unique/replace_all/rejections | none |
| `test_adapter_tool_use.py` | `OpenAIToolUseAdapter` lifecycle in isolation, with tool calls | `OPENAI_*` in `.env` |
| `test_spawn_parent.py` | `parent_session_id`: persistence, source working dir, unknown parent → 404 | none (TestClient + mock DB) |
| `test_tool_subagent.py` | `subagent` tool: git worktree creation + non-git fallback, child spawn through the router | `git` in PATH (TestClient + mock DB) |

## Env overrides

- `AUGENTIA_BASE_URL` — override `http://localhost:12598` for the WS/REST scripts.
- `AUGENTIA_AGENT_ID` — override the seed agent used by `test_rest_api.py`.
