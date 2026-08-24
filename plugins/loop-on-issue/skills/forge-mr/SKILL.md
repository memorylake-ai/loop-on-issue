---
name: forge-mr
description: "End-to-end pull request / merge request workflow for GitHub and GitLab: detect which forge the repo is on, commit outstanding changes, rebase onto the target, auto-create a linked issue if none was given, label both, push, and open the change request from the repository's own template. Uses gh or glab accordingly. Trigger this skill when the user wants to submit a PR or MR, finish a branch, push and open a review, or says anything like 'submit MR', 'create PR', 'open a pull request', 'push this branch', '提个 MR', '开个 PR', or wants to wrap up a feature branch."
---

# Forge Change Request Workflow

One workflow from a working branch to a submitted change request, on either forge.
A **change request** is a pull request on GitHub and a merge request on GitLab;
this skill uses the right word and the right CLI once it knows which it is on.

Throughout, `$LOOP` is the plugin's CLI:

```bash
LOOP=$(command -v loop 2>/dev/null || true)
[ -n "$LOOP" ] || LOOP="${CLAUDE_PLUGIN_ROOT:-${CODEX_PLUGIN_ROOT:-}}/scripts/loop"
[ -x "$LOOP" ] || LOOP=$(find ~/.claude/plugins ~/.codex/plugins -path '*loop-on-issue*/scripts/loop' 2>/dev/null | head -1)
```

## Inputs from context

Gather these from the conversation. Only `issue_id` might need asking — the rest
have sensible defaults, taken from `.loop-on-issue/config.json` where present.

| Parameter | Required | Default | Notes |
|---|---|---|---|
| `issue_id` | No | auto-created from the commits | If the invoking prompt names one (the swarm always does), **use it** — creating a duplicate splits the discussion |
| `labels` | No | derived in Step 4 | Never empty; see Step 4 |
| `push_remote` | No | `origin` | Where the branch goes |
| `target_remote` | No | `upstream` if it exists, else `origin` | Where the change request lands |
| `target_branch` | No | `main` | On `target_remote` |

## Step 0: Know which forge you are on

```bash
"$LOOP" doctor --json      # .repo.forge, .repo.host, .repo.path
```

Everything downstream branches on this one answer: `gh` or `glab`, "pull request"
or "merge request", `#` or `!` when referring to it in prose. If `doctor` reports
the CLI is missing or unauthenticated, stop and hand the human the fix line — the
rest of this workflow cannot proceed and half-doing it leaves a pushed branch with
no change request.

## Step 1: Detect the target remote

```bash
git remote -v
```

- `upstream` exists → `target_remote` = `upstream` (fork workflow: push to origin,
  the change request targets upstream)
- otherwise → `target_remote` = `origin`
- If neither has the target branch, ask.

```bash
git fetch <target_remote> <target_branch>
```

Keep `target_ref` (e.g. `upstream/main`) for later steps. `push_remote` stays
`origin`.

## Step 2: Check the working tree and commit

```bash
git status --short
```

If there are uncommitted changes: stage the relevant files — prefer naming them
over `git add -A`, which is how a copied credentials file ends up in a commit —
write a conventional-format message (`type(scope): description`), and commit.

Clean tree, skip to Step 3.

## Step 3: Rebase onto the target

```bash
git rebase <target_ref>
```

On conflicts, **pause and tell the user**. Do not auto-resolve. Continue with
`git rebase --continue` once they have.

## Step 4: Pick labels

**Every issue and every change request this skill touches carries labels.** Zero
labels is an incomplete result, not a shortcut.

```bash
"$LOOP" labels          # what the project actually defines
```

Pick **1–3** from that list:

| Slot | Required | How to choose |
|---|---|---|
| Type | Yes — exactly one | Map the branch's conventional-commit type onto the project's actual label name: `feat`→feature/enhancement, `fix`→bug/bugfix, `refactor`→refactor/tech-debt, `docs`→documentation, `test`→test, `perf`→performance, `chore`/`build`/`ci`→chore/infra |
| Scope | If the project defines matching ones | Component or area, from the touched paths in `git diff <target_ref>..HEAD --stat` |
| Priority / status | Only when stated | Apply only if the user or the issue said so. Never infer urgency from your own read of the diff |

Rules:

- **Only use labels the project already defines.** Do not invent names, and do not
  guess at capitalisation or separators — copy the exact string, including scoped
  forms like `type::bug`. Both forges create a label on first use, so a typo does
  not fail: it files the work under a label nobody's board filters on.
