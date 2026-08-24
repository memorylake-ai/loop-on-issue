---
name: dingtalk-bot
description: "Set up, operate, or switch off the optional DingTalk bot that gives loop-on-issue a conversation channel: relaying an unattended agent's questions to chat and injecting the answers back, reporting a run to the group and the issues at once, and taking requirements from chat that one named approver releases into the queue. Covers the DingTalk console steps (robot capability, Stream mode, publishing), credentials, the conversation allow-list, the approver, registering several repositories, and enabling or disabling the whole feature. Use this skill whenever the user wants to set up or configure the DingTalk bot, run or stop the listener, turn the bot on or off, add a repository the bot serves, change who approves, debug why the bot is not answering or why a message was ignored, or asks what the bot can do — including phrasings like '配一下钉钉'、'把 bot 关了'、'初始化钉钉机器人'、'bot 怎么不理我'、'加一个仓库给 bot'、'set up the dingtalk bot'、'disable the bot'、'why is the bot ignoring me'."
---

# The DingTalk bot

**Entirely optional.** Everything in this plugin works without it: a question goes
on the issue and the next scheduled run reads the answer. What the bot adds is
speed — an answer in minutes rather than an interval — and a way to put work in
from where the team already talks.

Throughout, `$LOOP` is the plugin's CLI:

```bash
LOOP=$(command -v loop 2>/dev/null || true)
[ -n "$LOOP" ] || LOOP="${CLAUDE_PLUGIN_ROOT:-${CODEX_PLUGIN_ROOT:-}}/scripts/loop"
[ -x "$LOOP" ] || LOOP=$(find ~/.claude/plugins ~/.codex/plugins -path '*loop-on-issue*/scripts/loop' 2>/dev/null | head -1)
```

The listener itself lives at `<plugin>/dingtalk/run-bot.sh` and is the only piece
with a pip dependency; it manages its own virtualenv.

## What it does, and the one line it does not cross

1. **Relays questions.** An unattended session that needs a decision gets it asked
   in chat, and the answer comes back as an issue comment.
2. **Reports to both surfaces.** A run's summary to the conversation, a per-issue
   note on each issue it touched.
3. **Takes requirements.** Anyone may raise one. It is held **locally** until the
   one configured approver releases it; only then does an agent decompose it.

**Nothing anyone says reaches a repository until the approver has said yes.** That
gate is the only thing between a chat message and unattended code changes, which is
why it is one named person, and why `/dev` — putting an agent on an existing issue —
sits behind the same gate. Answering a question is open to anyone in an allow-listed
conversation, because that steers work a human already framed.

## Setting it up

Walk the user through this in order and stop at the first thing that is not done —
each step depends on the one before it.

### 1. The DingTalk console (a human must do this)

You cannot do this part. Hand it over, and hold two points, because they are the
ones people get wrong:

- **消息接收模式 must be Stream, not HTTP.** HTTP asks for a public callback URL and
  nothing works from a laptop. Stream needs no URL, no tunnel and no open port —
  and it is the only mode that can *receive*, which is the entire point.
- **The app must be published** (版本管理与发布), or the robot never appears in the
  "add robot" list at all.

Then add the robot to the group, or start a private chat with it. The full walk-
through is in `<plugin>/dingtalk/README.md`; point at it rather than paraphrasing.

### 2. Credentials

`~/.loop-on-issue/dingtalk.env`, mode 600, **never in a repository**:

```sh
DINGTALK_CLIENT_ID="ding…"
DINGTALK_CLIENT_SECRET="…"
DINGTALK_ROBOT_CODE="ding…"        # usually the same as the client id
```

Never print a secret back to the user, never echo one into a shell transcript, and
never commit one.

### 3. Bring the listener up and bootstrap the rest

```bash
<plugin>/dingtalk/run-bot.sh --selftest    # builds its venv the first time
<plugin>/dingtalk/run-bot.sh               # foreground, to watch it connect
```

