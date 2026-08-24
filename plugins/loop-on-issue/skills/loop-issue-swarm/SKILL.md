---
name: loop-issue-swarm
description: "Autonomously pick up GitHub or GitLab issues that carry a queue label (default `loop`) and are assigned to a given user, claim them by stamping a state prefix onto the issue title, then run one resumable agent session per issue in its own git worktree through plan → code → review → pull/merge request. Tracks progress as [CLAIMED]/[WORKING]/[PAUSED]/[FINISHED]/[SKIP] title prefixes because both forges only offer open/closed, reports progress as marked issue comments, escalates blockers to a human, and on each run re-checks paused issues for answers and finished issues for review feedback so work resumes with its original context intact. Runs either `claude -p` or `codex exec`. Built to be driven unattended by a recurring routine, but works fine invoked by hand. Use this skill whenever the user wants to process an issue queue, drain the loop label, run the issue swarm, pick up assigned issues automatically, resume a PAUSED issue after replying, rework a PR/MR after review comments, check what the swarm is working on, or set up / debug the recurring routine that does any of that — including phrasings like '跑一轮 loop', '把 loop 队列清一下', '认领我的 issue 开始开发', 'what is the swarm doing', 'resume issue 612'."
---

# Loop Issue Swarm

Drain a labelled issue queue by running a full development cycle per issue,
unattended, with enough state on the board that a human can watch, interrupt, or
take over at any point.

Throughout, `$LOOP` is the plugin's CLI. Resolve it once:

```bash
LOOP=$(command -v loop 2>/dev/null || true)
[ -n "$LOOP" ] || LOOP="${CLAUDE_PLUGIN_ROOT:-${CODEX_PLUGIN_ROOT:-}}/scripts/loop"
[ -x "$LOOP" ] || LOOP=$(find ~/.claude/plugins ~/.codex/plugins -path '*loop-on-issue*/scripts/loop' 2>/dev/null | head -1)
```

**Run `"$LOOP" doctor` before the first run against a repository.** A missing
queue label or a misspelled assignee produces a board that looks idle rather than
broken, and you would spend the run discovering it. Exit 2 means stop and fix.

The CLI works the same against both forges. Where this document says **change
request**, that is a pull request on GitHub and a merge request on GitLab; the
commands accept `pr-` and `mr-` spellings interchangeably.

## Why the odd state machine

Issues on both forges are binary: open or closed. That cannot distinguish an
untouched issue from one an agent grabbed 3 seconds ago, one mid-refactor, one
blocked on a human, and one awaiting merge — and conflating them either
duplicates work or strands it.

So state lives in a **title prefix**: `[WORKING] fix drive URI late binding`. The
title appears in every issue list, notification email, and board card, so humans
and agents read the same state with no extra tooling — and a human can hand-edit a
prefix to redirect the swarm.

```
  no prefix ──────► [CLAIMED] ──────► [WORKING] ──────► [FINISHED] ────► human
  (the queue)                          ▲   │   ▲            │            closes
                                       │   │   │            │  merged
                        human replied  │   │   └────────────┘
                                       │   │   review feedback arrived
                                       │   ▼   needs a human decision
                                     [PAUSED]

  [CLAIMED] or [WORKING] ──────────► [SKIP]   needs no code change;
                                              dormant, never rescanned
```

`[FINISHED]` is **not** terminal. A change request that comes back with review
comments has to go somewhere, and dropping it loses work at the last and most
expensive step. So every run re-checks finished issues: unaddressed feedback sends
one back to `[WORKING]`, a merged change request is reported and left for the
human to close. Merging and closing stay human decisions — an agent that merges
its own work has no review gate.

Whatever the state change, **say so in an issue comment**. Recovering, skipping,
or reworking silently is what makes a board impossible to trust, and trust is the
only reason to let this run unattended.

### `[SKIP]` versus `[PAUSED]`

Not every queued issue wants code. `[SKIP]` retires those permanently — later runs
never look again — so the line matters:

