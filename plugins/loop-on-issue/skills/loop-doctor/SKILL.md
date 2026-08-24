---
name: loop-doctor
description: "Check and bootstrap a repository for the loop-on-issue workflow on GitHub or GitLab: detect which forge the origin points at, verify that gh or glab is installed and authenticated with sufficient token scopes, confirm write access, create the queue label, and scaffold issue and pull/merge request templates (defaults provided, fully customisable). Walks a human through installing and authorising whatever is missing. Use this skill whenever someone wants to check the loop setup, run status or init, find out why the queue looks empty, set up a new repository for the swarm, install or log into gh/glab, or create issue and PR/MR templates — including phrasings like 'loop status', 'loop init', '检查一下环境', 'gh 装了吗', '初始化这个仓库', '为什么队列是空的', 'set up the loop queue here', 'create an issue template', '搞个 MR 模板'."
---

# Loop Doctor

Answer "is this machine and this repository actually ready?" before anything runs
unattended, and fix what is not.

Throughout, `$LOOP` is the plugin's CLI. Resolve it once at the start of the
session:

```bash
LOOP=$(command -v loop 2>/dev/null || true)
[ -n "$LOOP" ] || LOOP="${CLAUDE_PLUGIN_ROOT:-${CODEX_PLUGIN_ROOT:-}}/scripts/loop"
[ -x "$LOOP" ] || LOOP=$(find ~/.claude/plugins ~/.codex/plugins -path '*loop-on-issue*/scripts/loop' 2>/dev/null | head -1)
```

If none of those resolve, ask the human where the plugin is installed rather than
guessing — every other command in this workflow goes through it.

## Why a doctor at all

A misconfigured queue does not announce itself. It looks *exactly* like a healthy
idle one:

| What is actually wrong | What a human sees |
|---|---|
| the queue label does not exist | every scan returns nothing; "the swarm isn't picking anything up" |
| the assignee is misspelled | issues are created, look fine on the board, and are never claimed |
| the token lacks `repo` | reads succeed, the first title write 403s hours into a run |
| no issue template | issues arrive without acceptance criteria and finish when the session decides they have |

Each is one command to check and one to fix, and none is discoverable from the
symptom. That is the whole argument for running this first.

## Status

```bash
"$LOOP" doctor          # human-readable
"$LOOP" doctor --json   # for a script or another agent
```

Exit **0** means nothing is broken. Exit **2** means at least one blocking
problem. Warnings print but never block — an unset `verify_command` is worth
knowing about and is not a reason to refuse to start.

Report the ✗ lines first, each with its fix, then the `!` lines. Do not
paraphrase the fix commands: they are written to be pasted.

### What the checks mean when they fail

**`gh`/`glab` not installed.** The forge is reached only through its CLI, so
nothing works without it. The fix line is platform-correct already
(`brew install gh` on macOS).

**Not authenticated.** Run the printed `… auth login --hostname <host>` yourself
— **do not run it for them.** It is interactive, it touches credentials, and on
some setups it opens a browser. Offer to re-run `doctor` once they say they are
done.

**Token scopes.** GitHub only, and the one people hit: a token with `gist` and
`read:org` but no `repo` reads everything and writes nothing, so the failure
lands hours later on the first title change. `gh auth refresh -h <host> -s repo,read:org`.

**Repository access.** Read-only access means the swarm can claim nothing. Either
get write access, or point `repo` at a fork.

**Queue label missing.** This is the one label the tooling will create for you —
see the boundary below.

**Base branch does not resolve.** Usually just a missing `git fetch`. Worktrees
branch from this ref, so nothing can start until it exists.

## Init

`init` plans first and writes nothing until told to:

```bash
"$LOOP" init                      # show the plan
"$LOOP" init --yes                # apply it
"$LOOP" init --yes --lang zh      # Chinese template set
"$LOOP" init --yes --force        # overwrite templates that already exist
```

Show the human the plan before applying it. It touches their repository — files
that will be committed and a label everyone on the project will see.

It creates:

- `.loop-on-issue/config.json`, with every key written out explicitly so the file
  documents its own vocabulary
- the issue template, at the forge's own location
- the pull request / merge request template, likewise
- the queue label, if missing

Then set the two things `init` cannot guess, and say so plainly rather than
leaving them at `null`:

- **`assignee`** — whose queue this is. The swarm filters on it; nothing else can
  stand in for it.
- **`verify_command`** — this repository's real test command. Without it a session
  invents a plausible-looking one and can report green from a run that tested
  nothing. Find it in `CONTRIBUTING.md`, `package.json`, `Makefile`, or CI config
  and propose it rather than asking an open question.

### Two boundaries init does not cross

**It creates exactly one label: the queue label.** Every other unknown label stays
a hard failure with close matches named. Both forges create a label on first use —
`--label web_admin` does not fail on the typo, it adds a new label and the issue
drops out of every board filter built on `web-admin`, silently. That is the entire
reason the create path validates labels, and init's convenience must not dilute
it. If a label is missing, a human creates it deliberately.

**It never runs `auth login`.** Print the command; let them run it.

## Templates

Three layers, most specific first. `"$LOOP" template show issue` prints the one
that actually applies and, on stderr, which layer it came from.

1. `.loop-on-issue/templates/{issue,pr}.md` — an explicit override, for a repo
   that wants agents to follow something other than what its humans see
2. the forge's own location — `.github/ISSUE_TEMPLATE/loop-task.md` and
   `.github/pull_request_template.md`, or `.gitlab/issue_templates/loop-task.md`
   and `.gitlab/merge_request_templates/loop.md`
3. the templates bundled with the plugin

Layer 2 is where `init` writes, deliberately: a template worth having is one the
person opening an issue in the web UI gets too. A template only the agent knows
about drifts from reality within a week.

**Customising is expected.** Edit the scaffolded file in place — it is a normal
file in their repository. The only structural requirement is that the issue
template keep a section asking what must be true for the work to be finished;
`doctor` warns when it cannot find one, because without it an issue is done when
the session decides it is.

## Configuration reference

`.loop-on-issue/config.json`, read by every command:

| Key | Meaning |
|---|---|
| `forge`, `repo` | `auto` works from the git remotes; set both to override |
| `queue_label` | the label that makes an issue startable |
| `assignee` | whose queue this is — **required in practice** |
| `base_branch`, `push_remote`, `target_remote` | where worktrees branch from and where change requests go |
| `runner` | `claude` or `codex` |
| `max_parallel`, `session_timeout` | concurrency, and the runaway-process bound |
| `worktree_dir` | where per-issue worktrees live |
| `template_lang` | `en` or `zh`, for the bundled fallbacks |
| `verify_command` | this repo's real test command — **set it** |
| `env_files` | gitignored files a worktree needs; `git worktree add` will not carry them over |
| `escalation_command` | optional faster-than-the-next-run channel to a human; null means issue comments only |

An unrecognised key is reported and ignored, so a config written by a newer
version of the plugin does not stop an older one.

## Reporting

End with what is left for the human, in this order: blocking problems with their
fix commands, then the values only they can supply (`assignee`, `verify_command`),
then anything `init` deliberately declined to do and why. If everything is clear,
say so in one line and name the next step — filing issues with
`loop-issue-creator`, or draining the queue with `loop-issue-swarm`.
