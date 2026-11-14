# WeChat Bridge

Connects an Augentia session to a WeChat account through the `wechatbot`
package.

- WeChat messages go into the session as user messages (prefixed with
  `message_prefix`, source `plugin:wechat`, label `wechat:<user id>`).
- The agent's final answer of each turn goes back to whoever wrote last.
  With `reply_mode: every_reply`, text the agent writes before tool calls is
  forwarded too.
- Messages starting with `command_prefix` (default `\cmd`) are bridge
  commands answered in WeChat: `\cmd status`, `\cmd help`.
- Agents that select the `wechat__send` tool can message the contact
  themselves, e.g. for progress updates.

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

1. In a session, open the sidebar's **Plugins** tab and switch WeChat Bridge on.
2. Press **Scan to connect** and scan the QR code link with WeChat.
   Credentials are saved per session in
   `~/.augentia/plugin_data/wechat/sessions/<session id>/wechat_credentials.json`,
   so later connections usually skip the QR code.
3. Send a message from WeChat; the agent's answer comes back to that contact.

**Disconnect** stops the bridge for the session. Switching the plugin off
does the same. Changing the session's model or system prompt does not
disconnect.

## Settings (per session)

| Key | Default | Meaning |
|---|---|---|
| `command_prefix` | `\cmd` | WeChat messages starting with this are bridge commands |
| `message_prefix` | `[send from wechat] ` | Prepended to messages the agent receives |
| `reply_mode` | `final_only` | `every_reply` also forwards text written before tool calls |

## Changes from the v1 `wechat-bridge`

The v1 plugin ran as a separate stdio process managed through "plugin
instances". This version runs in the backend on the v2 plugin API
(`backend/app/plugins/README.md`): the per-session dialog is the session's
plugin card, Connect/Disconnect are actions, and replies are forwarded by a
`turn.end` hook instead of filtering `message.created` events. The unused
`bindings` / `default_session_id` settings are gone.
