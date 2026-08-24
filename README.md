# loop-on-issue

An unattended issue-to-change-request pipeline for **GitHub and GitLab**, packaged
as one plugin for both **Claude Code** and **Codex**. This repository is also the
marketplace you install it from.

Queue an issue, and an agent claims it, develops it in its own git worktree, gets
it reviewed, and opens the pull request or merge request — reporting every state
change on the board so a human can watch, interrupt, or take over at any point.

```
  no prefix ──────► [CLAIMED] ──────► [WORKING] ──────► [FINISHED] ────► human
  (the queue)                          ▲   │   ▲            │            closes
                                       │   │   └────────────┘
                        human replied  │   ▼   review feedback arrived
                                     [PAUSED]

  [CLAIMED] or [WORKING] ──────────► [SKIP]   needs no code change
```

State lives in the issue **title prefix** because both forges only model an issue
as open or closed — which cannot tell an untouched issue from one an agent grabbed
three seconds ago, one blocked on a human, and one awaiting merge. The title shows
up in every issue list, notification email and board card, so humans and agents
read the same state with no extra tooling, and a human can hand-edit a prefix to
redirect the swarm.

## Install

**Claude Code**

```
/plugin marketplace add memorylake-ai/loop-on-issue
/plugin install loop-on-issue@loop-on-issue
```

**Codex**

```sh
codex plugin marketplace add memorylake-ai/loop-on-issue
codex plugin add loop-on-issue@loop-on-issue
```

