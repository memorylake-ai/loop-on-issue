---
description: Check whether this repo and machine are ready for the loop queue (GitHub or GitLab)
argument-hint: "[extra context, e.g. a repo path]"
---

Invoke the `loop-doctor` skill and run its status check against this repository.

$ARGUMENTS

Report the blocking problems first, each with the exact fix command, then the
warnings. Do not paraphrase the fix lines — they are written to be pasted. If
everything is clear, say so in one line and name the next step.
