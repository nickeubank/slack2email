# slack2email

Forwards every Slack message you can see — DMs, group DMs, and every channel
you're a member of — to your email, as readable HTML with links back to Slack.

Messages are grouped per conversation into a digest (default: every 60 seconds),
and every email for a given conversation is threaded together, so Gmail collapses
`#some-channel` into one growing thread instead of hundreds of loose messages.

## Why a user token

A Slack **bot** token only sees channels the bot has been explicitly invited to.
To see everything *you* see, the app authenticates as you with a user token
(`xoxp-…`). That's why the manifest requests user scopes and subscribes to
events "on behalf of users" rather than bot events.

## Two delivery modes

| | `socket` | `poll` |
| --- | --- | --- |
| How | WebSocket push (Socket Mode) | `conversations.history` on a timer |
| Latency | instant | one sweep interval |
| Thread replies | yes | **no** |
| Missed while offline | lost | caught up from a saved cursor |
| Rate limits | none | significant (see below) |

`socket` is the default and is better in every way *if it works*. Slack documents
Socket Mode for bot events; delivery of **user** events over Socket Mode is
undocumented and has been reported to be inconsistent between workspaces. Rather
than assume, run `slack2email probe` — it connects, prints every event that
arrives, and tells you which mode to use.

If your workspace doesn't deliver user events, set `mode = "poll"`. Be aware that
Slack tightened `conversations.history` limits in 2025: non-Marketplace apps
created on or after 2025-05-29 are capped near **1 request per minute**, so a
sweep of N conversations takes roughly N minutes. Older apps get the far more
generous Tier 3 limit. The poller backs off automatically on HTTP 429.

## Setup

**1. Create the Slack app.** Go to <https://api.slack.com/apps> → *Create New App*
→ *From an app manifest*, pick your workspace, and paste in
[`slack-app-manifest.yaml`](slack-app-manifest.yaml).

**2. Install it and collect two tokens.**

- *OAuth & Permissions* → *Install to Workspace* → copy the **User OAuth Token** (`xoxp-…`).
- *Basic Information* → *App-Level Tokens* → *Generate* with the `connections:write`
  scope → copy the **app token** (`xapp-…`).

Many workspaces require an admin to approve app installs. If you don't have that
right, this step is where you'll need to ask.

**3. Create a Gmail App Password** at <https://myaccount.google.com/apppasswords>
(requires 2-step verification). Your normal password will not work for SMTP.

**4. Put the secrets in the macOS Keychain** rather than in a plain file:

```bash
security add-generic-password -s slack2email-user-token  -a "$USER" -w 'xoxp-...'
security add-generic-password -s slack2email-app-token   -a "$USER" -w 'xapp-...'
security add-generic-password -s slack2email-smtp        -a "$USER" -w 'your-app-password'
```

**5. Install and configure.**

```bash
pip install -e .
slack2email init --email you@example.com    # writes ~/.config/slack2email/config.toml (0600)
slack2email doctor                          # verifies tokens, scopes, and SMTP login
slack2email test-email                      # sends yourself one test message
slack2email probe                           # confirms which mode to use — post in Slack while it runs
```

**6. Run it.**

```bash
slack2email run                     # foreground, Ctrl-C to stop
slack2email install-agent --load    # or: run at login via launchd
```

Logs go to `~/Library/Logs/slack2email.log`. Remove with `slack2email uninstall-agent`.

## Configuration

`~/.config/slack2email/config.toml`. The file must be mode `0600`; slack2email
refuses to start otherwise, because it can contain tokens.

Secrets may be written inline or pulled from elsewhere:
`"env:VAR"`, `"file:/path"`, `"keychain:service"`, `"keychain:service/account"`.

Useful `[forward]` keys:

| key | default | meaning |
| --- | --- | --- |
| `mode` | `"socket"` | `socket` or `poll` |
| `batch_seconds` | `60` | digest window; `0` = one email per message |
| `own_messages` | `false` | also forward messages you sent |
| `bot_messages` | `true` | forward messages from apps/bots |
| `noise` | `false` | forward joins/leaves/topic changes |
| `channel_allowlist` | `[]` | if non-empty, forward *only* these |
| `channel_blocklist` | `[]` | never forward these |
| `timezone` | system | e.g. `"America/New_York"` |

Allow/block lists match either a channel name (`#general` or `general`) or an ID.

## Limitations

- **Edits and deletions aren't forwarded.** You get the message as first posted.
- **`poll` mode does not see thread replies**, because `conversations.history`
  returns only top-level messages. `socket` mode does.
- **In `socket` mode, messages posted while the forwarder is down are lost.**
  The launchd agent restarts it, but a laptop asleep for a day will miss that day.
  `poll` mode resumes from its cursor and catches up.
- Undeliverable email is written to `~/.local/state/slack2email/failed/*.eml`
  rather than dropped silently.

## A note on what this copies

Your DMs contain other people's messages, and this places a durable copy of them
in your mailbox and with your mail provider. That's your call to make, but it is
worth knowing, and some workspaces have policies about exporting message content.
`channel_blocklist` is there if you want to keep specific conversations out.

## Development

```bash
pip install -e . && python -m pytest tests -q
```
