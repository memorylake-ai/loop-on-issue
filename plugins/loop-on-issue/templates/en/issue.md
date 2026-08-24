<!-- loop-on-issue:template -->

### Background

Why this matters now, and what a reader needs in order not to misread the rest.
One short paragraph.

### Current vs expected

What happens today — with a `file:line` from actually reading the code — against
what should happen instead. A slice that names a real location is one a session
can start on immediately; a slice that restates the feature request makes it redo
that work first.

### Acceptance criteria

1. Checkable statements, so a session can tell done from not-done without asking.
2. Exhaustive enough that "all of these hold" genuinely means finished.

### File boundary

The files this slice owns. Name what a *sibling* slice owns too, so a session
running at the same time in another worktree does not wander into it.

### Out of scope

Work a session would otherwise reasonably absorb. This is what keeps the slice
inside a single sitting.

### Verification

The real command, runnable exactly as written:

```sh
# e.g. npm test -- path/to/suite
```
