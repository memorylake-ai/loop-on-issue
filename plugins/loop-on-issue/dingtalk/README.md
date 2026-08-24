# The DingTalk channel

Three jobs, and nothing else:

1. **Relay questions.** An unattended session that needs a human decision gets it
   asked in the group and the answer injected back — replacing the wait-for-the-
   next-scheduled-run round trip with a wait for whoever is holding their phone.
2. **Report, dual-written.** A run's summary goes to the group, and every issue
   it touched gets a comment saying what happened to it. Both are written to be
   read on their own.
3. **Take requirements.** Anyone can raise one; it is held locally until the one
   configured approver releases it, and only then does an agent decompose it into
   queue-ready issues.

Nothing anyone says reaches a repository until the approver has said yes. That gate
is the only thing between a chat message and unattended code changes, which is why
it is one named person and why starting a session on an existing issue (`/dev`)
sits behind the same gate.

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
| **anything without a leading `/`** | files a requirement |
| **quote-reply a question card** | answers exactly that question, even with several open |
| `/a <id> <text>` | answers a specific issue, no card needed |

There is no guessing. Only a quote-reply answers a question; a plain sentence is
always a requirement. An earlier version read a bare message as an answer whenever
some question happened to be open, which meant a new requirement could be
swallowed by a question nobody was thinking about.

### Commands

```
/new <requirement>          file a requirement (same as sending it plainly)
/p                          requirements awaiting approval
/r <ID>                     one request: status, approval, what it produced
/q                          open questions
/ls [state]                 the issue queue by state
/i <id>                     one issue: state, runner, session, link
/a <id> <text>              answer an issue
/repos                      repositories this bot serves

同意 <ID> [repo] [note]      approve                      (approver only)
拒绝 <ID> <reason>           reject                       (approver only)
/dev <issue> [repo]         put an agent on that issue   (approver only)

/skip <id> confirm <why>    retire an issue
/requeue <id> confirm       put it back in the queue
/report                     re-send the last run summary
/whoami  /ping  /h
```

Destructive commands are confirmed by repeating them with the word `confirm`.
Nothing is remembered between the two messages, so restarting the listener in
between changes nothing.

## The requirement flow

```
@bot 把首页 CTA 改强一点
   → held locally as R20260824-01, pending. Nothing has touched any repository.
   → approver: 同意 R20260824-01 bloom 注意别动定价页
     (the repo is optional and redirects it; the note carries the same weight
      as the requirement itself from that moment on)
   → the serial worker runs loop-issue-creator in bloom's checkout
   → issues created, and the report comes back to chat
   → the swarm picks them up on its own schedule
```

**Nothing reaches a repository before approval.** An earlier design filed the
requirement immediately as an unqueued issue, which meant anybody who could message
the bot could write to the repository — and an issue tracker full of unapproved
one-liners stops being readable. The decision to build something deserves a durable
public record; the request to build it does not.

So a pending request lives in `~/.loop-on-issue/intake/<ID>/`, outside any
repository and outside version control, alongside the agent log and the report from
running it. `loop intake` lists them from a terminal; `loop intake --id <ID>` shows
one in full.

Requests nobody decides on expire after `intake_ttl`. Approved ones never do —
that would pull work out from under the agent holding it.

## Putting an agent on an existing issue

```
/dev 612            # the default repository
/dev 612 bloom      # a specific one
```

Approver-only, for the same reason approving is: both put an unattended agent to
work. It goes through the same queue, the same log directory and the same report,
and runs the `loop-issue-swarm` skill scoped to that one issue — so every safety
boundary that skill states still holds, including never merging and never closing.

## Several repositories

One bot can serve several. The registry is machine-level, because when a request
arrives nobody has yet decided which repository it belongs to:

```sh
loop repos add loop  memorylake-ai/loop-on-issue  ~/github/loop-on-issue
loop repos add bloom org/bloom                    ~/github/bloom
loop repos default loop
loop repos                     # what is registered, and which is the default
```

A requirement goes to the default, and the approver can redirect it in the same
breath as approving: `同意 R20260824-01 bloom`. With several registered and no
default set, a bare requirement is refused rather than filed somewhere arbitrary —
that mistake surfaces as a stranger's issue tracker filling up.

Each entry needs a **local checkout**, not just a project path: that is where the
agent runs.

## One job at a time

Approved work is drained by a single serial worker. Two agents in the same checkout
fight over git state and two anywhere compete for the same quota, and decomposing a
requirement is not urgent enough to be worth either. Jobs are durable, so a listener
that dies mid-queue picks up where it left off on restart.

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
