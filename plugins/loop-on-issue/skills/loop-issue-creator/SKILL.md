---
name: loop-issue-creator
description: "Turn a requirement into GitHub or GitLab issues that the loop-issue-swarm queue can actually execute: decomposed into slices that each fit one session slot, grounded in code you actually read, filled in against the repository's own issue template, and labelled and assigned so an unattended agent can start on one without needing to ask a question first. Use this skill whenever the user wants to break a requirement down into issues, file work for the swarm to pick up, decompose a spec or PRD document into issues, split an existing epic issue into children, turn a design just discussed in this conversation into issues, or fill the loop queue — including phrasings like '把这个需求拆成 issue'、'拆需求'、'给 swarm 喂几个 issue'、'这个 epic 拆一下子任务'、'decompose this spec into issues'、'file these as loop issues'."
---

# Loop Issue Creator

Write the issues that **loop-issue-swarm** drains. Throughout, `$LOOP` is the
plugin's CLI:

```bash
LOOP=$(command -v loop 2>/dev/null || true)
[ -n "$LOOP" ] || LOOP="${CLAUDE_PLUGIN_ROOT:-${CODEX_PLUGIN_ROOT:-}}/scripts/loop"
[ -x "$LOOP" ] || LOOP=$(find ~/.claude/plugins ~/.codex/plugins -path '*loop-on-issue*/scripts/loop' 2>/dev/null | head -1)
```

Works identically on both forges. If `"$LOOP" doctor` reports blocking problems,
fix those first — filing into a queue whose label does not exist produces issues
nothing will ever pick up, and nothing errors.

## What you are actually writing

Not a ticket for a human who will ask you a follow-up. The reader is a headless
agent that claims the issue, gets one of `max_parallel` **slots**, and must reach
a change request without you. Its escalation channel is narrower than it looks:
desktop notifications have been observed to be silently blocked by permissions in
a non-interactive session, so no prompt ever fired. The issue body and its comment
thread are the only channel that reliably works, and a round trip through them
costs a full routine interval.

So every defect you leave becomes a specific, observable cost:

| Defect | What the swarm does with it |
|---|---|
| nothing substantive to build | `[SKIP]` — retired permanently, never rescanned |
| ambiguity with real trade-offs | `[PAUSED]` — a routine interval per round trip |
| already built | claimed and triaged before anyone notices — a wasted slot |
| two queued slices touching one module | parallel worktrees, conflicting change requests |
| too big to finish in one sitting | holds a slot *and* makes the scheduler skip its runs, freezing the queue |

Boards do not start out in good shape. On one real project, of 100 open issues
**71 had an empty description** and **92 carried no label** — none of them
executable by anything but a human. That is the baseline this skill exists to beat.

## Parameters

Read from `.loop-on-issue/config.json`, overridden by the prompt.

| Parameter | Config key | Default | Notes |
|---|---|---|---|
| assignee | `assignee` | — | the swarm filters on it; an unassigned issue is invisible to it |
| repo | `repo` | from git remotes | `owner/name`, or a nested GitLab path |
| queue label | `queue_label` | `loop` | must match what the swarm scans for |
| base branch | `base_branch` | `origin/main` | what "already built" is judged against |
| template language | `template_lang` | `en` | only for the bundled fallback |

Always show the resolved `assignee` in the draft. Guessing it wrong is silent: the
issue is created, looks fine on the board, and is never picked up.

## Intake

Four ways in, converging on one requirement statement before you decompose.

| Input | Handling |
|---|---|
| free text in the prompt | ask about the gaps that would otherwise become `[PAUSED]`, and only those |
| a spec / PRD path | read it; an unresolved TBD in the doc is pause-risk, not scope |
| an existing epic issue | read the body *and* the comment thread — decisions live there; children get `--epic <n>`, and the parent never carries the queue label |
| this conversation | decompose what was just designed; no separate artifact to read |
| an intake issue from chat | a requirement raised in the team's group and approved; see below |

## Invoked from a chat intake

When a requirement arrives through the DingTalk channel it is filed as an **intake
issue** first — unqueued, so the swarm cannot claim it — and decomposition starts
only after the approver has released it. You will be pointed at that issue number.

Read three things before anything else, and treat them as one document:

1. **The requirement, verbatim**, in the issue body. It is the source of scope;
   nothing outside it gets built.
2. **The approval comment.** An approval note carries the *same weight as the
   requirement itself* — "同意 712 注意别动定价页" narrows the scope as surely as the
   original sentence did, and a slice that ignores it will be rejected in review.
3. **The rest of the thread**, where clarifications land.

Then:

- Pass `--epic <intake id>` on every slice, so each one links back and the
  requirement stays readable as a unit.
- Confirm the draft through `loop ask --id <intake id>` rather than waiting at a
  keyboard nobody is sitting at. Ask **one** question with the slice titles as
  options plus an explicit "these are wrong, let me re-explain" — a person on
  their phone can answer that; they cannot review six bodies there.
- When the slices exist, comment the list on the intake issue and transition it to
  `[FINISHED]`. The person who raised the requirement reads that comment, so name
  each slice and link it.

Refuse to decompose an intake issue that is not approved — no approval comment, or
still `[PAUSED]`. The gate exists because filing queued issues *is* the trigger for
unattended code changes, and being helpful past it defeats the only control there
is.

## Recon: write the delta, not the feature

**Read the code before you write a slice.** Requirements are almost always
*partly* built, and the issue's value is naming precisely what is missing.

The test is not whether the word appears in the codebase — it is whether a user
can do the thing end to end. Both of these real examples failed in the middle
rather than at either end:

- An issue asked to surface usage detail in a UI. The full payload was **already
  in frontend memory**; only an outlet was missing. A session spent its whole
  triage-and-plan cycle rediscovering that.
- "Add run cancel" looked unbuilt and was mostly built: the service method wrote
  the cancelled state and released the lease, the endpoint existed, and the admin
  UI already rendered cancel buttons. What was actually missing was narrow — the
  frontend hook had an `onSuccess` and no `onError`, so a 409 was swallowed
  silently.

Grep the surface, not the obvious file: the page *and* its hook, the route *and*
the service. A slice that names a real `file:line` is one a session can start on
immediately; a slice that restates the feature request makes it redo your work.

**Settle questions here, not in a slot.** Recon is free — no claim, no worktree,
no session. If a slice's first task would be "find out whether X is true", find
out now. An issue whose acceptance criteria could be satisfied by writing a
comment is not an issue; it is a question you have not finished answering.

## Sizing: one slice, one sitting

A slice must reach a change request in a single session. This is the constraint
most easily missed, because nothing about a well-written issue *looks* oversized.

A slice that bundles *investigate whether (a) holds* + *investigate (b)* + *fix
whatever turned out true* + *repair the doc drift you noticed* is four tasks with
an unknown total, and the swarm has no way to bill it as anything but one slot.
Split it: the investigation belongs in recon, and each confirmed fix becomes its
own slice.

When work genuinely cannot fit, the first slice ends at a committed, reviewable
point rather than half a session's context — the queue keeps moving and the
remainder is a fresh slice with a real brief.

## Independence: a gate, not a note

Two slices with the queue label may run **at the same time, in different
worktrees, against the same `base_branch`**. So the only real question is: *would
these two change requests conflict?*

If they would, **a sentence in the body does not prevent it.** "Don't touch
`tests/e2e/`, that belongs to the other issue" is advice to a session that cannot
see its sibling. The gate is the label:

```bash
# the slice that can start now
"$LOOP" create --title "…" --label web-admin --body-file -

# the slice that cannot — created, visible, deliberately not queued
"$LOOP" create --title "…" --label web-admin --blocked-by 613 --body-file -
```

`--blocked-by` refuses to attach the queue label at all and stamps the dependency
into the body, so the queue only ever holds startable work. Releasing it is a
human adding the label once the blocker merges.

Prefer slicing so this rarely comes up: cut **vertically** by surface (CLI, admin
UI, API) where each slice owns its own files end to end, rather than horizontally
by layer, where every slice reaches into the shared middle.

## The body: fill in the repository's own template

Do not invent a structure. Ask for the one this repository actually uses — a
session reads those sections positionally, and a human filing an issue in the web
UI sees the same shape:

```bash
"$LOOP" template show issue      # the body; stderr names which layer it came from
```

The bundled default has six slots, and each one earns its place:

| Slot | What goes in it |
|---|---|
| Background | why now, and what a reader needs in order not to misread the rest — one short paragraph |
| Current vs expected | what happens today, with a `file:line` from recon, against what should happen |
| Acceptance criteria | checkable statements; exhaustive enough that "all of these hold" means finished |
| File boundary | the files this slice owns, and what a *sibling* slice owns |
| Out of scope | work a session would otherwise reasonably absorb |
| Verification | the real command, runnable as written — take it from `verify_command` |

Keep each one short. This is a brief, not a design doc; the session does its own
planning.

Omit acceptance criteria and the issue is done when the session decides it is. One
real issue finished with *"e2e case not actually run, rendering not eyeballed"* —
which is exactly what "done" means when nobody wrote it down.

If the repository's template has different sections, fill in *those* and keep the
substance: whatever the headings are called, the session still needs the delta,
the definition of done, and the command that proves it.

State a **`[PAUSED]` trigger** in any slice with a judgement call in it: name the
decision and say to stop and ask rather than pick. A session hitting one calls
`loop ask`, which reaches a human in chat and falls back to the issue; pausing on a
flagged question costs one interval at worst, and guessing wrong costs the whole
slot and the review.

## Labels and assignee

`$LOOP` validates both against the project before writing, and this is not
bookkeeping — **both forges create a label on first use.** `--label web_admin`
does not fail; it adds a new project label and the issue silently drops out of
every board filter built on `web-admin`. An unresolvable assignee is dropped the
same way, leaving an issue the swarm can never see.

```bash
"$LOOP" labels          # the only legal vocabulary; marks the queue label
```

Pick from what exists: the queue label (added automatically), at most one type, at
most one area. Never invent one — if nothing fits, say so in the draft and let the
human create the label first. `loop init` creates the queue label and nothing
else, deliberately.

The type label is an instruction, not a filing category: the swarm reads a bug
label as "start from a red test that reproduces this", and a `runner::codex` label
routes the issue to a different agent.

## Draft, then create

Render every slice first — title, labels, assignee, queued or held, and the full
body — and wait. A misread requirement costs a whole session, and you are at the
keyboard; this checkpoint is nearly free. `--dry-run` renders exactly what would
be sent, validating labels and assignee without writing:

```bash
"$LOOP" create --title "…" --label web-admin --body-file draft.md --dry-run
```

Exit codes: **0** success, **2** precondition not met (nothing was written), **1**
a real error.

On approval, create in dependency order so `--blocked-by` can reference real
numbers. Then report each issue's URL, whether it is queued or held, and — for
held ones — what has to merge before a human adds the label.

Two things to raise in the draft rather than decide alone: a slice you could not
ground in code because the area was unfamiliar, and a requirement you chose **not**
to file. Something deliberately left out is invisible on the board, so the draft is
the only moment anyone can disagree.