| | means | cases |
|---|---|---|
| `[SKIP]` | there is no code change to make | already fixed on `base_branch`, superseded by another change request, describes behaviour that no longer exists, a question whose answer belongs in a comment, or **no substantive content at all** — placeholder title like `lalalala`, empty description, stray test issue |
| `[PAUSED]` | I cannot settle this myself | ambiguous requirement with real trade-offs, a failure you cannot reproduce, uncertainty about whether it is already fixed, a product call |

The test is **whether a question exists that a human could usefully answer**.
`lalalala` has none — asking "what does this mean?" just makes them write the
issue from scratch, which they can do unprompted. "Drive URIs break on resume"
without saying which resume path *does* have one, so it pauses.

Guard against collapsing `[PAUSED]` into `[SKIP]`: that turns skip into an escape
hatch and the queue drops real work while looking clean. Difficulty is never a
reason to skip, and hesitation means pause.

Retire through the CLI, which enforces a reason and posts it before changing state
— an unexplained `[SKIP]` is indistinguishable from an agent dodging work by the
time anyone reads the board:

```bash
"$LOOP" skip --id 612 --reason "Already fixed by #881; that write path is gone."
```

A human overrides by deleting the prefix, which requeues the issue. The posted
comment says so, because the person reading it next week will not remember.

## One resumable session per issue

Each issue is developed by its **own agent subprocess**, not an in-process
subagent. Not a style choice: in-process subagents are one-shot and have no
session id, so when an issue pauses for an answer or a change request comes back
for rework there is nothing to resume — a fresh agent would rebuild its reasoning
from issue comments alone, then quietly redo or contradict its own work.

Ask the CLI for the id rather than deriving one yourself; it knows which runner
the issue uses and where that runner's id comes from.

```bash
SID=$("$LOOP" session-id --id 612)   # exit 2 means "no session yet, start fresh"
```

### claude

The id is **derived from the issue's identity**, so any later run recomputes it
with no stored state:

```bash
cd .worktrees/loop-612 && claude -p --session-id "$SID" \
  --permission-mode acceptEdits "<brief>"

# resume — human answered, or the change request needs rework. The whole point.
cd .worktrees/loop-612 && claude -p --resume "$SID" \
  --permission-mode acceptEdits \
  "The human answered: \"<quote>\". Continue from where you stopped."
```

`--generation` is the escape hatch for a context that has gone bad and keeps
returning to a wrong path: `session-id --id 612 --generation 1` yields a fresh id.
Say in a comment that you did, and why.

### codex

`codex exec` **cannot be told a session id at start** — it assigns its own. So the
first run captures it from the event stream and records it, and later runs read it
back. That bookkeeping is the price of this runner; without it a paused issue can
never resume.

```bash
cd .worktrees/loop-612
codex exec --json --sandbox workspace-write -c approval_policy='"never"' "<brief>" \
  | tee /tmp/loop-612.jsonl
TID=$(grep -m1 -o '"thread_id":"[^"]*"' /tmp/loop-612.jsonl | cut -d'"' -f4)
"$LOOP" session-record --id 612 --session "$TID" --runner codex

# resume
cd .worktrees/loop-612 && codex exec resume "$SID" --json \
  --sandbox workspace-write -c approval_policy='"never"' "<continuation>"
```

Record the id **immediately** after capturing it, before any development work.
A session that crashes mid-run with an unrecorded id is unresumable, and you
cannot tell that apart from one that never started.

### Common to both

- **`cd` into the worktree first** — that is where the session's edits land; from
  the repo root it would edit the main checkout.
- **Pass the brief only on the first call.** A resume already holds the issue, the
  plan, and everything tried; re-pasting invites a restart.
- **Approvals must be non-interactive** or an unattended run stalls on the first
  prompt. If runs still stall, widen the mode rather than posting a human on
  standby.

## Parameters

Read these from `.loop-on-issue/config.json` first, then from the invoking prompt,
which overrides it. Only `assignee` has no usable default — "whose queue is this"
is the one thing that cannot be guessed, and guessing starts real work on issues
nobody asked for. If neither config nor prompt supplies it, stop and say so.

