# WeChat Bridge

Connects one WeChat account to Augentia through the `wechatbot` package.
Only you use the account, so there is no contact allowlist.

## How text moves

- **Inbound session** (one, persisted): WeChat messages are sent into it as
  user messages, prefixed with `message_prefix` (source `plugin:wechat`,
  label `wechat:<user id>`). Set it from a session's plugin card.
- **Replies**: the agent answers with the `wechat__send` tool. Ordinary
  replies are *not* forwarded; the default message prefix tells the agent so.
  Messages go to the WeChat contact who wrote last.
- **Mirror session** (none by default): every assistant message written
  there, including text before tool calls and turns started from the web
  console, is forwarded to WeChat. Only `\cmd bind` sets it.

If no session receives WeChat messages, the bridge answers in WeChat saying
so. Deleting the inbound session, or switching the plugin off in it, clears
the role.

## WeChat commands

Messages starting with `command_prefix` (default `\cmd`) are handled by the
bridge and never reach an agent.

| Command | Effect |
|---|---|
| `\cmd bind <session>` | Temporarily make that session both inbound and mirror. `<session>` is an id, an id prefix or an exact title (case-insensitive). Without an argument it lists recent sessions. |
| `\cmd unbind` | Back to the saved inbound session and no mirror. |
| `\cmd status` | Connection state and where messages go. |
| `\cmd help` | The command list. |

The binding is kept in memory only: a backend restart also unbinds.

## Install

```bash
pip install wechatbot          # into the backend's Python environment
mkdir -p ~/.augentia/plugins
cp -r backend/plugins/wechat ~/.augentia/plugins/
```

Or, for development, point the backend at this repo copy:
`AUGENTIA_PLUGIN_PATH=./plugins` in `backend/.env`.

Then open the Plugins page (or press Rescan) and check that **WeChat Bridge**
is `loaded`. Without `wechatbot` installed it shows `error` with the missing
package.

## Use

1. In a session, open the sidebar's **Plugins** tab and switch WeChat Bridge
   on. Give the session's agent the `wechat__send` tool.
2. Press **Scan to connect** and scan the QR code with WeChat. Connecting
   from a session card makes that session the inbound one if none is set.
3. Other sessions with the plugin switched on show **Receive WeChat here**
   to move the inbound role to them.

The connection belongs to the account, not to a session: Connect and
Disconnect work from any session card or the Plugins page. After a
successful connect the bridge reconnects by itself when the backend starts,
until you press Disconnect.

## Files

In `~/.augentia/plugin_data/wechat/`:

- `wechat_credentials.json`: the WeChat login, so reconnects skip the QR code.
- `state.json`: `auto_connect`, the `inbound` session and the last `contact`.

## Settings (global, Plugins page)

| Key | Default | Meaning |
|---|---|---|
| `command_prefix` | `\cmd` | WeChat messages starting with this are bridge commands |
| `message_prefix` | `[from WeChat; reply with the wechat__send tool] ` | Prepended to messages the agent receives |

The plugin has no per-session settings; its session switch decides whether
the card (and the inbound role) is offered in that session.

## Changes from 0.2

0.2 ran one WeChat login per session and forwarded every turn's final answer
(`reply_mode`). 0.3 has one account-level login (a global service, credentials
moved from `sessions/<id>/` to the plugin data directory, so scan once again),
replies through `wechat__send` only, and adds the inbound / mirror roles with
`\cmd bind` / `\cmd unbind`.
