# WeChat Bridge

Connects WeChat accounts to Augentia through the `wechatbot-sdk` package
(imported as `wechatbot`). Several accounts can be logged in at once; each
one talks to its own session.

## How text moves

Per account:

- **Inbound session** (one, persisted): the account's WeChat messages are
  sent into it as user messages, prefixed with `message_prefix` (source
  `plugin:wechat`, label `wechat:<user id>`). Set it from a session's plugin
  card. An account has at most one inbound session and a session receives
  from at most one account.
- **Replies**: the agent answers with the `wechat__send` tool, which sends
  through the account the session belongs to (the `\cmd bind` target first,
  else the saved inbound session). A session that belongs to no account gets
  an error from the tool. Ordinary replies are *not* forwarded; the default
  message prefix tells the agent so. Messages go to the account's contact who
  wrote last. WeChat only lets the bot write to someone who has messaged it
  since it connected, so after a reconnect or backend restart, send any
  message from WeChat first.
- **Mirror session** (none by default): every assistant message written
  there, including text before tool calls and turns started from the web
  console, is forwarded to the account's WeChat. Only `\cmd bind` sets it.

If an account has no inbound session, the bridge answers in WeChat saying
so. Deleting a session, or switching the plugin off in it, clears it as an
inbound session.

## WeChat commands

Messages starting with `command_prefix` (default `\cmd`) are handled by the
bridge and never reach an agent. They act on the account they were sent to.

| Command | Effect |
|---|---|
| `\cmd bind <session>` | Temporarily make that session both inbound and mirror for this account. `<session>` is an id, an id prefix or an exact title (case-insensitive). Without an argument it lists recent sessions and which account each already uses. A session another account receives can't be bound. |
| `\cmd unbind` | Back to the account's saved inbound session and no mirror. |
| `\cmd status` | The account's name and where its messages go. |
| `\cmd help` | The command list. |

Bindings are kept in memory only: a backend restart also unbinds.

## Install

```bash
pip install wechatbot-sdk      # into the backend's Python environment (imported as `wechatbot`)
mkdir -p ~/.augentia/plugins
cp -r backend/plugins/wechat ~/.augentia/plugins/
```

Or, for development, point the backend at this repo copy:
`AUGENTIA_PLUGIN_PATH=./plugins` in `backend/.env`.

Then open the Plugins page (or press Rescan) and check that **WeChat Bridge**
is `loaded`. Without `wechatbot-sdk` installed it shows `error` with the
missing package.

## Use

1. In a session, open the sidebar's **Plugins** tab and switch WeChat Bridge
   on. Give the session's agent the `wechat__send` tool.
2. Type a name (optional) next to **Add WeChat account**, press it and scan
   the QR code with that WeChat. Added from a session card that has no
   account yet, the new account is bound to that session.

The cards show one row per account and the connection state only when it
needs you (QR code, connecting, error):

- **Plugins page**: name, the bound session (with **Unbind**), **Remove**.
- **Session card, bound**: just its account, with **Unbind**.
- **Session card, not bound**: every account with **Bind**, or a greyed
  "Bound to …" when the account already has a session.

Every added account stays connected and reconnects by itself (also when the
backend starts). There is no disconnect: **Remove** deletes the account and
its saved login. Scanning a WeChat that is already added drops the new entry.

## Files

In `~/.augentia/plugin_data/wechat/`:

- `accounts.json`: per account (keyed by a local id) its `alias`,
  `wechat_id`, `inbound` (bound) session and last `contact`.
- `accounts/<id>/credentials.json`: the WeChat login, so reconnects skip
  the QR code.

## Settings (global, Plugins page)

| Key | Default | Meaning |
|---|---|---|
| `command_prefix` | `\cmd` | WeChat messages starting with this are bridge commands |
| `message_prefix` | `[from WeChat; reply with the wechat__send tool] ` | Prepended to messages the agent receives |

The plugin has no per-session settings; its session switch decides whether
the card (and the inbound role) is offered in that session.

## Changes

- **0.4**: several accounts, compact one-row-per-account cards (name set when
  adding; no connect / disconnect / rename). The 0.3 `state.json` + `wechat_credentials.json`
  are migrated into the first account (`state.json` is renamed
  `state.json.migrated`). Uses the host's card sections, action `data` and
  text inputs.
- **0.3**: one account-level login instead of one per session; replies
  through `wechat__send` only; inbound / mirror roles with `\cmd bind` /
  `\cmd unbind`.
