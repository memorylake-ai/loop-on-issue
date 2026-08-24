# loop-on-issue — Claude Code + Codex plugin marketplace

**Date:** 2026-08-24
**Status:** approved, ready to implement

## Problem

Three private skills — `loop-issue-swarm`, `loop-issue-creator`, `gitlab-mr` — implement a
working unattended issue→MR pipeline, but they are undistributable and GitLab-only:

- All GitLab access is hardcoded to `glab` + GitLab REST v4. There is no GitHub path.
- Two scripts duplicate `gl()`, `enc()`, `default_repo()`.
- The skills embed one machine's specifics: a LoopCue `PYTHONHOME` workaround, the absolute
  path `/Users/donal/zbyte/zootopia/.venv/bin/python`, a hardcoded `PYTHONPATH`, and
  `loopcue` as the escalation channel.
- There is no way to tell, before a run starts, whether the machine is even set up for it.

## Goal

Ship the three skills as **one plugin** from a repo that is **itself a marketplace** for both
Claude Code and Codex, working against **GitHub (`gh`) and GitLab (`glab`)** with the same
semantics, plus `status` / `init` commands that diagnose and bootstrap a repo.

## Decisions

| Decision | Choice |
|---|---|
| Plugin granularity | Single plugin `loop-on-issue` (3 ported skills + 1 new doctor skill) |
| State machine on GitHub | Same `[WORKING]`-style title prefixes as GitLab |
| Template location | forge-native files, plugin-bundled defaults as fallback |
| Codex depth | swarm supports `--runner codex` alongside `claude` |
| PR attribution on GitHub | native development link first, `to #N` title as fallback |
| Language | English default, `--lang zh` template set, bilingual trigger phrases |
| Tests | stdlib `unittest`, zero pip dependencies |
| Issue template format | Markdown, same shape on both forges |

## Architecture

```
loop-on-issue/                              repo root IS the marketplace
├── .claude-plugin/marketplace.json         Claude Code marketplace manifest
├── .agents/plugins/marketplace.json        Codex marketplace manifest
├── plugins/loop-on-issue/
│   ├── .claude-plugin/plugin.json
│   ├── .codex-plugin/plugin.json
│   ├── commands/                           Claude-only thin wrappers over skills
│   ├── skills/
│   │   ├── loop-doctor/                    status + init (new)
│   │   ├── loop-issue-swarm/               ported
│   │   ├── loop-issue-creator/             ported
│   │   └── forge-mr/                       ported from gitlab-mr
│   ├── scripts/
│   │   ├── loop                            POSIX sh entrypoint
│   │   ├── loop_cli.py                     argparse front end
│   │   └── loopkit/                        stdlib-only library, Python 3.9+
│   ├── templates/{en,zh}/{issue,pr}.md
│   └── tests/
└── README.md
```

Codex has no `commands/` concept, so **all `status`/`init` logic lives in the `loop-doctor`
skill**; the Claude slash commands are one-line wrappers that invoke it. Both runtimes get
identical behaviour from one source.

### Python version floor

`loopkit` targets **Python 3.9** and imports nothing outside the standard library. macOS ships
`/usr/bin/python3` as 3.9.6; a plugin that cannot run on a stock Mac is not distributable.
This is why config is JSON, not TOML (`tomllib` is 3.11+).

## Components

### `loopkit.state` — the state machine (pure)

Unchanged semantics from `issue_state.py`: `CLAIMED → WORKING → PAUSED → FINISHED`, with
`SKIP` as the dormant terminal bucket, encoded as a title prefix because both forges only
offer open/closed. `split_state` / `compose` stay pure functions and are the main unit-test
surface: stacked prefixes from an interrupted transition, unrelated prefixes like `[TEST]`,
literal brackets in a title.

**Agent marker.** Writes `<!-- loop-on-issue:agent -->`. Reads accept **both** that and the
legacy `<!-- loop-swarm-agent -->`, so boards already running the private skills do not lose
their "has a human replied" anchor on upgrade.

### `loopkit.remotes` — forge detection (pure + one probe)

1. `config.forge` when not `auto`
2. `git remote get-url` for `upstream`, then `origin`; parse host and `owner/name`
3. host unrecognised → probe `gh repo view --json nameWithOwner`, then `glab repo view`, in
   the working directory. This is what handles SSH host aliases
   (`git@github-work:org/repo.git`) and self-hosted instances.
4. still unknown → hard error naming `forge` in config as the fix

### `loopkit.forge` + `loopkit.gh` + `loopkit.gl`

One interface, two implementations, both driving the already-authenticated CLI rather than a
raw token — the same reason the original scripts used `glab api`.

```
name, cr_word, cr_sigil
list_issues / get_issue / set_issue_title / create_issue
list_labels / resolve_assignee
list_issue_comments / add_issue_comment / add_cr_comment
find_cr_for_issue / unattributed_crs / cr_review_threads / cr_comments
```

Three GitHub-specific hazards the implementation must handle:

| Hazard | Handling |
|---|---|
| `GET /repos/{o}/{r}/issues` returns pull requests too | drop entries carrying a `pull_request` key — without this the swarm can claim a PR as an issue |
| REST review comments carry no `isResolved` | GraphQL `pullRequest.reviewThreads{isResolved}`; on GraphQL failure degrade to "any unmarked comment newer than our last marker is feedback" |
| attributing a PR to an issue | GraphQL `issue.closedByPullRequestsReferences` (the native `closes #N` development link) first, `to #N` title match as fallback |

