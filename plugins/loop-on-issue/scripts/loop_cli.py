#!/usr/bin/env python3
"""loop — one command surface over GitHub and GitLab for the issue queue.

Every read and write against the board goes through here rather than through raw
`gh` / `glab` invocations in a skill. Two reasons that is not merely tidiness:

* The prefix edge cases (stacked prefixes from an interrupted transition,
  unrelated prefixes like `[TEST]`, literal brackets in a title) have exactly one
  implementation, and it is tested.
* Every comment this posts carries an invisible agent marker. The agent
  authenticates as the same account as the human it reports to, so "did a human
  reply" means "is there an unmarked note newer than our last marked one". A
  comment posted outside this tool lacks the marker and later reads as a human
  reply, waking an issue nobody answered.

Exit codes are load-bearing: 0 success, 2 precondition not met (a normal
"skip this one" signal), 1 a real error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loopkit import config as cfg  # noqa: E402
from loopkit import doctor as doctor_mod  # noqa: E402
from loopkit import remotes, runner as runner_mod, scaffold  # noqa: E402
from loopkit import state as state_mod  # noqa: E402
from loopkit import templates as tpl  # noqa: E402
from loopkit.errors import ERROR, PRECONDITION, Precondition  # noqa: E402
from loopkit.forge import for_repo  # noqa: E402
from loopkit.proc import CommandError  # noqa: E402


def out(payload, pretty=True):
    print(json.dumps(payload, indent=2 if pretty else None, ensure_ascii=False))


def read_text(inline, path):
    if path:
        return sys.stdin.read() if path == "-" else open(path).read()
    return inline or ""


class Ctx:
    """Config, repository and forge, resolved once per invocation."""

    def __init__(self, args):
        self.cwd = os.path.abspath(getattr(args, "cwd", None) or os.getcwd())
        self.root = doctor_mod.repo_root(self.cwd) or self.cwd
        self.config = cfg.load(self.root).with_overrides(
            forge=getattr(args, "forge", None),
            repo=getattr(args, "repo", None),
            queue_label=getattr(args, "queue_label", None),
            assignee=getattr(args, "assignee", None),
            runner=getattr(args, "runner", None),
            template_lang=getattr(args, "lang", None),
        )
        self.config.validate()

    @property
    def repo(self):
        if not hasattr(self, "_repo"):
            self._repo = remotes.detect(
                cwd=self.root,
                forge=self.config.forge,
                repo_path=self.config.repo,
                prefer_remote=None if self.config.target_remote == "auto" else self.config.target_remote,
            )
        return self._repo

    @property
    def forge(self):
        if not hasattr(self, "_forge"):
            self._forge = for_repo(self.repo)
        return self._forge


# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #


def cmd_doctor(args):
    ctx = Ctx(args)
    report = doctor_mod.diagnose(ctx.cwd, ctx.config)
    if args.json:
        out(report.as_dict())
        return report.exit_code

    symbol = {doctor_mod.OK: "✓", doctor_mod.WARN: "!", doctor_mod.FAIL: "✗"}
    for check in report.checks:
        print("{} {:<28} {}".format(symbol[check.status], check.title, check.detail))
        if check.fix and check.status != doctor_mod.OK:
            print("  {:<28} → {}".format("", check.fix))
    print()
    if report.failures:
        print("{} blocking problem(s), {} warning(s). Fix the ✗ lines, then re-run "
              "`loop doctor`.".format(len(report.failures), len(report.warnings)))
    elif report.warnings:
        print("Ready, with {} warning(s).".format(len(report.warnings)))
    else:
        print("Ready.")
    return report.exit_code


def cmd_init(args):
    ctx = Ctx(args)
    existing = None
    try:
        existing = ctx.forge.list_labels()
    except (CommandError, Precondition) as exc:
        # Templates and config can still be written on a machine that cannot yet
        # reach the forge; the label step is simply reported as unavailable.
        print("note: cannot read labels yet ({}); the queue label will be skipped".format(exc),
              file=sys.stderr)

    actions = scaffold.plan(
        ctx.root, ctx.repo, ctx.config, lang=args.lang, force=args.force, existing_labels=existing
    )

    if not args.yes:
        if args.json:
            out({"planned": [a.as_dict() for a in actions], "applied": False})
        else:
            print("Would set up {} ({}):\n".format(ctx.repo.path, ctx.repo.forge))
            for action in actions:
                mark = "+" if action.status == scaffold.CREATE else (
                    "~" if action.status == scaffold.OVERWRITE else "=")
                print("  {} {:<44} {}".format(mark, action.target, action.detail))
            print("\nRe-run with --yes to apply. Nothing has been written.")
        return 0

    scaffold.apply(actions, ctx.root, ctx.forge if existing is not None else None)
    if args.json:
        out({"planned": [a.as_dict() for a in actions], "applied": True})
    else:
        for action in actions:
            print("{:<9} {}".format(action.status, action.target))
        print("\nNext: `loop doctor` to confirm, and set \"assignee\" and "
              "\"verify_command\" in {}/{}.".format(cfg.CONFIG_DIR, cfg.CONFIG_FILE))
    failed = [a for a in actions if a.status == scaffold.FAILED]
    return PRECONDITION if failed else 0


def cmd_template(args):
    ctx = Ctx(args)
    resolved = tpl.resolve(args.kind, ctx.root, ctx.repo.forge, ctx.config.template_lang)
    if args.action == "path":
        print(resolved.path)
    elif args.json:
        out({"kind": resolved.kind, "source": resolved.source, "path": resolved.path,
             "slots": tpl.slots(resolved.text), "body": resolved.body})
    else:
        print("# source: {} ({})\n".format(resolved.source, resolved.path), file=sys.stderr)
        sys.stdout.write(resolved.body)
    return 0


# --------------------------------------------------------------------------- #
# the board
# --------------------------------------------------------------------------- #


def cmd_list(args):
    ctx = Ctx(args)
    issues = ctx.forge.list_issues(
        label=ctx.config.queue_label, assignee=ctx.config.assignee, state=args.state
    )
    buckets = {k: [] for k in ("UNCLAIMED",) + state_mod.STATES}
    for issue in issues:
        st, base = state_mod.split_state(issue.title)
        buckets[st or "UNCLAIMED"].append(
            {
                "id": issue.number,
                "state": st,
                "title": base,
                "raw_title": issue.title,
                "url": issue.url,
                "updated_at": issue.updated_at,
                "labels": issue.labels,
                "runner": runner_mod.select(None, issue.labels, ctx.config.runner),
                "session_id": runner_mod.session_id(ctx.repo, issue.number),
            }
        )
    if args.active_only:
        for name in state_mod.DORMANT:
            buckets.pop(name, None)

    if args.json:
        out(buckets)
    else:
        for name, rows in buckets.items():
            tag = "  (dormant — not rescanned)" if name in state_mod.DORMANT else ""
            print("{} ({}){}".format(name, len(rows), tag))
            for row in rows:
                print("  #{}  {}".format(row["id"], row["title"]))
        print("\ntotal: {}".format(sum(len(v) for v in buckets.values())))
    return 0


def cmd_claim(args):
    """Take an unclaimed issue, verifying before and after the write.

    Neither forge offers compare-and-swap on a title, so this narrows the race
    window rather than pretending to close it: re-read immediately before
    writing, then read back to confirm our prefix is the one that stuck.
    """
    ctx = Ctx(args)
    issue = ctx.forge.get_issue(args.id)

    if issue.state != "opened":
        raise Precondition("#{} is {}, not opened".format(args.id, issue.state))
    if ctx.config.queue_label not in issue.labels:
        raise Precondition("#{} no longer carries {!r}".format(args.id, ctx.config.queue_label))
    if ctx.config.assignee and ctx.config.assignee not in issue.assignees:
        raise Precondition("#{} is not assigned to {!r}".format(args.id, ctx.config.assignee))
    st, base = state_mod.split_state(issue.title)
    if st is not None:
        raise Precondition("#{} already in state {}".format(args.id, st))

    ctx.forge.set_issue_title(args.id, state_mod.compose("CLAIMED", base))

    after, after_base = state_mod.split_state(ctx.forge.get_issue(args.id).title)
    if after != "CLAIMED":
        raise Precondition("#{} claim did not stick (now {}); another run won".format(args.id, after))

    out({"id": args.id, "state": "CLAIMED", "title": after_base,
         "runner": runner_mod.select(None, issue.labels, ctx.config.runner),
         "session_id": runner_mod.session_id(ctx.repo, args.id)}, pretty=False)
    return 0


def cmd_transition(args):
    ctx = Ctx(args)
    issue = ctx.forge.get_issue(args.id)
    st, base = state_mod.split_state(issue.title)

    if args.expect and st != (None if args.expect == "NONE" else args.expect):
        raise Precondition("#{} is in {}, expected {}".format(args.id, st, args.expect))

    target = None if args.to == "NONE" else args.to
    if st == target:
        out({"id": args.id, "state": target, "noop": True}, pretty=False)
        return 0

    ctx.forge.set_issue_title(args.id, state_mod.compose(target, base))
    out({"id": args.id, "from": st, "state": target, "title": base}, pretty=False)
    return 0


def cmd_skip(args):
    """Retire an issue that needs no code change, recording why.

    The reason is required and posted *before* the state changes, deliberately: a
    `[SKIP]` with no explanation is indistinguishable from an agent quietly
    dodging work it could not do, and nobody can tell which by the time they read
    the board. Comment first, so that if the transition fails the reasoning
    survives anyway.
    """
    ctx = Ctx(args)
    reason = read_text(args.reason, args.reason_file).strip()
    if len(reason) < 15:
        raise Precondition(
            "refusing to skip without a substantive reason — say what makes this "
            "issue need no code change, or use PAUSED to ask a human"
        )

    issue = ctx.forge.get_issue(args.id)
    st, base = state_mod.split_state(issue.title)
    if st == "SKIP":
        out({"id": args.id, "state": "SKIP", "noop": True}, pretty=False)
        return 0

    ctx.forge.add_issue_comment(
        args.id,
        state_mod.stamp(
            "**Skipped — no code change needed.**\n\n{}\n\n"
            "_To put this back in the queue, remove the `[SKIP]` prefix from the title._".format(reason)
        ),
    )
    ctx.forge.set_issue_title(args.id, state_mod.compose("SKIP", base))
    out({"id": args.id, "from": st, "state": "SKIP", "title": base}, pretty=False)
    return 0


def cmd_comment(args):
    ctx = Ctx(args)
    body = read_text(args.body, args.body_file)
    if not body.strip():
        raise Precondition("empty comment body")
    if not args.no_marker:
        body = state_mod.stamp(body, session=args.session, runner=args.runner)

    if args.pr:
        note = ctx.forge.add_cr_comment(args.pr, body)
        out({"pr": args.pr, "comment_id": note.id}, pretty=False)
    else:
        note = ctx.forge.add_issue_comment(args.id, body)
        out({"id": args.id, "comment_id": note.id}, pretty=False)
    return 0


def cmd_human_reply(args):
    """Has a human said anything since our last comment? Exit 0 if so, else 2."""
    ctx = Ctx(args)
    replies = state_mod.unanswered(ctx.forge.list_issue_comments(args.id))
    out({
        "id": args.id,
        "has_reply": bool(replies),
        "session_id": runner_mod.session_id(ctx.repo, args.id),
        "replies": [{"author": r.author, "created_at": r.created_at, "body": r.body} for r in replies],
    })
    return 0 if replies else PRECONDITION


def _no_cr_message(ctx, number):
    near = ctx.forge.unattributed_crs(number)
    word = ctx.forge.cr_word
    if not near:
        return "#{} has no {}".format(number, word)
    listed = ", ".join("{}{} ({}) {!r}".format(ctx.forge.cr_sigil, c.number, c.state, c.title)
                       for c in near[:5])
    return (
        "#{} has no {} titled 'to #{}: …'. {}s that merely mention it, and were "
        "NOT attributed: {}".format(number, word, number, ctx.forge.cr_short, listed)
    )


def cmd_cr_status(args):
    ctx = Ctx(args)
    cr = ctx.forge.find_cr_for_issue(args.id)
    if cr is None:
        raise Precondition(_no_cr_message(ctx, args.id))
    out({
        "id": args.id, "cr": cr.number, "forge": ctx.repo.forge, "kind": ctx.forge.cr_short,
        "state": cr.state, "merged": cr.merged, "draft": cr.draft,
        "source_branch": cr.source_branch, "target_branch": cr.target_branch,
        "url": cr.url, "session_id": runner_mod.session_id(ctx.repo, args.id),
    })
    return 0


def cmd_cr_feedback(args):
    """Is there review feedback the agent has not addressed? Exit 0 if so, else 2.

    Two independent signals, because both forges model an inline diff thread and a
    plain comment differently: an unresolved review thread, or any unmarked
    comment newer than our last marked one. Either means there is work to do.
    """
    ctx = Ctx(args)
    cr = ctx.forge.find_cr_for_issue(args.id)
    if cr is None:
        raise Precondition(_no_cr_message(ctx, args.id))
    if cr.state != "opened":
        # Feedback on a merged or closed change request has nowhere to go. Without
        # this guard every historical comment on an old one (none of which carry
        # our marker) reads as outstanding work, and the issue bounces out of
        # FINISHED forever.
        raise Precondition("#{} {} {}{} is {}, not open for review".format(
            args.id, ctx.forge.cr_word, ctx.forge.cr_sigil, cr.number, cr.state))

    unresolved = [t for t in ctx.forge.cr_review_threads(cr.number)
                  if not t.resolved and not state_mod.is_agent_note(t.body)]
    plain = state_mod.unanswered(ctx.forge.list_cr_comments(cr.number))
    has = bool(unresolved or plain)
    out({
        "id": args.id, "cr": cr.number, "cr_state": cr.state, "has_feedback": has,
        "unresolved_threads": [
            {"thread_id": t.id, "author": t.author, "created_at": t.created_at,
             "body": t.body, "path": t.path} for t in unresolved
        ],
        "new_comments": [
            {"author": c.author, "created_at": c.created_at, "body": c.body} for c in plain
        ],
        "session_id": runner_mod.session_id(ctx.repo, args.id),
    })
    return 0 if has else PRECONDITION


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #


def cmd_session_id(args):
    ctx = Ctx(args)
    issue = ctx.forge.get_issue(args.id)
    name = runner_mod.select(args.runner, issue.labels, ctx.config.runner)

    if name == runner_mod.CLAUDE:
        print(runner_mod.session_id(ctx.repo, args.id, args.generation))
        return 0

    # Codex assigns its own thread id, so the only record is what a previous run
    # wrote into a marker comment.
    recorded = state_mod.latest_session(ctx.forge.list_issue_comments(args.id))
    if not recorded or not recorded.get("session"):
        raise Precondition(
            "no {} session recorded for #{}; start a fresh one and record its id "
            "with `loop session-record`".format(name, args.id)
        )
    print(recorded["session"])
    return 0


def cmd_session_record(args):
    """Write a runner's session id into a marker comment.

    For runners whose session id cannot be chosen up front. The comment is the
    durable record: it survives the machine, and it is visible to a human who
    wonders which session a resume will land in.
    """
    ctx = Ctx(args)
    name = args.runner or ctx.config.runner
    body = state_mod.stamp(
        "Session recorded for the `{}` runner: `{}`.\n\n"
        "_This is how a later run resumes with the original context. Deleting this "
        "comment loses that._".format(name, args.session),
        session=args.session,
        runner=name,
    )
    note = ctx.forge.add_issue_comment(args.id, body)
    out({"id": args.id, "runner": name, "session": args.session, "comment_id": note.id}, pretty=False)
    return 0


# --------------------------------------------------------------------------- #
# creating issues
# --------------------------------------------------------------------------- #

BLOCKED_NOTE = (
    "> ⛔ **Blocked by {refs}** — this issue is deliberately **not** in the `{queue}` queue.\n"
    "> Add the `{queue}` label once {refs} has merged, and the next run will pick it up.\n"
)


def cmd_labels(args):
    ctx = Ctx(args)
    known = ctx.forge.list_labels()
    if args.json:
        out(known, pretty=False)
    else:
        for name in known:
            print("{}{}".format(name, "  ← queue" if name == ctx.config.queue_label else ""))
    return 0


def cmd_create(args):
    ctx = Ctx(args)
    body = read_text(args.body, args.body_file)
    if not body.strip():
        # A body-less issue is triaged as [SKIP] ("no substantive content") and
        # retired without ever being worked. Refusing here is cheaper than
        # discovering it on the board a run later.
        raise Precondition("empty issue body; content-free issues are retired as [SKIP]")
    body = body.rstrip() + "\n"

    queue = ctx.config.queue_label
    labels = list(dict.fromkeys(args.label or []))
    if args.blocked_by and queue in labels:
        # Queueing blocked work is the failure this flag exists to prevent: a run
        # would claim it, discover the dependency mid-session, and pause — having
        # spent a slot to learn what the author already knew.
        raise Precondition(
            "--blocked-by with the {!r} label would queue work that cannot start; "
            "drop the label or drop the dependency".format(queue)
        )
    if not args.blocked_by and not args.no_queue and queue not in labels:
        labels.append(queue)
    if args.no_queue and queue in labels:
        labels.remove(queue)

    ctx.forge.check_labels(labels)
    assignee = args.assignee or ctx.config.assignee
    if not assignee:
        raise Precondition(
            "no assignee; the swarm filters on it and an unassigned issue is "
            "invisible to it. Pass --assignee or set it in the config."
        )
    ctx.forge.resolve_assignee(assignee)

    if args.blocked_by:
        refs = ", ".join("#{}".format(str(b).lstrip("#")) for b in args.blocked_by)
        body = BLOCKED_NOTE.format(refs=refs, queue=queue) + "\n" + body
    if args.epic:
        # Neither forge has a portable epic; a plain issue reference works
        # everywhere and renders as a backlink on the parent.
        body = "Part of #{}\n\n{}".format(str(args.epic).lstrip("#"), body)

    if args.dry_run:
        out({"dry_run": True, "repo": ctx.repo.path, "forge": ctx.repo.forge, "title": args.title,
             "labels": labels, "queued": queue in labels, "assignee": assignee,
             "blocked_by": args.blocked_by or [], "body": body})
        return 0

    issue = ctx.forge.create_issue(args.title, body, labels=labels, assignees=[assignee])
    out({"id": issue.number, "url": issue.url, "title": issue.title, "labels": issue.labels,
         "queued": queue in (issue.labels or []), "assignee": assignee})
    return 0


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #


def build_parser():
    p = argparse.ArgumentParser(prog="loop", description=(__doc__ or "").strip().split("\n")[0])
    p.add_argument("-C", "--cwd", help="run as if started in this directory")
    p.add_argument("--repo", help="project path, e.g. acme/widget (default: from git remotes)")
    p.add_argument("--forge", choices=("auto", "github", "gitlab"), help="override forge detection")
    sub = p.add_subparsers(dest="cmd", required=True)

    def queue_filters(sp):
        # dest is queue_label, not label: `create` uses --label for the issue's own
        # (repeatable) labels, and sharing a dest silently made one overwrite the
        # other in the config overlay.
        sp.add_argument("--label", "--queue-label", dest="queue_label",
                        help="queue label (default: from config, else 'loop')")
        sp.add_argument("--assignee", help="required assignee (default: from config)")

    sp = sub.add_parser("doctor", help="check that this machine and repo are ready")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("init", help="write config, templates and the queue label")
    sp.add_argument("--yes", action="store_true", help="apply the plan (without this, plan only)")
    sp.add_argument("--lang", choices=("en", "zh"), help="template language")
    sp.add_argument("--force", action="store_true", help="overwrite files that already exist")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("template", help="show the template that actually applies here")
    sp.add_argument("action", choices=("show", "path"))
    sp.add_argument("kind", choices=tpl.KINDS)
    sp.add_argument("--lang", choices=("en", "zh"))
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_template)

    sp = sub.add_parser("list", help="group matching issues by state")
    queue_filters(sp)
    sp.add_argument("--state", default="opened", choices=("opened", "closed", "all"))
    sp.add_argument("--active-only", action="store_true", help="drop dormant buckets (SKIP)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("claim", help="unclaimed -> CLAIMED, with verification")
    queue_filters(sp)
    sp.add_argument("--id", "--iid", type=int, required=True, dest="id")
    sp.set_defaults(func=cmd_claim)

    sp = sub.add_parser("transition", help="move an issue to another state")
    sp.add_argument("--id", "--iid", type=int, required=True, dest="id")
    sp.add_argument("--to", required=True, choices=state_mod.STATES + ("NONE",))
    sp.add_argument("--expect", choices=state_mod.STATES + ("NONE",),
                    help="only transition if currently in this state")
    sp.set_defaults(func=cmd_transition)

    sp = sub.add_parser("skip", help="retire an issue that needs no code change")
    sp.add_argument("--id", "--iid", type=int, required=True, dest="id")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--reason")
    g.add_argument("--reason-file", help="path, or - for stdin")
    sp.set_defaults(func=cmd_skip)

    sp = sub.add_parser("comment", help="post a note, stamped as agent-written")
    sp.add_argument("--id", "--iid", type=int, dest="id", help="issue number")
    sp.add_argument("--pr", "--mr", type=int, dest="pr", help="change request number instead")
    sp.add_argument("--no-marker", action="store_true",
                    help="omit the agent marker (only when relaying something a human wrote)")
    sp.add_argument("--session", help="record a session id in the marker")
    sp.add_argument("--runner", choices=runner_mod.RUNNERS)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--body")
    g.add_argument("--body-file", help="path, or - for stdin")
    sp.set_defaults(func=cmd_comment)

    sp = sub.add_parser("human-reply", help="exit 0 if a human replied since our last note")
    sp.add_argument("--id", "--iid", type=int, required=True, dest="id")
    sp.set_defaults(func=cmd_human_reply)

    for name in ("pr-status", "mr-status"):
        sp = sub.add_parser(name, help="state of the change request linked to this issue")
        sp.add_argument("--id", "--iid", type=int, required=True, dest="id")
        sp.set_defaults(func=cmd_cr_status)

    for name in ("pr-feedback", "mr-feedback"):
        sp = sub.add_parser(name, help="exit 0 if there is unaddressed review feedback")
        sp.add_argument("--id", "--iid", type=int, required=True, dest="id")
        sp.set_defaults(func=cmd_cr_feedback)

    sp = sub.add_parser("session-id", help="the id to resume this issue's session with")
    sp.add_argument("--id", "--iid", type=int, required=True, dest="id")
    sp.add_argument("--runner", choices=runner_mod.RUNNERS)
    sp.add_argument("--generation", type=int, default=0,
                    help="bump to abandon a poisoned context and start fresh (claude only)")
    sp.set_defaults(func=cmd_session_id)

    sp = sub.add_parser("session-record", help="record a runner-assigned session id")
    sp.add_argument("--id", "--iid", type=int, required=True, dest="id")
    sp.add_argument("--session", required=True)
    sp.add_argument("--runner", choices=runner_mod.RUNNERS)
    sp.set_defaults(func=cmd_session_record)

    sp = sub.add_parser("labels", help="the labels this project already defines")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_labels)

    sp = sub.add_parser("create", help="create one queue-ready issue")
    sp.add_argument("--title", required=True)
    sp.add_argument("--assignee", help="default: from config")
    sp.add_argument("--label", action="append",
                    help="repeatable; must already exist. The queue label is added "
                         "automatically unless --no-queue or --blocked-by")
    sp.add_argument("--queue-label", dest="queue_label",
                    help="the label that makes an issue startable (default: from config)")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--body")
    g.add_argument("--body-file", help="path, or - for stdin")
    sp.add_argument("--blocked-by", action="append",
                    help="repeatable issue this depends on; implies --no-queue")
    sp.add_argument("--no-queue", action="store_true", help="create without the queue label")
    sp.add_argument("--epic", help="parent issue number; adds a 'Part of #N' backlink")
    sp.add_argument("--dry-run", action="store_true", help="render the payload, write nothing")
    sp.set_defaults(func=cmd_create)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "comment" and not (args.id or args.pr):
        print("loop comment: needs --id or --pr", file=sys.stderr)
        return ERROR
    try:
        return args.func(args)
    except Precondition as exc:
        print("skip: {}".format(exc), file=sys.stderr)
        return PRECONDITION
    except cfg.ConfigError as exc:
        print("config error: {}".format(exc), file=sys.stderr)
        return ERROR
    except (CommandError, ValueError, KeyError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