| Parameter | Config key | Default | Notes |
|---|---|---|---|
| assignee | `assignee` | — | **required**; the queue is filtered on it |
| queue label | `queue_label` | `loop` | other labels ignored |
| repo | `repo` | from git remotes | `owner/name`, or a nested GitLab path |
| base branch | `base_branch` | `origin/main` | what worktrees branch from and change requests target |
| concurrency | `max_parallel` | `2` | each slot is a full session running builds |
| session bound | `session_timeout` | `43200` | hard 12h cap per issue |
| runner | `runner` | `claude` | per-issue override via a `runner::codex` label |
| verification | `verify_command` | — | this repo's real test command |
| worktree extras | `env_files` | `[".env"]` | gitignored files to copy in |
| escalation | `escalation_command` | none | optional faster channel to a human |

The label is a **containment** check: `loop, bug, P1` qualifies exactly like `loop`
alone. Issues carry priority and area labels for human triage, and demanding an
exact match would mean nobody could triage a queued issue without dropping it out
of the queue. Read the other labels anyway — `bug` versus `enhancement` tells you
whether to start from a failing test, and `runner::codex` picks the agent.

`base_branch` moves together with the change request target: branching from
`origin/release-0.5` and then opening against `main` would smuggle the whole
release delta into the diff.

`session_timeout` is a **runaway detector, not a work-time budget** — real issues
do run over an hour, and killing one mid-progress wastes more than it saves. It
exists because an agent process can fail to exit at all: one was found alive for
**15 days**, still burning CPU, orphaned from a listener replaced twice since.
Know what long sessions cost, though — one holds a slot *and* makes the outer
routine skip its scheduled runs, so a 12-hour issue freezes the queue for 12
hours. The fix is sessions that commit and exit at a sensible point, staying at
`WORKING` for the next run to resume; splitting work across runs is what the
resumable session is for.

## The CLI

`$LOOP` owns every read and write against the board. Use it rather than
hand-rolling `gh` or `glab` — it already handles the prefix edge cases (stacked
prefixes from an interrupted transition, unrelated ones like `[TEST]`, literal
brackets in a title), the forge differences, and the marker below.

```bash
"$LOOP" list --label loop --assignee muxuan --json --active-only
"$LOOP" claim --id 612
"$LOOP" skip --id 612 --reason "<why this needs no code change>"
"$LOOP" transition --id 612 --to WORKING --expect CLAIMED
"$LOOP" transition --id 612 --to NONE               # release to the queue

printf 'note with **markdown** and 中文\n' | "$LOOP" comment --id 612 --body-file -
"$LOOP" comment --pr 882 --body "Addressed in <sha>."

"$LOOP" human-reply --id 612    # exit 0 = a human answered, 2 = still waiting
"$LOOP" pr-status --id 612      # linked change request; exit 2 = none attributed
"$LOOP" pr-feedback --id 612    # exit 0 = unaddressed review feedback
"$LOOP" session-id --id 612     # the id to resume with

"$LOOP" ask --id 612 --question "…" --option A --option B   # ask a human
"$LOOP" report --json-file -    # a run's outcome, to the issues and the group
```

Exit codes are load-bearing: **0** success, **2** precondition not met, **1** a
real error. Treat 2 as routine — losing a claim race or finding no reply yet is
expected, not worth escalating.

**Why comments carry a hidden marker.** The agent authenticates with the *same
account as the human*, so note authorship cannot tell them apart. Every note the
CLI posts starts with `<!-- loop-on-issue:agent -->` (invisible when rendered),
and "a human replied" means *an unmarked note newer than our last marked one*.
Without it a `[PAUSED]` issue could never wake up — the check would compare the
agent against itself. So **post every agent comment through the CLI**: a raw `gh`
or `glab` note lacks the marker and later reads as a human reply, waking an issue
nobody answered. `--no-marker` is only for relaying text a human actually wrote.

