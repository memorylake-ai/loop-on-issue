# DingTalk trigger — replacing loopcue with an issue-centric conversation channel

**Date:** 2026-08-24
**Status:** approved, ready to implement
**Supersedes:** the `escalation_command`-only escalation path in the base design

## Problem

The open-source plugin currently escalates to a human through a configurable
`escalation_command`, which in practice means a private tool (`loopcue`) that
cannot ship. Without it the only channel to a human is an issue comment, and the
only wake-up mechanism is the next scheduled run — so every blocker costs a full
routine interval even when someone is looking at their phone right now.

Two further gaps: a headless session cannot use `AskUserQuestion` at all (there is
no UI to answer it), and there is no way to put work *into* the queue from where
the team actually talks.

## What the bot is for

Three responsibilities, and nothing else:

1. **Hook `AskUserQuestion`** — an unattended session that asks a question gets it
   relayed to DingTalk and the answer injected back.
2. **Report, dual-written** — a run's summary goes to the group *and* each issue
   the run touched gets a comment saying what happened to it.
3. **Requirement intake, gated by approval** — a requirement raised in the group,
   once approved, is decomposed into queue-ready issues by `loop-issue-creator`.

It never runs the swarm. Filing issues is the boundary; execution stays with the
scheduled routine, which is what makes the approval gate the only thing standing
between a group message and unattended code changes.

## What we take from the prior art, and what we change

`loopcue` (`cloudservice/loopcue`) established the mechanism this reuses:

- DingTalk **Stream mode** is the only way to *receive*; a webhook is send-only.
- Outbound `oToMessages/batchSend` / `groupMessages/send` returns a
  **`processQueryKey`** (pqk); a user's **quote-reply** carries
  `originalProcessQueryKey` equal to it. That is exact, parallel-safe routing with
  no ticket numbers.
- Stream delivery is **at-least-once**: dedupe on `msgId`. Skipping this is a real
  P0 — a redelivered bare reply gets treated as new and answers the *second*
  newest question.
- Three-way dispatch: `/`-prefixed → command; quote-reply → its pqk; bare reply →
  newest pending.
- `ask_human`'s four hard constraints: bounded timeout, sender authorisation,
  structured answer (index selects an option, otherwise free text), idempotent
  posting.

**The one thing we change is the routing target.** loopcue routes an answer back
to *the session that asked*, through a local file rendezvous. We route it to
**the issue**, as a comment. That follows from the decision that the issue is the
single source of truth, and it collapses a lot of machinery:

| loopcue | here |
|---|---|
| `pending/`, `replies/`, `.buffer` file protocol | none — the answer is an issue comment |
| session registry, `/s ls`, fork, recover, headless manager | none — no session management |
| `queue/<ID>.json` intake state machine (the trigger bot) | none — the intake *is* an issue |
| answer survives only on that machine | answer is on the board, readable anywhere |

The pqk → issue index remains, but it is only an **index**: losing it degrades to
"a bare reply answers the newest open question", it is never the record.

## Components

### `loop ask` — the primitive (standard library only, no daemon required)

```
loop ask --id 612 --question "…" [--option A --option B] [--wait 120] [--dry-run]
```

1. Posts the question as a **marked** issue comment. This is the durable channel
   and it works with no DingTalk configured at all.
2. Sends a DingTalk card, keeps the returned pqk, and records
   `pqk → (repo, issue, options)` in the machine-level index.
3. Polls `human-reply` on the issue until `--wait` expires.
4. Exit **0** with the answer on stdout; exit **2** on timeout (nobody answered).

A digit in the answer selects an option; anything else is returned as free text.
`try/finally` plus SIGINT/SIGTERM handling removes the index entry on the way out,
and a TTL sweep collects orphans from a SIGKILL.

**On waiting.** The swarm's standing rule is *do not wait for the human* — a
blocked session holds a slot. `--wait` defaults to **0** (post and return
immediately, exit 2) precisely so the swarm's blocker path is unchanged. Only the
hook passes a non-zero wait, and only a short one: someone may be looking at their
phone right now, and if not, the session falls back to `[PAUSED]` as before.

### The `AskUserQuestion` hook (Claude Code)

`PreToolUse` matching `AskUserQuestion`. The hook reads the tool input, calls
`loop ask --id $LOOP_ISSUE --wait <short>`, and:

- **answered** → exit 2 with the answer on stderr. Claude Code feeds a blocked
  tool call's stderr back to the model as the reason, which is the only injection
  mechanism available — there is no way for a hook to supply a synthetic tool
  result.
- **not answered** → exit 2 with text telling the session the question is on the
  issue and to pause.
- **`LOOP_ISSUE` unset** → exit 0, allow. A session outside the loop is untouched.

Codex has hooks but no documented tool-use event; there, the skill calls
`loop ask` explicitly at its blocker step, which is the same code path.

