# The DingTalk channel

Three jobs, and nothing else:

1. **Relay questions.** An unattended session that needs a human decision gets it
   asked in the group and the answer injected back — replacing the wait-for-the-
   next-scheduled-run round trip with a wait for whoever is holding their phone.
2. **Report, dual-written.** A run's summary goes to the group, and every issue
   it touched gets a comment saying what happened to it. Both are written to be
   read on their own.
3. **Take requirements.** A requirement raised in the group becomes an issue,
   and — once approved — is decomposed into queue-ready work by
   `loop-issue-creator`.

It never runs the swarm. Filing issues is where it stops, which is what makes the
approval gate the only thing between a group message and unattended code changes.

## What is optional, and what is lost without it

The whole plugin works with none of this configured. `loop ask` writes its
question to the issue and the next scheduled run picks up the answer, exactly as
before. What DingTalk adds is speed and a way in — not correctness.

| Configured | You get |
|---|---|
| nothing | questions on the issue, answered on the issue |
| `LOOP_DINGTALK_WEBHOOK` only | notifications in the group; answers still go on the issue |
| app robot + Stream listener | answer in chat, run commands, raise requirements |

The listener is the **only** part of this plugin with a pip dependency. It keeps
its own virtualenv, so nothing else is affected either way.

## One-time DingTalk console setup

You need an internal enterprise app with the **robot** capability in **Stream
mode**. Stream is not a preference: a custom group webhook can only *send*.
Stream receives over a long connection, which needs no public callback URL, no
tunnel and no open port — and it is the only way an answer can ever come back.

1. <https://open-dev.dingtalk.com/> → sign in as an enterprise administrator →
   pick the right enterprise → **应用开发 → 企业内部应用**.
2. Create an app (or reuse one) and note its **AppKey** and **AppSecret**.
3. **应用能力 → 机器人 → 添加**. Give it a short name — that is what people type
   after `@`. Set **消息接收模式 = Stream**. This is the step most often got
   wrong; choosing HTTP mode asks for a public callback URL and nothing will work
   locally.
4. **版本管理与发布 → 创建新版本 → 发布.** Skip this and the robot does not appear
   in the group's "add robot" list at all.
5. In the group: **群设置 → 智能群助手 → 添加机器人 → 企业内部开发** → pick yours.

## Credentials

Never in a repository. Written to `~/.loop-on-issue/dingtalk.env`, mode `600`:

```sh
DINGTALK_CLIENT_ID="ding..."
DINGTALK_CLIENT_SECRET="..."
DINGTALK_ROBOT_CODE="ding..."          # usually the same as the client id

LOOP_DINGTALK_CONVERSATIONS=""         # filled in below
LOOP_DINGTALK_APPROVER=""              # filled in below
LOOP_DINGTALK_APPROVER_NICK=""

# Optional send-only fallback for a machine with no app credentials.
LOOP_DINGTALK_WEBHOOK=""
LOOP_DINGTALK_WEBHOOK_SECRET=""
```

Resolution order: `$LOOP_DINGTALK_ENV` (and *only* that, when set) →
`~/.loop-on-issue/dingtalk.env` → `$LOOPS_DIR/.env.dingtalk` → the process
environment.

## Bring it up

```sh
dingtalk/run-bot.sh --selftest     # builds its venv on first run, ~20s
dingtalk/run-bot.sh                # foreground, to watch it connect
```

Then, in the group:

```
@Loop助手 /whoami
```

It answers with your `staffId` and this conversation's id, already formatted for
pasting. **`/whoami` is the one command an unlisted conversation may run** —
without that exemption the allow-list is a deadlock, since you cannot learn a
conversation id from a conversation that ignores you.

Paste both values into the env file, re-run `--selftest` — everything green — and
then:

```sh
dingtalk/run-bot.sh --daemon       # background; log in dingtalk/bot.log
dingtalk/run-bot.sh --status
dingtalk/run-bot.sh --stop
```

**Run exactly one.** Two listeners each receive every message and act on it twice.

> The listener runs on your machine. Closing the lid takes it offline, and DingTalk
> does not redeliver messages sent while it was away. Questions and requirements
> are unaffected — they are on the board and wait there.

## Using it

| | |
|---|---|
| **quote-reply a question card** | answers exactly that question, even with several open |
| **reply without quoting** | answers the newest open question |
| `/a <id> <text>` | answers a specific issue, no card needed |
| `@bot <requirement>` | files a requirement (when no question is waiting) |
| `/new <requirement>` | files a requirement, unambiguously |

A bare message is read as an answer while a question is open, and as a new
requirement otherwise. The bot always says which reading it took, so a wrong guess
is visible immediately — and `/a` and `/new` force either one.

### Commands

```
/q                       open questions
/ls [state]              the board by state
/i <id>                  one issue: state, runner, session, link
/a <id> <text>           answer an issue
/new <requirement>       file a requirement
同意 <id> [note]          approve            (approver only)
拒绝 <id> <reason>        reject             (approver only)
/now <id>                decompose now      (approver only)
/report                  re-send the last run summary
/skip <id> confirm <why> retire an issue
/requeue <id> confirm    put it back in the queue
/whoami  /ping  /h
```

Destructive commands are confirmed by repeating them with the word `confirm`.
Nothing is remembered between the two messages, so restarting the listener in
between changes nothing.

## The requirement flow

```
@bot 把首页 CTA 改强一点
   → intake issue #712, unqueued, [PAUSED], approval requested
   → approver: 同意 712 注意别动定价页
   → approval recorded on #712 (the note carries the same weight as the request)
   → loop-issue-creator decomposes it with --epic 712
   → its draft is confirmed through the same question channel
   → slices created, queued, Part of #712 · #712 → [FINISHED]
   → swarm picks them up on its own schedule
```

The intake issue is created **before** approval and **without** the queue label,
so the swarm cannot see it. That is why there is no local queue file and no
approval state machine: the board is both.

`creator_mode` decides who decomposes an approved requirement. `routine` — the
default — leaves it for the next scheduled run, which keeps the listener stateless
and therefore safe to restart at any moment. `immediate` has the listener spawn
one headless agent; `/now <id>` forces that on demand.

## Who may do what

| | Who |
|---|---|
| answer a question | anyone in an allow-listed conversation; their name is recorded on the issue |
| approve or reject a requirement | only `LOOP_DINGTALK_APPROVER` |
| a requirement raised by the approver | auto-approved, and recorded as such |

The two are held to different standards deliberately. Answering steers work that a
human already framed; approving lets a chat message become unattended code
changes.

## When something is wrong

| Symptom | Cause |
|---|---|
| no reply to anything | robot not in the group, app not published, or mode is HTTP not Stream |
| `/whoami` works, nothing else does | the conversation is not in `LOOP_DINGTALK_CONVERSATIONS` |
| "只有 … 能执行" | `LOOP_DINGTALK_APPROVER` is someone else, or unset |
| answers land on the wrong issue | two listeners running; check `--status` |
| `requires python-socks` | a system proxy is set; delete `dingtalk/.venv` and re-run |
| replies arrive twice | two listeners, or a `bot.pid` left behind by a kill |
