# Working in this repository

`loop-on-issue` coordinates development through the issue tracker. The board is
where work is described, claimed, questioned, reported on and handed back — so
agents and people read the same thing, and no coordination lives anywhere a human
cannot see it. Keep changes faithful to that: if a fact matters to somebody
watching, it belongs on the board rather than in a log.

## How changes land

**Branch, then pull request. Never push to `main`.**

```sh
git checkout -b <type>/<topic>
# ... work ...
```

Then use the `forge-mr` skill, which handles the rest: labels chosen from what the
project actually defines, a linked issue created with `--no-queue`, the branch
pushed, and the change request opened from this repository's own template with the
load-bearing `to #<id>` title and `closes #<id>` line.

Submitting is where it stops. Merging is a human's call.

> Eight commits sit on `main` without a pull request, from before this was written.
> They are staying; the convention starts after them.

## Verification

```sh
plugins/loop-on-issue/run-tests.sh
PYTHON=/usr/bin/python3 plugins/loop-on-issue/run-tests.sh
```

**Both interpreters, every time.** `loopkit` targets Python 3.9 because macOS ships
that as `/usr/bin/python3`, and a plugin that cannot run on a stock Mac is not
distributable. That floor is also why configuration is JSON rather than TOML.

Standard library only, no pip install. The one exception is the DingTalk listener,
which keeps its own virtualenv under `plugins/loop-on-issue/dingtalk/`.

## What tests are for here

Most of the defects found in this repository were invisible to review and obvious
in a real run: a permission mode that denied every command an agent was told to
use, a clean exit taken as evidence of work, a derived session id that collided
with itself on retry, markdown that renders as one paragraph on a phone.

So when a bug is found, the test that lands with the fix should describe **the
failure**, not the function. `test_a_decomposition_that_filed_no_issues_produced_nothing`
says what went wrong; `test_produced_nothing` does not, and the next person will
not know what it is protecting.

Prefer testing what a person or another program actually sees — a rendered reply, a
constructed command line, an exit code — over internal shape. The forge backends
run against a real fake `gh`/`glab` on `PATH` for exactly this reason.

## Things that look like taste and are not

- **Exit codes 0 / 2 / 1.** `2` means "precondition not met, move on" and the
  workflow branches on it heavily. Turning a lost claim race into an error would
  escalate something routine.
- **The agent marker is read in two forms and written in one.** Boards running the
  private predecessors must keep waking up.
- **`to #<id>` in a change request title, `closes #<id>` in its description.**
  These are how an issue finds its own work. Dropping either makes later runs
  report the issue as having none.
- **A new issue carries no state prefix.** `claim` refuses a prefixed title, so one
  created with a prefix can never be picked up.
