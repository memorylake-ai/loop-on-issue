---
description: Set up this repo for the loop queue — config, issue/PR templates, queue label
argument-hint: "[--lang zh] [--force] [any preferences]"
---

Invoke the `loop-doctor` skill and walk this repository through setup.

$ARGUMENTS

Show me the plan before applying anything — it writes files I will commit and a
label everyone on the project will see. Then help me fill in the two values that
cannot be guessed: `assignee`, and `verify_command` (propose one from this repo's
own CONTRIBUTING/package.json/Makefile/CI rather than asking an open question).

Do not run `gh auth login` or `glab auth login` for me; print them if needed.