- **No match for the type slot?** Pick the closest existing label rather than
  creating one.
- **Project has no labels at all?** Say so in the final report and proceed with
  none — the one case where empty is allowed.
- **Fork workflow:** list labels from the **target** project; that is where both
  the issue and the change request live.

Save the result for Steps 5 and 7.

## Step 5: Resolve or create the issue

**If `issue_id` was given:** use it. Read its labels; if it has some, adopt them
for the change request. If it has none, apply the ones from Step 4.

**If not:** create one from the branch's history against the target.

```bash
git log <target_ref>..HEAD --oneline
git diff <target_ref>..HEAD --stat
"$LOOP" create --title "<title>" --label "<type>" --no-queue --body-file -
```

`--no-queue` matters: this issue documents work that is *already done*. Attaching
the queue label would put it in front of the swarm, which would claim it and spend
a slot rediscovering that the change request already exists.

## Step 6: Push the branch

```bash
git push origin <current_branch> --force-with-lease
```

`--force-with-lease` because Step 3 rewrote history. It only force-pushes if
nobody else has pushed to the same branch.

## Step 7: Open the change request

Build the description from the repository's own template:

```bash
"$LOOP" template show pr
```

**Title format:** `to #<issue_id>: <Meaningful Title>`

**Two things in that output are load-bearing, not decoration:**

- The **`to #<id>` title** is how `pr-status` and `pr-feedback` find this change
  request. On GitLab it is the *only* attribution rule, because the related-merge-
  requests listing returns everything that merely mentions the issue.
- The **`closes #<id>` line** in the description creates GitHub's native
  development link, which is what attribution prefers there and what survives a
  retitle.

Retitle by hand and drop the closing keyword, and later runs report the issue as
having no change request at all.

**Language:** match the resolved template. The bundled default is English, so
titles and descriptions are English even when the conversation, commits and issue
are not — translate the substance into idiomatic English rather than transcribing.
If the repository ships a Chinese template, follow that instead. Code identifiers,
file paths, error strings and proper nouns are preserved as-is either way.

### GitHub

```bash
gh pr create \
  --base <target_branch> \
  --head <current_branch> \
  --title "to #<issue_id>: <Title>" \
  --body "<description with closes #<issue_id>>" \
  --label "<labels>"
```

Fork workflow — add `--repo <target_owner/repo>` and use `--head <fork_owner>:<branch>`.

**GitHub has no per-change-request squash flag at creation.** Squashing is chosen
at merge time (`gh pr merge --squash`) or fixed by a repository setting. Say so in
the report rather than implying the change request is configured to squash.

### GitLab

```bash
glab mr create \
  --source-branch <current_branch> \
  --target-branch <target_branch> \
  --title "to #<issue_id>: <Title>" \
  --description "<description>" \
  --label "<labels>" \
  --squash-before-merge \
  --remove-source-branch \
  --related-issue <issue_id> \
  --no-editor --yes
```

Fork workflow — add `--repo <target_namespace/project>` and
`--head <fork_namespace/project>`.

**Pass `--label` explicitly.** `--copy-issue-labels` alone is not enough: it only
fires when the linked issue already had labels.

### Either forge

If a change request already exists for this branch, **update it** rather than
creating a duplicate — title, description and labels — with `gh pr edit` or
`glab mr update`.

## Step 8: Report

- Issue URL (created or existing) and its labels
- Change request URL and its labels
- Merge mode: squash configured (GitLab) or squash-at-merge-time (GitHub)

If either ended up with no labels, say so explicitly and why — project defines
none, or the user declined. Do not let it pass silently.

**Never merge it.** Submitting is where this skill stops; a change request that
merges itself has no review gate.

## Edge cases

- **Branch is the target branch** — abort immediately, tell the user to switch to
  a feature branch.
- **No commits ahead of the target** — nothing to submit; say so.
- **Rebase conflicts** — pause, report the files, let the user resolve.
- **Change request already exists** — update it instead of duplicating.
- **CLI not authenticated** — `"$LOOP" doctor` prints the exact login command.
  Hand it over; do not run it yourself.
- **Label rejected, or silently doesn't stick** — the name is not in the project's
  list. Re-run `"$LOOP" labels`, copy the exact string, retry. Do not drop the
  flag to make the command pass.
- **User asks to skip labels** — honour it, and note in the report that the work
  went out unlabelled.

## Quick reference

```
Forge → Remote → Commit → Rebase → Labels → Issue → Push → Change request
```

Labels are chosen once (Step 4) and applied twice — to the issue and to the change
request — so the two always match.