**How a change request is attributed to its issue.** On GitHub the native
development link (`closes #N` in the description) is authoritative, with the
`to #N` title convention as fallback. On GitLab the title convention is the *only*
rule, because the "related merge requests" listing returns every one that so much
as mentions the issue — on 2026-08-19 that attributed a third issue's work to two
others, and review feedback on the real ones could never wake them. Either way,
keep the `to #<id>` title and the `closes #<id>` line: retitling by hand and
dropping the closing keyword together makes a change request invisible to
`pr-status` and `pr-feedback`.

## Run flow

### Phase 0 — Reconcile and wake (always first)

This is what makes the loop a loop. A run is one session, and a paused issue's
answer arrives long after it ended, so **the next scheduled run is the wake-up
mechanism** — nothing else polls. The routine's interval is the worst-case latency
from a human answering to work resuming.

`list --active-only` drops the dormant `[SKIP]` bucket. If a
`<worktree_dir>/loop-<id>` directory survives for an issue that is now `[SKIP]`,
remove it — nothing will look at it again and an unattended loop otherwise
accumulates dead checkouts.

**`[CLAIMED]` / `[WORKING]` — orphans.** Any issue in these states *at the start
of a run* is an orphan: a scheduler skips a run while the previous one is still
going, so a live session's issue can never be visible to a starting run. That
inference is what makes recovery safe without heartbeat files or lock tables.

Inspect the worktree (`git -C <worktree_dir>/loop-<id> log --oneline -5`,
`status --short`), then resume its session with what you found rather than
restarting. If the worktree is missing or empty, transition back to `CLAIMED` and
start fresh.

**`[PAUSED]`.** Run `human-reply`. Exit 2 means still blocked: leave it alone and
do not count it against `max_parallel`. Exit 0 means an answer arrived, and how
you continue depends on where it paused:

- **worktree exists** (paused mid-development): transition to `WORKING` and resume
  the session with the answer quoted. This is what the resume mechanism is for.
- **no worktree** (paused during triage, before anything was built): there is no
  session to resume. Re-run triage with the answer in hand and continue through
  Phase 2. Never resume an id that was never started — `session-id` exits 2 for a
  codex issue with nothing recorded, which is exactly this case.

**`[FINISHED]`.** Run `pr-status`, then `pr-feedback`:

| Situation | Action |
|---|---|
| unaddressed feedback on an open change request | → `WORKING`, resume with the comments, rework, push, reply on it, → `FINISHED` |
| open, nothing new | leave it; mention it in the report |
| merged | report it; the human closes the issue. Leave the worktree unless asked — a merge is exactly when someone wants to inspect what ran |
| none (`pr-status` exit 2) | submit failed → `PAUSED`, comment that it is missing. The message names any change request that mentions the issue without claiming it — usually a hand-written title, occasionally the real answer |

### Phase 1 — Scan and claim

Claim up to `max_parallel` minus the issues already resumed in Phase 0, oldest
first — `list` returns creation order, so the queue drains fairly instead of
favouring whatever was filed last.

Claim **immediately** on selection, before any planning or file reading. The gap
between deciding to work an issue and marking it is the only window where two runs
can collide, so keep it near zero. If `claim` exits 2, move on.

`claim` also writes a comment recording which runner and which session now own the
issue. That is what makes the board — not a derivation rule — the answer to "who is
working this", so a later run, another machine, or a human can read it back
without recomputing anything. For a runner that assigns its own session id, the
comment records the runner now and `session-record` fills in the id at start.

Then comment on each claimed issue with a one-line statement of what you
understand the task to be — a human's earliest chance to catch a misread.

### Phase 2 — Triage, then swarm

**Triage each claimed issue before building anything**: read the body and comment
thread, and look at the relevant code in the main checkout **read-only** if you
need to tell whether the change already landed.

| Verdict | Action |
|---|---|
| no code change to make | `skip --id <n> --reason "…"`, stop |
| a real request, but blocked | comment the question, → `PAUSED`, stop |
| actionable | continue below |