### `loop report` — the dual write

```
loop report --json-file -     # {"summary": "...", "notes": {"612": "...", ...}}
```

Each note becomes a marked comment on its issue; the summary goes to the group.
Both surfaces are written to be read on their own — the group message never says
"see the issue", and the issue comment never says "see the group".

### `loop dingtalk serve` — the listener

Singleton, Stream mode, and the only component with a pip dependency
(`dingtalk-stream`); it lives in `dingtalk/` with its own self-managing venv, so a
plugin installed without it works exactly as before, minus the group.

Inbound handling, in order: dedupe on `msgId` (10-minute window) → authorise the
conversation → dispatch three ways.

**Answers** (quote-reply or bare) are mirrored into the issue as an **unmarked**
comment reading `<nick> answered in DingTalk: …`. Unmarked is deliberate: that is
what makes `human-reply` see it, and the marker — not authorship — is what
separates agent from human, so the bot writing as the agent's own account is fine.

**Commands** — board-facing only, never execution:

```
/h  /ping                     help, liveness
/q                            open questions, with issue links
/ls [state]                   the board by state
/i <id>                       one issue: state, runner, session, last question, CR
/a <id> <text>                answer issue <id> explicitly
/report                       re-send the last run summary
/skip <id> <reason>           retire an issue          (confirm required)
/requeue <id>                 strip the prefix         (confirm required)
/now <id>                     run the creator on an approved intake issue now
```

### Requirement intake

A requirement in the group becomes an **intake issue** immediately — created
`--no-queue` so the swarm cannot see it, carrying the requester, the source
conversation, and the request verbatim.

```
@bot <requirement>
  → intake issue #712 created, [PAUSED], approval asked via `loop ask`
  → approver replies 同意 → recorded as a comment, → [WORKING]
  → loop-issue-creator runs with --epic 712
  → its draft checkpoint asks through `loop ask --id 712`
  → slices created (Part of #712, queued), #712 → [FINISHED]
  → result dual-written: comment on #712 and a group message
```

`[PAUSED]` for "awaiting approval" is exact rather than a stretch: it already
means *I cannot settle this myself*. Approval detection is `human-reply`, so no
new state machinery exists anywhere in this flow.

**Approval is stricter than answering**, because they carry different risk. A
single configured approver may approve; anyone else saying 同意 is refused out
loud. A requirement raised *by* the approver needs no second approval and is
recorded as auto-approved. Answering a question stays open to anyone in an
allow-listed conversation, with the answerer's name recorded.

**Who runs the creator** is configurable. Default `routine`: the intake issue is
marked approved and the next scheduled run decomposes it — the listener stays
stateless, which is what makes it survive its own restarts. `immediate` spawns a
headless agent from the listener, and `/now <id>` forces that on demand.

## Session id write-back

`claim` now always posts a marked comment recording the runner and, where it is
knowable at claim time, the session id. `session-id` reads the newest recorded
session first and falls back to derivation only when nothing is recorded — so
issues already `[PAUSED]` on an existing board keep resuming into their original
context, while everything created from now on is answerable from the board alone.

For codex the id does not exist until the process starts, so the claim comment
records only the runner and `session-record` fills in the id immediately after
start. Recording a placeholder would be worse than recording nothing: it would
look authoritative.

## Configuration

Credentials never enter the repository. Resolved in order:
`$LOOP_DINGTALK_ENV` → `~/.loop-on-issue/dingtalk.env` → `$LOOPS_DIR/.env.dingtalk`
(compatibility with an existing deployment) → the process environment.

```
DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET / DINGTALK_ROBOT_CODE
LOOP_DINGTALK_CONVERSATIONS      allow-listed openConversationIds
LOOP_DINGTALK_APPROVER           staffId; the only account that may approve
LOOP_DINGTALK_APPROVER_NICK
LOOP_DINGTALK_WEBHOOK[_SECRET]   optional send-only fallback
```

Repository-level settings stay in `.loop-on-issue/config.json`: `ask_wait`,
`intake_label`, `creator_mode` (`routine` | `immediate`), and the existing
`escalation_command`, which remains as the generic escape hatch for anyone who
wants Slack or Feishu instead.

## Testing

The listener's decidable parts are pure and tested without the SDK: `msgId`
dedupe, three-way dispatch, command parsing, approval gating, answer parsing
(index versus free text), pqk index round-trip and TTL. Outbound DingTalk is
tested against a fake HTTP layer. The live round trip against a real group is not
something tests can cover and is called out as such.

## Out of scope

- Session management of any kind: no session registry, no fork/recover/attach, no
  headless supervision beyond the optional single creator spawn.
- Triggering a swarm run from the group.
- Any IM other than DingTalk; `escalation_command` covers those.
