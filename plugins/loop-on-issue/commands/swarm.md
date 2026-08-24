---
description: Run one pass of the issue swarm — reconcile, claim, develop, submit
argument-hint: "[assignee] [--label loop] [--max-parallel 2] [--runner claude|codex]"
---

Invoke the `loop-issue-swarm` skill and run one full pass over the queue.

$ARGUMENTS

Start with `loop doctor`; if it exits non-zero, stop and show me what to fix
rather than running against a broken setup. Then work Phase 0 through Phase 3 as
the skill describes, and finish with the report — anything `PAUSED` first, then
everything you retired as `[SKIP]` with reasons.