**Do not create a worktree until an issue passes triage.** Beyond the wasted
setup, an empty worktree left behind by a skipped or paused issue makes the next
run's Phase 0 read it as work in progress — it then resumes a session that never
started and reports nonsense. Triage stays cheap on purpose: no worktree, no
branch, no session.

For each issue that passes, create the worktree, transition, and start the
session. Run up to `max_parallel` concurrently; worktrees isolate the filesystem,
so machine load is the real limit.

```bash
case "$base_branch" in */*) git fetch "${base_branch%%/*}";; *) git fetch;; esac
git worktree add "$worktree_dir/loop-$id" -b "<type>/loop-$id-<slug>" "$base_branch"
for f in $env_files; do [ -f "$f" ] && cp "$f" "$worktree_dir/loop-$id/$f"; done
"$LOOP" transition --id "$id" --to WORKING --expect CLAIMED
(cd "$worktree_dir/loop-$id" && \
   perl -e 'alarm shift @ARGV; exec @ARGV' "$session_timeout" <runner command>)
```

Three things about that, all of which have cost a run somewhere:

- **The path is `<worktree_dir>/loop-<id>` exactly**, branched off `base_branch`
  rather than a local `main` that may be stale. Phase 0 locates orphaned work by
  deriving the path from the id, so a creative name makes it invisible.
- **`env_files` are gitignored**, so `git worktree add` does not carry them over.
  The service and much of the test suite may read credentials and endpoints from
  them, and skipping the copy produces failures that look like code bugs.
- **Always bound the subprocess.** macOS ships neither `timeout` nor `gtimeout`,
  so the `perl -e 'alarm'` form is the portable one; it exits **142**
  (128 + SIGALRM). It kills the agent process but not its process group, so glance
  for stragglers if it fires repeatedly. On timeout leave the issue at `WORKING`
  and comment — Phase 0 picks it up as an orphan next run.

### Phase 3 — Report, to both surfaces

Per issue: final state, branch, change request URL if any, and what a human needs
to do next. Lead with anything in `PAUSED` — those hold up the queue.

Always list the issues you retired as `[SKIP]` this run, with reasons. Later runs
never revisit them, so this is the only moment a human is prompted to disagree.

Write it with `loop report`, which puts the summary in the group and a per-issue
note on each issue the run touched:

```bash
printf '%s' "$(cat <<'JSON'
{
  "summary": "3 claimed · 1 finished (!903) · 1 paused on #612 · 1 skipped\n…",
  "notes": {
    "630": "Finished this run: !903 open against main, awaiting review.",
    "612": "Paused: asked which writer is authoritative. Answer on this issue to resume."
  }
}
JSON
)" | "$LOOP" report --json-file -
```

Write each surface to stand on its own. The group message must not say "see the
issue", and an issue note must not say "see the group" — someone reading either
one is usually reading only that one. If the chat channel is not configured,
`report` says so and the issue notes still land; that is a normal outcome, not a
failure.

## Per-issue session brief

The brief handed to the runner on first start should carry the issue number,
title, full description and comment thread, the repo path, `base_branch`, the
`verify_command`, the triage verdict that let this issue through, and this
workflow:

### 1. Where you are

You start in `<worktree_dir>/loop-<id>`, on a fresh branch off `base_branch`, with
the configured `env_files` already copied in. Phase 2 set that up — do not create
worktrees or switch branches, because later runs locate your work by this exact
path.

A worktree has no virtualenv or `node_modules` of its own. Use the main
checkout's toolchain by absolute path; a relative path silently resolves to
nothing.

### 2. Plan

Read the issue and the surrounding code before writing anything. Post the plan as
an issue comment — bullets, files you expect to touch, how you will verify.
Cheapest checkpoint you get: a human who spots a misread here saves the whole
slot.

Triage established there is something to build, but depth changes the picture and
both exits stay open. If the plan shows the change is already there or the request
evaporates on inspection, `skip` it with that finding and `git worktree remove` so
nothing later mistakes it for live work. If it surfaces an ambiguity with real
trade-offs, escalate as a blocker. Either way, say so instead of inventing a
change to justify the slot.