Then have them send **`/whoami`** in the conversation they want to use. It replies
with the staffId and conversation id already formatted for pasting.

That command is deliberately exempt from the allow-list, and it has to be: without
it, filling in the allow-list would require a value only an allow-listed
conversation would tell you. Everything else from an unlisted conversation is
ignored, silently and on purpose.

Paste both into the env file:

```sh
LOOP_DINGTALK_CONVERSATIONS="cid…"          # comma-separated; empty means none
LOOP_DINGTALK_APPROVER="0147…"              # the only account that may approve
LOOP_DINGTALK_APPROVER_NICK="…"
LOOP_DINGTALK_DM_USERS="0147…"              # set for a private chat, empty for a group
```

`LOOP_DINGTALK_DM_USERS` is separate from the allow-list on purpose: a group and a
private chat need **different endpoints**, and their ids look alike, so which to use
cannot be inferred. A card sent to the group endpoint with a private-chat id is
accepted and never delivered — the worst kind of failure, because nothing errors.

### 4. Repositories

The registry is machine-level, because when a request arrives nobody has yet decided
which repository it belongs to. Each entry needs a **local checkout** as well as a
project path: that is where the agent runs.

```bash
"$LOOP" repos add loop memorylake-ai/loop-on-issue ~/github/loop-on-issue
"$LOOP" repos default loop
"$LOOP" repos
```

With several registered and no default, a bare requirement is **refused** rather
than filed somewhere arbitrary. Do not resolve that by picking one — say which are
registered and let them choose.

### 5. Run it for real

```bash
<plugin>/dingtalk/run-bot.sh --selftest    # everything green
<plugin>/dingtalk/run-bot.sh --daemon
<plugin>/dingtalk/run-bot.sh --status
```

**Exactly one listener.** Two each receive every message and act on it twice, which
on an approval means the same job runs twice.

## Switching it off

```bash
"$LOOP" dingtalk disable
"$LOOP" dingtalk enable
"$LOOP" dingtalk              # what is configured, and whether it is on
```

Disabled, the plugin behaves exactly as one that never had the feature: the
`AskUserQuestion` hook becomes a no-op so the tool works normally, and nothing is
sent anywhere. Questions still land on issues and the swarm still reads answers
from them — that path never depended on chat.

Stop the listener separately (`run-bot.sh --stop`); the switch does not kill a
running process.

## When it is not answering

Check in this order — each rules out everything below it:

| Symptom | Cause |
|---|---|
| nothing at all, even `/whoami` | robot not in the conversation, app not published, or mode is HTTP not Stream |
| `/whoami` works, nothing else | the conversation is not in `LOOP_DINGTALK_CONVERSATIONS` |
| no cards arrive, replies work | `LOOP_DINGTALK_DM_USERS` wrong for the conversation type |
| "只有 … 能执行" | `LOOP_DINGTALK_APPROVER` is someone else, or unset |
| answers land on the wrong issue | two listeners running — check `--status` |
| approvals do nothing | no local checkout registered for that repository, or `claude` not on PATH |
| everything ignored, `dingtalk` says disabled | somebody ran `dingtalk disable` |

`<plugin>/dingtalk/bot.log` records every inbound message with its conversation id,
sender staffId and routing key, which settles most of these in one glance.
`<plugin>/dingtalk/run-bot.sh --simulate "<text>"` runs a message through the real
pipeline with no group involved — useful before anyone is watching, and for
reproducing a message that misbehaved. It is not a dry run: a command that writes
to the board writes to it.

## Inspecting from a terminal

```bash
"$LOOP" intake                # requirements awaiting a decision
"$LOOP" intake --id R…        # one in full: approval, note, log, what it produced
"$LOOP" dingtalk sweep        # drop stale open-question routing entries
```

A pending request lives in `~/.loop-on-issue/intake/<ID>/` with the agent log and
report beside it — outside any repository and outside version control, which is the
point: an unapproved requirement should not be in anyone's git history.