GitLab keeps `to #<iid>` title matching as the **only** attribution rule. That is deliberate
and documented in the original: `related_merge_requests` returns every MR that merely
*mentions* the issue, which on 2026-08-19 attributed a stranger's MR to #630 and #631 and made
review feedback unreadable. A wrong-but-confident attribution is silent and lasting; "no MR"
merely pauses the issue in front of a human.

### `loopkit.config`

`.loop-on-issue/config.json` at repo root. `init` writes every key explicitly so the file
documents itself.

```
forge              auto | github | gitlab
repo               owner/name, or null to derive from remotes
queue_label        loop
assignee           null
base_branch        origin/main
push_remote        origin
target_remote      auto (upstream if present, else origin)
runner             claude | codex
max_parallel       2
session_timeout    43200
worktree_dir       .worktrees
template_lang      en | zh
verify_command     null   — the repo's real test command, injected into each session brief
env_files          [".env"] — gitignored files copied into each worktree
escalation_command null   — e.g. a loopcue invocation; null means issue comments only
```

`verify_command`, `env_files` and `escalation_command` are what remove the last of the
machine-specific content from the skills.

### `loopkit.templates`

Resolution order, reported by `loop template show`:

1. `.loop-on-issue/templates/{issue,pr}.md`
2. forge-native — GitHub `.github/ISSUE_TEMPLATE/loop-task.md` and
   `.github/pull_request_template.md`; GitLab `.gitlab/issue_templates/loop-task.md` and
   `.gitlab/merge_request_templates/loop.md` (each with the usual casing/location variants)
3. bundled `templates/{en,zh}/`

Default issue template carries the six slots the swarm reads positionally — Background,
Current vs Expected, Acceptance criteria, File boundary, Out of scope, Verification — plus a
`<!-- loop-on-issue:template -->` marker (matching the `loop-on-issue:agent` comment marker)
so tooling can tell a loop template from a generic one.
Default PR template is Motivation / Modifications / `closes #N`.

### `loopkit.runner`

- **claude**: session id derived as `uuid5`, recomputable with no stored state. GitLab repos
  keep the legacy key `loop-issue://{repo}#{n}` so issues already `[PAUSED]` on an existing
  board still resume; GitHub uses a forge-qualified key.
- **codex**: `codex exec` cannot be given a session id at start. Start with `--json`, take the
  thread id from the first event that carries one, and record it via `loop session record`,
  which posts it inside a marker comment
  (`<!-- loop-on-issue:agent session=<uuid> runner=codex -->`). Resume reads the newest such
  comment. No recorded id means no session: start fresh and say so in a comment.
- Runner selection: `--runner` > a `runner::codex` / `runner:codex` label on the issue >
  config > `claude`.
- Subprocess bounding stays `perl -e 'alarm shift @ARGV; exec @ARGV'` (macOS ships neither
  `timeout` nor `gtimeout`), exit 142 on expiry.

### `loopkit.doctor` and `init`

`loop doctor` emits one ok/warn/fail row per check with a paste-ready fix:

git repo and remotes · forge detected · `gh`/`glab` installed · authenticated · token scopes ·
repo reachable with write access · queue label exists · assignee resolvable · issue template
source · PR template source · config valid · runner binary present · `base_branch` resolvable ·
git identity set.

Exit codes: **0** when nothing is *blocking*, **2** when at least one check failed. Warnings
print but never block — an unset `verify_command` is worth knowing about and is not a reason
to refuse to start.

`loop init` is doctor plus guided remediation, and has two hard boundaries:

- **It creates exactly one label: the queue label.** Every other unknown label stays a hard
  failure with close matches named. Both forges create labels on first use, so a typo silently
  files work under a label no board filters on — that silent failure is the entire reason
  `create_issue.py` validates, and `init`'s convenience must not dilute it.
- **It never runs `gh auth login` / `glab auth login`**, only prints them. Those are
  interactive and touch credentials.

Templates are written for the detected forge and never overwritten without `--force`.

### CLI surface

```
loop doctor [--json]
loop init [--yes] [--lang en|zh] [--force]
loop template show|path issue|pr
loop list | claim | transition | skip | comment | human-reply
loop pr-status | pr-feedback            (aliases mr-status | mr-feedback)
loop session [--runner] [--generation] | loop session record
loop labels | create
```

Exit codes **0 / 2 / 1** throughout: 2 is a routine "precondition not met, move on" that the
swarm branches on heavily, not an error.

## Skills

| Skill | Origin | Change |
|---|---|---|
| `loop-doctor` | new | drives `loop doctor` / `loop init`, walks a human through install, auth, labels, templates and config |
| `loop-issue-swarm` | ported | forge-neutral wording, `$S` → `loop`, params read from config, runner selection, machine specifics removed |
| `loop-issue-creator` | ported | same, and template-aware when composing bodies |
| `forge-mr` | from `gitlab-mr` | branches on forge (`gh pr create` vs `glab mr create`), template-driven description, same label discipline |

## Testing

`tests/` under stdlib `unittest`, run by `python3 -m unittest discover`:

- prefix parse/compose incl. stacked, unrelated, and bracket-literal titles
- remote URL → host → forge, SSH aliases, self-hosted, config override
- config defaults and merge
- template resolution order
- both forge implementations against a fake `gh` / `glab` placed on `PATH` replaying recorded
  fixtures: request construction, pull requests excluded from issue listings, exit codes
- doctor outcomes for each simulated CLI state

## Out of scope

- Merging a PR/MR or closing an issue. `FINISHED` hands off to a human; an agent that merges
  its own work has no review gate.
- Migrating existing boards. The dual marker read is the only compatibility affordance.
- Forges other than GitHub and GitLab.
- Removing the user's existing `~/.claude/skills/` copies. The README says to delete them to
  avoid double-triggering; the plugin does not touch them.