### 3. Code

Implement it, following the repository's own conventions files. Tests-first is the
right default for anything with a definable failure mode — a bug especially, where
a red test proves you reproduced the actual problem rather than an adjacent one.

Then run the repository's real verification, which is the configured
`verify_command`, verbatim. Do not substitute a command you think is equivalent:
a green run of the wrong suite is worse than no run, because it gets reported as
evidence.

### 4. Review

Get fresh eyes before submitting: dispatch a reviewer subagent that has not seen
your reasoning, pointed at `git diff <base_branch>...HEAD`, checking correctness,
repo conventions, and whether the diff actually closes the issue. Fix what it
finds; where you disagree, say why in the change request description rather than
silently declining.

### 5. Submit

Use the **`forge-mr` skill**, with one deviation: **pass the existing issue number
as `issue_id`.** That skill auto-creates an issue when none is given, and here the
issue is the whole point — a duplicate would split the discussion and break the
queue's bookkeeping.

Its other defaults are already right: push the branch to `push_remote`, target
`base_branch`, squash, title `to #<id>: <Title>`, and `closes #<id>` in the
description. **Both of those are load-bearing** — see attribution above.

Then transition the issue to `FINISHED` and comment with the URL.

### 6. On blockers

A blocker is anything where guessing could waste the work: an ambiguous
requirement, a design decision with real trade-offs, a failing test you cannot
attribute, a missing credential, a conflict needing product judgement.

1. **Ask through `loop ask`.** It writes the question to the issue — the durable
   channel, what the next run reads — and pushes it to chat in the same step, so a
   human who is around can settle it in minutes instead of a routine interval.

   ```bash
   "$LOOP" ask --id 612 \
     --question "Resume paths differ on the two writers; which one is authoritative?" \
     --option "The streaming writer — the batch one is legacy" \
     --option "The batch writer, and the streaming one should follow it" \
     --option "Park it: I need to look at the data first"
   ```

   Make the question answerable in one sentence, and **offer a park option**:
   someone who is looking but cannot decide now should be able to say so without
   the queue stalling on silence.

   `ask` exits **2** when nobody has answered, which is the expected outcome, not
   a failure. It does not wait by default, deliberately — see below.

2. Then transition to `PAUSED` and **end the session**, leaving the worktree and
   branch intact so the next run resumes this same session instead of rebuilding.

**Do not wait for the human.** No retry loop, no `sleep`, no second ask, no
polling the issue. The outer routine *is* the waiting mechanism and it waits for
free; a session that blocks for fifteen minutes holds a slot, keeps the machine
warm, and still ends up paused if the human was at lunch.

The one exception is not yours to make: if a session calls `AskUserQuestion`, a
hook intercepts it and waits a short, bounded window before telling the session to
pause. That is the same channel, taken automatically, because a headless session
has no other way to answer that tool.

`escalation_command` remains for anyone routing to Slack, Feishu or a pager
instead of DingTalk; when it is set, invoke it after `ask` and require that it
**push and return** rather than wait.

And park rather than abandon: releasing a stuck issue with `--to NONE` guarantees
the next run repeats the same failure, while `PAUSED` puts it in front of a human.

## Safety boundaries

An unattended loop compounds mistakes across runs, so these are hard lines.

- **Never merge a change request, and never close an issue.** `FINISHED` hands off
  to a human.
- **Never touch an issue outside the filter** — wrong label, wrong assignee, or
  carrying a prefix you did not write.
- **Never force-push a branch you did not create**, and never push to the target
  remote when it differs from `push_remote`.
- **Never commit credentials.** Stage specific files; `git add -A` in a worktree
  that just received copied `env_files` is how secrets leak.
- **Stay inside the worktree directory** for all code changes. The main checkout
  may hold the user's own uncommitted work, and a routine that stomps it is
  unrecoverable.
- **Never post an agent comment outside the CLI.** An unmarked note later reads as
  a human reply and wakes an issue nobody answered.