Requirements: `git`, Python 3.9+ (macOS ships this), and either
[`gh`](https://cli.github.com) or [`glab`](https://gitlab.com/gitlab-org/cli)
depending on where your repo lives. The plugin has **no pip dependencies**.

> Upgrading from the private `loop-issue-swarm` / `loop-issue-creator` /
> `gitlab-mr` skills? Delete those from `~/.claude/skills/` after installing, or
> both copies will trigger. Boards already running them keep working: the agent
> marker is written in the new form and read in both, and session ids for GitLab
> repositories keep their original derivation so issues sitting at `[PAUSED]`
> still resume into their existing context.

## Set up a repository

```
/loop-on-issue:status     # what is missing, with paste-ready fixes
/loop-on-issue:init       # config, templates, queue label
```

Or directly:

```sh
loop doctor
loop init --yes           # add --lang zh for the Chinese template set
```

`doctor` checks the things whose failure looks like an idle queue rather than a
broken one: forge detection, CLI installed, authenticated, **token scopes**,
write access, queue label, assignee resolvable, template source, config validity,
runner binary, base branch, git identity.

`init` writes `.loop-on-issue/config.json`, the issue template and the change
request template — at the forge's own location, so a human opening an issue in the
web UI gets the same one — and creates the queue label. It holds two lines:

- **It creates exactly one label: the queue label.** Every other unknown label
  stays a hard failure with close matches named. Both forges create a label on
  first use, so a typo does not error — it files the work under a label nobody's
  board filters on.
- **It never runs `auth login`.** That is interactive and touches credentials; it
  prints the command instead.

Then set the two values nothing can guess: `assignee` (whose queue this is) and
`verify_command` (this repository's real test command — without it a session
invents a plausible one and can report green from a run that tested nothing).

## Use it

| Command | Skill | What it does |
|---|---|---|
| `/loop-on-issue:status` | `loop-doctor` | is this repo and machine ready |
| `/loop-on-issue:init` | `loop-doctor` | config, templates, queue label |
| `/loop-on-issue:issues` | `loop-issue-creator` | decompose a requirement into executable issues |
| `/loop-on-issue:swarm` | `loop-issue-swarm` | run one pass over the queue |
| `/loop-on-issue:mr` | `forge-mr` | take this branch to a submitted PR/MR |
| `/loop-on-issue:init-dingtalk-bot` | `dingtalk-bot` | set up, operate or switch off the optional chat bot |

Codex has no slash commands; the five skills carry the same logic and activate from
the same phrasings, in English or Chinese.

## The `loop` CLI

Every read and write against the board goes through one CLI, so the title-prefix
edge cases and the agent marker have exactly one implementation.

```sh
loop doctor [--json]                    # readiness
loop init [--yes] [--lang en|zh] [--force]
loop template show|path issue|pr        # which template actually applies, and from where

loop list --label loop --assignee me --json --active-only
loop claim --id 612
loop transition --id 612 --to WORKING --expect CLAIMED
loop skip --id 612 --reason "…"         # a substantive reason is required
loop comment --id 612 --body-file -     # or --pr 88
loop human-reply --id 612               # exit 0 = answered, 2 = still waiting
loop pr-status --id 612                 # aliases: mr-status
loop pr-feedback --id 612               # aliases: mr-feedback
loop session-id --id 612                # the id to resume with
loop session-record --id 612 --session <id> --runner codex
loop labels
loop create --title "…" --body-file - [--blocked-by 613] [--epic 600] [--dry-run]

loop ask --id 612 --question "…" [--option A --option B] [--wait 120]
loop report --json-file -                # {"summary": "...", "notes": {"612": "..."}}
loop dingtalk [status|enable|disable|sweep|serve]
loop repos [list|add|remove|default] …    # repositories the chat bot serves
loop intake [list|sweep] [--id R…]        # requirements awaiting a decision
```

Exit codes are load-bearing: **0** success, **2** precondition not met (a routine
"skip this one"), **1** a real error. It lives at `plugins/loop-on-issue/scripts/loop`;
symlink it onto your `PATH` if you want to use it by hand.

### The two forges, and where they differ

Everything above the forge layer is identical. Three GitHub-specific hazards are
handled underneath:

- **Its issues endpoint returns pull requests too**, so every listing filters
  them out — without that the swarm can claim a pull request as queued work.
- **Review-thread resolution exists only in GraphQL**, so that one read goes
  through `gh api graphql` and degrades to "any unmarked comment newer than our
  last one" rather than failing a run.
- **Attribution has a native answer there.** GitHub's `closes #N` development link
  is asked first and survives a retitle; the `to #N` title convention is the
  fallback. On GitLab the title is the *only* rule, because its related-merge-
  requests listing returns every MR that merely *mentions* the issue — which has
  attributed a third issue's work to two others and made their review feedback
  unreadable.

## Asking a human

An unattended run stops at decisions only a person can make. `loop ask` writes the
question to the issue — which always works and is what the next scheduled run
reads — and, when a chat channel is configured, pushes it to the group in the same
step so somebody holding their phone can settle it in minutes instead of an
interval.

```sh
loop ask --id 612 --question "Which writer is authoritative?"   --option "The streaming one" --option "The batch one" --option "Park it"
```

Answers come back **as issue comments**, wherever they were given. That is the
whole design: an answer typed in chat is mirrored onto the issue, so it survives
the machine, the process, and the person answering from a laptop instead of a
phone — and a human who never opens chat can simply reply on the issue.

**`AskUserQuestion` is intercepted.** A headless session has no UI to answer it, so
a `PreToolUse` hook relays the question through `loop ask`, waits a short bounded
window, and feeds the answer back to the model. Nothing answers within the window
and the session is told to pause, which is where it would have ended up anyway.
Outside a loop session the hook does nothing and the tool behaves normally.

## Chat channel (DingTalk) — optional

Off unless you set it up, and switchable off again at any time:

```
/loop-on-issue:init-dingtalk-bot     # guided setup
loop dingtalk disable                # and back on with `enable`
```

Disabled, the plugin behaves exactly as one that never had the feature — the
`AskUserQuestion` hook becomes a no-op and nothing is sent. Questions still land on
issues and the swarm still reads answers from them; that path never depended on
chat. Setup, operation and troubleshooting live in the `dingtalk-bot` skill and in
[`plugins/loop-on-issue/dingtalk/README.md`](plugins/loop-on-issue/dingtalk/README.md).

Three jobs, and nothing else:

1. **Relay questions**, as above.
2. **Report to both surfaces** — a run's summary to the conversation, and a note on
   each issue the run touched, each written to be read on its own.
3. **Take requirements.** Anyone may raise one by sending a plain sentence. It is
   held **locally**, outside any repository, until the one configured approver
   releases it — and only then does an agent decompose it into queued issues.

**Nothing anyone says reaches a repository until the approver has said yes.** That
gate is the only thing standing between a chat message and unattended code changes,
which is why it is one named person, and why `/dev <issue>` — putting an agent on
an existing issue right now — sits behind the same gate. Answering a question is
open to anyone in an allow-listed conversation and records who answered, because
that steers work a human already framed.

Only a **quote-reply** answers a question; a plain sentence is always a
requirement. There is no guessing between the two.

One bot can serve several repositories. The registry is machine-level, because when
a request arrives nobody has yet decided which one it belongs to, and each entry
needs a local checkout as well as a project path — that is where the agent runs:

```sh
loop repos add loop  memorylake-ai/loop-on-issue  ~/github/loop-on-issue
loop repos add bloom org/bloom                    ~/github/bloom
loop repos default loop
```

A requirement goes to the default, and the approver redirects it in the same breath
as approving (`同意 R20260824-01 bloom`). With several registered and no default, a
bare requirement is refused rather than filed somewhere arbitrary.

Approved work is drained by a **single serial worker** — two agents in one checkout
fight over git state — and every job is durable, so a listener that dies mid-queue
resumes on restart. The listener is the only component with a pip dependency
(`dingtalk-stream`) and keeps its own virtualenv.

## Runners

`claude` and `codex` both work, chosen by `--runner`, by a `runner::codex` label
on an individual issue, or by config.

They resume differently, and it matters. `claude -p` can be told its session id up
front, so the id is **derived** from the issue's identity and any later run
recomputes it with nothing stored. `codex exec` assigns its own thread id, so the
first run captures it from the `--json` event stream and records it in a marker
comment; deleting that comment loses the ability to resume.

## Configuration

`.loop-on-issue/config.json` — written in full by `init`, so the file documents
its own vocabulary.

| Key | Default | Meaning |
|---|---|---|
| `forge`, `repo` | `auto`, `null` | overrides for detection |
| `queue_label` | `loop` | the label that makes an issue startable |
| `assignee` | `null` | whose queue this is — **set it** |
| `base_branch` | `origin/main` | what worktrees branch from, and the target |
| `push_remote`, `target_remote` | `origin`, `auto` | fork workflow support |
| `runner` | `claude` | `claude` or `codex` |
| `max_parallel` | `2` | concurrent issues; each slot runs a full build |
| `session_timeout` | `43200` | a runaway detector, not a work budget |
| `worktree_dir` | `.worktrees` | per-issue checkouts |
| `template_lang` | `en` | `en` or `zh`, for the bundled fallbacks |
| `verify_command` | `null` | this repo's real test command — **set it** |
| `ask_wait` | `0` | seconds `loop ask` waits; 0 keeps sessions from blocking on a human |
| `intake_ttl` | `604800` | how long a chat requirement waits for a decision; approved ones never expire |
| `env_files` | `[".env"]` | gitignored files to copy into each worktree |
| `escalation_command` | `null` | a channel other than the built-in one — Slack, Feishu, a pager |

Unrecognised keys are reported and ignored, so a config from a newer version does
not stop an older one.

## Templates

Resolution order, reported by `loop template show`:

1. `.loop-on-issue/templates/{issue,pr}.md` — explicit override
2. the forge's own location — `.github/ISSUE_TEMPLATE/loop-task.md` and
   `.github/pull_request_template.md`, or `.gitlab/issue_templates/loop-task.md`
   and `.gitlab/merge_request_templates/loop.md`
3. the bundled defaults, in English or Chinese

Layer 2 is where `init` writes and where customisation belongs — edit the file, it
is yours. The one structural expectation is that the issue template keeps a
section asking what must hold for the work to be finished; `doctor` warns when it
cannot find one, because without it an issue is done when the session decides it
is.

## Safety boundaries

An unattended loop compounds mistakes across runs, so these are hard lines the
skills do not cross:

- **Never merge a change request, and never close an issue.** `FINISHED` hands off
  to a human; an agent that merges its own work has no review gate.
- **Never touch an issue outside the filter** — wrong label, wrong assignee, or a
  prefix it did not write.
- **Never push to the target remote** when it differs from `push_remote`, and never
  force-push a branch it did not create.
- **All code changes stay inside the worktree directory.** The main checkout may
  hold your uncommitted work.
- **Every agent comment goes through the CLI**, so it carries the invisible marker.
  Without it, an agent's own note later reads as a human reply and wakes an issue
  nobody answered.
- **Nothing from chat reaches a repository unapproved.** A requirement is held
  locally until one named approver releases it, and putting an agent on an existing
  issue sits behind the same gate.
- **Credentials live outside the repository**, in `~/.loop-on-issue/dingtalk.env`
  at mode 600.

## Development

```sh
plugins/loop-on-issue/run-tests.sh
PYTHON=/usr/bin/python3 plugins/loop-on-issue/run-tests.sh   # the 3.9 floor
```

Standard library `unittest`, no pip install. The forge backends are tested against
a real fake `gh`/`glab` installed on `PATH` rather than mocked in Python, because
the behaviour worth testing is command-line construction and output parsing.

## Licence

MIT.
