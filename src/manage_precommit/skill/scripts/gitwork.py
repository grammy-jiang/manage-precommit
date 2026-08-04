#!/usr/bin/env python3
"""Deterministic git operations for the manage-precommit skill.

Everything here is a decision a program can make correctly every time: which of
this run's files are tracked, what the diff actually is, whether a commit
touched only those files *and only the content this run verified*, and which of
the push outcomes a branch is in. None of it is left to be inferred from prose
output, and none of it is re-derived by hand.

The one thing this script never does is decide on the user's behalf: it reports
the state and the single action that state permits. Asking the user, and passing
--confirm-force back, stays with the caller.

Unlike its sibling in manage-gitignore, the managed path is not a constant. A
run writes ``.pre-commit-config.yaml`` and, depending on the hooks chosen, some
of ``.yamllint.yaml``, ``.markdownlint.yaml`` and ``scripts/lint-mermaid.mjs``.
That set -- and the sha256 each file was verified as -- comes from the facts
file precommit.py wrote, so the commit gate is bound to what this run actually
did rather than to a hardcoded name.

Subcommands (all take --dir REPO, default "."):
  status         which managed files changed -- and print the real diff
  commit         add + commit ONLY this run's files from a message file, verified
  push-plan      classify the push situation; emit the one permitted action
  push           execute exactly what push-plan permits (--confirm-force to force)
  facts          merge git-side facts into the JSON precommit.py wrote

Every subcommand prints JSON to stdout (plus human-readable text to stderr where
useful) and exits non-zero on any failure worth stopping for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from typing import NoReturn, cast

from shared import (
    Facts,
    PushPlan,
    clean,
    has_suspicious_chars,
    make_git,
    read_bytes_nofollow,
    read_bytes_or_die,
    refuse_facts_inside_repo,
    refuse_option_like,
    write_json_or_die,
)

CONFIG_NAME = ".pre-commit-config.yaml"

# Exit codes. Callers are told to branch on the JSON, but a distinct status per
# refusal keeps a shell wrapper honest too.
EXIT_ERROR = 1
EXIT_BAD_COMMIT = 2  # committed, but not what this run intended
EXIT_NOT_PUSHED = 3  # a stop-* action: nothing to push
EXIT_NEEDS_FORCE = 4  # diverged, or the approved remote state moved
EXIT_REMOTE_CHOICE = 5  # ambiguous or unknown remote
EXIT_NEEDS_EXPECT = 6  # a force was asked for without the approved sha


def die(msg: str) -> NoReturn:
    print(f"gitwork: {msg}", file=sys.stderr)
    sys.exit(EXIT_ERROR)


git = make_git(die)


def emit(payload: Mapping[str, object]) -> None:
    json.dump(dict(payload), sys.stdout, indent=2)
    sys.stdout.write("\n")


def is_repo(repo: str) -> bool:
    rc, out, _ = git(repo, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"  # inside a .git dir this exits 0 and says "false"


def require_repo(repo: str) -> None:
    if not is_repo(repo):
        die(f"{repo} is not a git work tree")


def has_commits(repo: str) -> bool:
    """False on an unborn HEAD -- a repo with no commit yet.

    `git diff HEAD` is a fatal error there, so anything comparing against HEAD
    has to ask first.
    """
    rc, _, _ = git(repo, "rev-parse", "--verify", "-q", "HEAD")
    return rc == 0


# -- the managed set ---------------------------------------------------------
def load_facts(path: str) -> Facts:
    # Through the same no-follow reader as every other file: a facts path is
    # caller-supplied, so it can be a symlink or a FIFO just as easily.
    raw = read_bytes_or_die(path, die)
    try:
        facts = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        die(f"cannot read facts file {path}: {exc}")
    if not isinstance(facts, dict):
        die("facts file must contain a JSON object")
    # The file was written by these same tools, so its shape is Facts; json.load
    # simply cannot say so. Every read site still uses .get() with a default.
    return cast("Facts", facts)


def save_facts(path: str, facts: Facts) -> None:
    """Atomic: several commands update this file in turn, and a half-written one
    would fail the next step with a JSON error rather than the real cause."""
    write_json_or_die(path, dict(facts), die)


def managed(facts: Facts) -> list[tuple[str, str]]:
    """This run's (path, sha256) pairs, validated.

    A path is rejected if it escapes the repository or is absolute: the facts
    file is on disk between steps, and a rewritten one must not be able to talk
    `commit` into staging something outside the tree.
    """
    entries = (facts.get("internal") or {}).get("managed_files") or []
    if not entries:
        die(
            "the facts file records no managed files, so there is nothing this run "
            "may commit. Re-run the write step with --facts-out."
        )
    out: list[tuple[str, str]] = []
    for item in entries:
        path = str(item.get("path", ""))
        digest = str(item.get("sha256", ""))
        if not path or not digest:
            die("a managed_files entry is missing its path or sha256")
        refuse_option_like(path, "managed path", die)
        if os.path.isabs(path) or os.path.normpath(path).startswith(".."):
            die(f"refusing a managed path outside the repository: {path!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            die(f"refusing a managed sha256 of unexpected shape for {path!r}")
        out.append((os.path.normpath(path), digest))
    return out


def managed_paths(facts: Facts) -> list[str]:
    return [p for p, _ in managed(facts)]


def porcelain(repo: str, paths: list[str]) -> str:
    """`git status --porcelain -- <paths>`, or "" when nothing changed.

    Unstripped: the leading column is meaningful (see shared.make_git).

    A non-zero exit is fatal rather than empty. Empty output means "nothing
    changed", and file_states() seeds every path clean -- so swallowing a
    failure here (a locked index, an I/O error) would report the whole set as
    clean, `status` would emit changed:false, and SKILL.md's Step 5 table would
    send the agent to the summary with "no change: the config already matched",
    silently discarding work this run had just written.
    """
    rc, out, err = git(repo, "status", "--porcelain", "--no-renames", "--", *paths, strip=False)
    if rc != 0:
        die(
            f"git status failed (exit {rc}): {err or 'no stderr'}. Refusing to report "
            "the state of this run's files, because a failed check is not a clean result."
        )
    return out


def file_states(repo: str, paths: list[str]) -> dict[str, str]:
    """One of untracked/staged/modified/clean, per managed path.

    Porcelain codes are XY: X is the index status, Y the work-tree status. Both
    dirty ("MM") counts as staged with more on top -- reported as "staged" so
    the diff shown is the one that would be committed.
    """
    states = dict.fromkeys(paths, "clean")
    for line in porcelain(repo, paths).splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip().strip('"')
        if path not in states:
            continue
        states[path] = "untracked" if code == "??" else ("modified" if code[0] == " " else "staged")
    return states


# -- status ------------------------------------------------------------------
def build_diff(repo: str, states: dict[str, str]) -> tuple[str, list[str]]:
    """The real diff for the managed set, and the commands that produced it.

    A run's files are routinely in different states at once -- the config
    modified, a linter config brand new -- so this is two comparisons, not one.
    Tracked paths are diffed against HEAD, because `commit --only` re-stages
    from the working tree and a --cached diff would hide unstaged hunks. An
    untracked path has no HEAD side at all, so it is shown whole against
    /dev/null, which is exactly what its first commit records.
    """
    chunks: list[str] = []
    commands: list[str] = []
    tracked = sorted(p for p, s in states.items() if s in ("staged", "modified"))
    fresh = sorted(p for p, s in states.items() if s == "untracked")

    if tracked and has_commits(repo):
        cmd = ["diff", "HEAD", "--", *tracked]
        rc, out, err = git(repo, *cmd)
        if rc != 0:
            die(f"git {' '.join(cmd)} failed: {err}")
        commands.append("git " + " ".join(cmd))
        if out:
            chunks.append(out)
    elif tracked:
        fresh = sorted({*fresh, *tracked})  # unborn HEAD: nothing to compare against

    for rel in fresh:
        cmd = ["diff", "--no-index", "--", os.devnull, rel]
        rc, out, err = git(repo, *cmd)
        # `--no-index` exits 1 to mean "these differ", which is the normal case
        # for a new file, not a failure.
        if rc not in (0, 1):
            die(f"git {' '.join(cmd)} failed: {err}")
        commands.append("git " + " ".join(cmd))
        if out:
            chunks.append(out)
    return "\n".join(chunks), commands


def cmd_status(args: argparse.Namespace) -> int:
    repo = args.dir
    refuse_facts_inside_repo(repo, args.facts, die)
    facts = load_facts(args.facts)
    paths = managed_paths(facts)
    if not is_repo(repo):
        emit({"is_repo": False, "states": None, "diff": None, "files": paths})
        return 0
    states = file_states(repo, paths)
    diff, commands = build_diff(repo, states)
    # The diff is read by a human to approve a change, so it is shown verbatim
    # -- rewriting it would defeat the point. But a config's content is partly
    # upstream-supplied and partly repo-supplied, so an ESC or a bidi override
    # could make the rendered diff disagree with the bytes. Flag that rather
    # than silently pass it through.
    suspicious = has_suspicious_chars(diff)
    if diff:
        if suspicious:
            print(
                "gitwork: WARNING - this diff contains control or text-reordering "
                "characters; what your terminal shows may not be what the files "
                "say. Inspect with `git diff | cat -v`.",
                file=sys.stderr,
            )
        print(diff, file=sys.stderr)
    emit(
        {
            "is_repo": True,
            "files": paths,
            "states": states,
            "diff_commands": commands,
            "diff": diff,
            "changed": any(s != "clean" for s in states.values()),
            "suspicious_characters": suspicious,
        }
    )
    return 0


# -- commit ------------------------------------------------------------------
def safe_token(value: str, what: str) -> str:
    """Reject a remote/branch git would read as an option.

    These come from repository config, which a checked-out repo can set: a
    remote literally named "--upload-pack=..." would otherwise reach `git push`
    as a flag.
    """
    return refuse_option_like(value, what, die)


def safe_ref(ref: str) -> str:
    """Reject a ref git would read as an option."""
    return refuse_option_like(ref, "ref", die)


def commit_files(repo: str, ref: str = "HEAD") -> list[str]:
    """Paths touched by a commit. A failed lookup is an error, not an empty commit."""
    rc, out, err = git(repo, "show", "--name-only", "--format=", safe_ref(ref))
    if rc != 0:
        die(f"cannot read commit {ref}: {err or f'git exit {rc}'}")
    return [line for line in out.splitlines() if line.strip()]


def undo_hint(repo: str, ref: str = "HEAD") -> str:
    """How to undo `ref` -- `<ref>^` does not exist for a first commit."""
    rc, count, _ = git(repo, "rev-list", "--count", safe_ref(ref))
    if rc == 0 and count.isdigit() and int(count) <= 1:
        return f"`git update-ref -d {ref}` removes it (there is no parent commit)"
    return f"`git reset --soft {ref}^` undoes it and restores the index"


def remote_push_url(repo: str, name: str) -> str:
    """Where `git push <name>` actually goes.

    pushurl when set, else url: git lets them differ, and showing the fetch URL
    would have the user approve a destination the push never reaches.
    """
    rc, pushurl, _ = git(repo, "config", "--get", f"remote.{name}.pushurl")
    if rc == 0 and pushurl:
        return pushurl
    _, url, _ = git(repo, "config", "--get", f"remote.{name}.url")
    return url


def safe_merge_ref(ref: str) -> str:
    """A branch ref from repo config, shape-checked before it builds a refspec.

    merge_ref is interpolated into `HEAD:<ref>` and into a --force-with-lease
    argument. A ':' or a leading '+' there would change what the push means, and
    the value comes from branch.<name>.merge, which a checked-out repo controls.
    """
    refuse_option_like(ref, "upstream ref", die)
    if not re.fullmatch(r"refs/heads/[^:\s+][^:\s]*", ref):
        die(f"refusing upstream ref of unexpected shape: {ref!r}")
    return ref


def current_short_sha(repo: str) -> str:
    _, sha, _ = git(repo, "rev-parse", "--short", "HEAD")
    return sha


def blob_matches_verified(repo: str, ref: str, rel: str, expected_sha256: str) -> bool:
    """Does `ref` record `rel` as the bytes this run wrote and verified?

    Two steps, because git object ids and our sha256 are not comparable: the
    work-tree file is re-checked against the recorded sha256, and the commit's
    blob is then compared to the work tree as git object ids -- which no
    encoding or trailing-newline handling can make differ for equal content.
    Reading the blob back through a text pipe would not round-trip arbitrary
    bytes, so it is never done.
    """
    rc_ref, recorded, _ = git(repo, "rev-parse", f"{ref}:{rel}")
    rc_tree, current, _ = git(repo, "hash-object", "--", os.path.join(repo, rel))
    if rc_ref != 0 or rc_tree != 0:
        return True  # cannot tell; the caller's other checks still apply
    if recorded != current:
        return False
    try:
        on_disk = read_bytes_nofollow(os.path.join(repo, rel))
    except OSError:
        return True  # unreadable now; the commit-time gate already ran
    return hashlib.sha256(on_disk).hexdigest() == expected_sha256


def commit_scope(paths: list[str]) -> str:
    """The scope string recorded in the facts. Written once, read twice."""
    if paths == [CONFIG_NAME]:
        return f"{CONFIG_NAME} only"
    return f"{len(paths)} pre-commit setup files only"


def scope_violation(repo: str, committed: list[str], expected: list[str]) -> str | None:
    """The message for a commit that touched anything outside the run's set."""
    extra = sorted(set(committed) - set(expected))
    if not extra:
        return None
    return (
        f"commit touches {extra} -- expected only {sorted(expected)}. "
        f"Do NOT push; {undo_hint(repo)}."
    )


def cmd_commit(args: argparse.Namespace) -> int:
    """Commit ONLY this run's files, and only the content it verified.

    precommit.py refused to start if any of these already carried an
    uncommitted change, so the whole of each file is this run's work and
    committing it whole is honest.
    """
    repo = args.dir
    require_repo(repo)
    refuse_facts_inside_repo(repo, args.facts, die)
    facts = load_facts(args.facts)
    entries = managed(facts)
    paths = [p for p, _ in entries]

    if not os.path.lexists(args.message_file):
        die(f"message file not found: {args.message_file}")
    # Same guard as every other file this skill reads: a message file that is a
    # symlink or a FIFO is refused rather than followed.
    message = read_bytes_or_die(args.message_file, die).decode("utf-8", "replace")
    if not message.strip():
        die(f"message file is empty: {args.message_file}")

    # Before ANY git call: git itself will hash the working-tree files, and on a
    # FIFO that blocks forever. Refusing a non-regular target here turns a hang
    # into an error, and does it before the index has been touched. The same
    # read binds each file to the bytes precommit.py verified -- a path match is
    # not enough, because anything could have rewritten one since.
    for rel, expected in entries:
        full = os.path.join(repo, rel)
        if not os.path.lexists(full):
            die(f"{rel} is missing; refusing to commit. Re-run from the write step.")
        actual = hashlib.sha256(read_bytes_or_die(full, die)).hexdigest()
        # A window remains between this check and `git add` below: another
        # process could rewrite the file in between. Closing it entirely needs
        # the content staged from a held descriptor, which git offers no
        # porcelain for; the check still turns the common case (a stale or
        # edited file) from a silent commit into a refusal.
        if actual != expected:
            die(
                f"{rel} changed since it was written and verified "
                f"(sha256 {actual[:12]} != {expected[:12]}); refusing to commit it"
            )

    states = file_states(repo, paths)
    if all(state == "clean" for state in states.values()):
        die("none of this run's files have changes to commit")

    # Everything else that is dirty right now: reported so the summary can say
    # what the commit deliberately left alone. Unstripped, so the XY columns
    # stay aligned and ln[3:] really is the path on every line. --no-renames
    # keeps every line "XY path"; with renames a line reads "R  old -> new" and
    # the comparison would miss.
    _, all_status, _ = git(repo, "status", "--porcelain", "--no-renames", strip=False)
    untouched = [
        ln for ln in all_status.splitlines() if ln[3:].strip().strip('"') not in set(paths)
    ]

    # The blob id of exactly the bytes just verified, taken before staging; each
    # committed object is compared against it once the commit exists.
    staged_oids: dict[str, str] = {}
    for rel in paths:
        _, oid, _ = git(repo, "hash-object", "--", os.path.join(repo, rel))
        if oid:
            staged_oids[rel] = oid

    git(repo, "add", "--", *paths, check=True)
    # `-F -` with the bytes already validated above: git never re-reads the
    # caller's path, so what was checked and what is committed are the same.
    rc, _, err = git(repo, "commit", "--only", "-F", "-", "--", *paths, stdin=message)
    if rc != 0:
        # `add` succeeded, so leaving now would strand the files in the index in
        # a state the caller did not create. Put the index back -- and if even
        # that fails, say so rather than asserting a cleanup that did not happen.
        reset_rc, _, reset_err = git(repo, "reset", "-q", "--", *paths)
        if reset_rc == 0:
            die(
                f"commit failed (exit {rc}); this run's files were unstaged again: "
                f"{err or 'no stderr'}"
            )
        die(
            f"commit failed (exit {rc}): {err or 'no stderr'} -- AND the cleanup "
            f"reset also failed: {reset_err or f'exit {reset_rc}'}. Files may still "
            "be staged; check `git status` before doing anything else."
        )

    committed = commit_files(repo)
    sha = current_short_sha(repo)
    problem = scope_violation(repo, committed, paths)
    if problem:
        # Still JSON on stdout: the caller needs the hash to report (and undo)
        # the commit that should not have happened.
        emit(
            {
                "hash": sha,
                "files": committed,
                "only_managed": False,
                "verdict": "touched-extra-files",
                "remedy": problem,
                # What the summary step must record. Derived here so the doc does
                # not carry a second copy of the same mapping.
                "record_choice": "not committed",
                "record_note": (
                    f"commit {sha} was made but touched extra files; not recorded "
                    "-- see the reported undo command"
                ),
            }
        )
        print(f"gitwork: {problem}", file=sys.stderr)
        return EXIT_BAD_COMMIT

    # The file list is not the content: a hook (or a race) could commit
    # different bytes under the same path.
    for rel, oid in staged_oids.items():
        rc_oid, committed_oid, _ = git(repo, "rev-parse", f"{sha}:{rel}")
        if rc_oid != 0 or committed_oid != oid:
            emit(
                {
                    "hash": sha,
                    "files": committed,
                    "only_managed": True,
                    "content_matches": False,
                    "verdict": "content-mismatch",
                    "remedy": f"Do NOT push; {undo_hint(repo)}.",
                    "record_choice": "not committed",
                    "record_note": (
                        f"commit {sha} recorded different content than was verified "
                        f"for {rel}; not recorded -- see the reported undo command"
                    ),
                }
            )
            print(
                f"gitwork: commit {sha} recorded {rel} with content that is not what "
                f"this run wrote and verified. Do NOT push; {undo_hint(repo)}.",
                file=sys.stderr,
            )
            return EXIT_BAD_COMMIT

    _, raw_subject, _ = git(repo, "log", "-1", "--format=%s")
    subject = clean(raw_subject)
    n = len(untouched)
    phrase = f"{n} other file{'' if n == 1 else 's'}" if n else None
    result = {
        "hash": sha,
        "subject": subject,
        "files": committed,
        "only_managed": True,
        "content_matches": True,
        "verdict": "ok",
        "untouched_count": n,
        "untouched": phrase,
    }
    # Recorded here, at the moment the numbers are true. Nothing downstream has
    # to re-observe a working tree that has since moved, or reword a raw count.
    commit = facts.setdefault("commit", {})
    commit.update({"hash": sha, "subject": subject, "scope": commit_scope(paths)})
    if phrase:
        commit["untouched"] = phrase
    save_facts(args.facts, facts)
    result["facts"] = args.facts
    emit(result)
    return 0


# -- push --------------------------------------------------------------------
# What each action means for a human, and whether `push` will do anything. These
# would otherwise be two tables in SKILL.md restating, in prose, a decision this
# function already made. One authority; the doc reads `guidance` and
# `permits_push`.
ACTION_GUIDANCE = {
    "fast-forward": "it would go to {dest}",
    "stop-up-to-date": "{dest} already has this commit; nothing to push. Not a failure.",
    "no-upstream": "first push for this branch; it would go to {dest}",
    "diverged": (
        "{dest} has commits this branch does not. Pushing needs a force-push "
        "decision that can drop them -- see references/push-safety.md."
    ),
    "stop-behind-only": (
        "{dest} is ahead and there is nothing new to send. Once this commit "
        "lands the branch becomes diverged, and pushing would then need a "
        "force-push decision that can drop remote commits."
    ),
    "stop-no-remote": "no remote is configured, so a push has nowhere to go",
    "stop-detached-head": "not on a branch (detached HEAD); check one out first",
    "stop-fetch-failed": "could not reach the remote; check network and auth",
    "stop-compare-failed": "could not read ahead/behind, so no push decision is safe",
    "stop-not-a-repo": "not a git work tree",
}
PUSH_PERMITTED = {"fast-forward", "no-upstream", "diverged"}


def destination(plan: PushPlan) -> str:
    """Where a push would land, always naming the URL and not just a nickname."""
    urls = plan.get("remote_urls") or {}
    remote = plan.get("remote")
    if plan.get("merge_ref"):  # an upstream exists: one destination, fully known
        branch = str(plan["merge_ref"]).removeprefix("refs/heads/")
        url = plan.get("remote_url")
        return f"{remote}/{branch}" + (f" ({url})" if url else "")
    if remote:  # first push, remote already settled
        return f"{remote}" + (f" ({urls[remote]})" if remote in urls else "")
    if urls:  # several candidates and no origin: nothing is settled yet
        listed = ", ".join(f"{n} ({u})" for n, u in sorted(urls.items()))
        return f"one of {listed} -- not settled yet, a follow-up question will confirm"
    return "the remote"


def describe(plan: PushPlan) -> PushPlan:
    """Annotate a plan with the sentence to say and whether a push can happen."""
    action = str(plan.get("action", ""))
    plan["guidance"] = ACTION_GUIDANCE.get(action, action).format(dest=destination(plan))
    plan["permits_push"] = action in PUSH_PERMITTED
    return plan


def push_plan(repo: str) -> PushPlan:
    """Classify the push situation. `action` names the ONE permitted next step."""
    if not is_repo(repo):
        return {"action": "stop-not-a-repo"}
    rc, _, _ = git(repo, "symbolic-ref", "-q", "HEAD")
    if rc != 0:
        return {"action": "stop-detached-head"}
    _, branch, _ = git(repo, "branch", "--show-current")

    rc, _, _ = git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    if rc != 0:
        _, remotes_out, _ = git(repo, "remote")
        remotes = [r for r in remotes_out.splitlines() if r.strip() and not r.startswith("-")]
        if not remotes:
            return {"action": "stop-no-remote", "branch": branch}
        remote = remotes[0] if len(remotes) == 1 else ("origin" if "origin" in remotes else None)
        # The PUSH url, which can differ from the fetch url: showing the fetch
        # url would have the user approve a destination the push never goes to.
        urls = {name: remote_push_url(repo, name) for name in remotes}
        return {
            "action": "no-upstream",
            "branch": branch,
            "remotes": remotes,
            "remote_urls": urls,
            "remote": remote,  # null => the caller must ask which one
            # Remote and branch names are repo-controlled and are about to be
            # shown as a push destination, so flag them like any other display
            # text the user acts on.
            "suspicious_characters": has_suspicious_chars(" ".join([*remotes, branch])),
        }

    # Upstream exists: refresh, then read ahead/behind. A failed fetch means the
    # comparison would be against stale data, so it is a hard stop.
    rc, _, err = git(repo, "fetch")
    if rc != 0:
        return {"action": "stop-fetch-failed", "branch": branch, "error": err}
    _, remote, _ = git(repo, "config", "--get", f"branch.{branch}.remote")
    _, merge_ref, _ = git(repo, "config", "--get", f"branch.{branch}.merge")
    _, upstream_sha, _ = git(repo, "rev-parse", "@{u}")
    remote_url = remote_push_url(repo, remote)
    rc, counts, _ = git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    if rc != 0 or "\t" not in counts:
        return {"action": "stop-compare-failed", "branch": branch}
    ahead, behind = (int(n) for n in counts.split("\t", 1))
    base: PushPlan = {
        "branch": branch,
        "remote": remote,
        "merge_ref": merge_ref,
        # Surfaced for every upstream action, not just the exotic ones: a
        # fast-forward and a force-push both get confirmed against a real URL
        # rather than a bare remote name.
        "remote_url": remote_url,
        "suspicious_characters": has_suspicious_chars(
            " ".join([branch, remote, merge_ref, remote_url])
        ),
        # The remote commit this comparison was made against. A force-push must
        # be leased against THIS sha -- the one whose consequences were shown to
        # the user -- not against whatever the remote holds by the time the push
        # runs. See cmd_push.
        "upstream_sha": upstream_sha,
        "ahead": ahead,
        "behind": behind,
    }
    if behind == 0:
        base["action"] = "stop-up-to-date" if ahead == 0 else "fast-forward"
        return base
    if ahead == 0:
        # Nothing local to add: a force here would delete remote commits and
        # contribute none. Never offered.
        base["action"] = "stop-behind-only"
        return base
    _, drop, _ = git(repo, "log", "--oneline", "HEAD..@{u}")
    _, add, _ = git(repo, "log", "--oneline", "@{u}..HEAD")
    base["action"] = "diverged"
    base["would_drop"] = drop.splitlines()
    base["would_add"] = add.splitlines()
    # Those subject lines are read to approve an irreversible act, so they join
    # the names already checked in `base`.
    base["suspicious_characters"] = base.get("suspicious_characters", False) or (
        has_suspicious_chars(drop + add)
    )
    return base


def cmd_push_plan(args: argparse.Namespace) -> int:
    emit(describe(push_plan(args.dir)))
    return 0


def record_push(args: argparse.Namespace, plan: PushPlan, sha: str) -> None:
    """Store where the push landed, from verified state -- never from free text.

    Kept as its three pieces rather than a sentence: summary.py owns every
    display string, exactly as it does for the commit hash and subject.
    """
    if not args.facts:
        return
    facts = load_facts(args.facts)
    ref = plan.get("merge_ref") or plan.get("branch") or ""
    # removeprefix, not rsplit: "refs/heads/feature/foo" is the branch
    # "feature/foo", and splitting on the last slash would call it "foo".
    facts.setdefault("commit", {})["push"] = {
        "sha": sha,
        # A push only happens once a remote is settled, so this is never null here.
        "remote": str(plan["remote"]),
        "branch": ref.removeprefix("refs/heads/"),
    }
    save_facts(args.facts, facts)


def cmd_push(args: argparse.Namespace) -> int:
    """Execute exactly the action push-plan permits -- nothing else.

    The plan is recomputed here rather than taken as an argument, so a stale or
    hand-edited plan cannot talk this into a push the current state forbids.
    """
    repo = args.dir
    refuse_facts_inside_repo(repo, args.facts, die)
    plan = describe(push_plan(repo))
    action = plan["action"]

    if action.startswith("stop-"):
        emit({**plan, "pushed": False})
        print(f"gitwork: not pushing ({action})", file=sys.stderr)
        return 0 if action == "stop-up-to-date" else EXIT_NOT_PUSHED

    if action == "fast-forward":
        # Explicit refspec: under push.default=matching a bare `git push` would
        # push every matching branch, not just this one.
        git(
            repo,
            "push",
            safe_token(str(plan["remote"]), "remote"),
            f"HEAD:{safe_merge_ref(plan['merge_ref'])}",
            check=True,
        )
        sha = current_short_sha(repo)
        record_push(args, plan, sha)
        emit({**plan, "pushed": True, "forced": False})
        return 0

    if action == "no-upstream":
        remote = args.remote or plan["remote"]
        if not remote:
            emit({**plan, "pushed": False, "error": "ambiguous-remote"})
            names = ", ".join(clean(r) for r in plan["remotes"])
            warn = (
                " (some names contain characters that can misrepresent themselves)"
                if plan.get("suspicious_characters")
                else ""
            )
            print(
                f"gitwork: several remotes ({names}){warn}; pass --remote to choose",
                file=sys.stderr,
            )
            return EXIT_REMOTE_CHOICE
        if remote not in plan["remotes"]:
            emit({**plan, "pushed": False, "error": "unknown-remote"})
            names = ", ".join(clean(r) for r in plan["remotes"])
            print(f"gitwork: unknown remote {clean(remote)!r} (have: {names})", file=sys.stderr)
            return EXIT_REMOTE_CHOICE
        git(
            repo,
            "push",
            "-u",
            safe_token(remote, "remote"),
            safe_token(str(plan["branch"]), "branch"),
            check=True,
        )
        sha = current_short_sha(repo)
        record_push(args, {**plan, "remote": remote}, sha)
        emit({**plan, "remote": remote, "pushed": True, "forced": False})
        return 0

    if action == "diverged":
        if not args.confirm_force:
            emit({**plan, "pushed": False})
            print(
                "gitwork: branch has diverged -- a force-push would DROP "
                f"{plan['behind']} remote commit(s). Refusing without --confirm-force. "
                "See references/push-safety.md.",
                file=sys.stderr,
            )
            return EXIT_NEEDS_FORCE
        # A bare --force-with-lease leases against the remote-tracking ref, which
        # push_plan just refreshed with `git fetch` -- so it would authorise
        # dropping commits that appeared AFTER the user saw the plan, which is
        # precisely what the lease is supposed to prevent. Lease explicitly
        # against the sha whose consequences were shown and approved.
        if not args.expect_remote:
            emit({**plan, "pushed": False, "error": "missing-expect-remote"})
            print(
                "gitwork: --confirm-force also requires --expect-remote <sha>, the "
                "`upstream_sha` from the push-plan the user approved. Without it the "
                "lease would be computed after this command's own fetch and protect "
                "nothing. See references/push-safety.md.",
                file=sys.stderr,
            )
            return EXIT_NEEDS_EXPECT
        if args.expect_remote != plan["upstream_sha"]:
            emit({**plan, "pushed": False, "error": "remote-moved"})
            print(
                f"gitwork: the remote moved since that plan was made "
                f"({args.expect_remote[:12]} -> {str(plan['upstream_sha'])[:12]}). "
                "Re-run push-plan and re-confirm: the commits a force would drop "
                "are no longer the ones the user agreed to drop.",
                file=sys.stderr,
            )
            return EXIT_NEEDS_FORCE
        git(
            repo,
            "push",
            f"--force-with-lease={safe_merge_ref(plan['merge_ref'])}:{safe_ref(args.expect_remote)}",
            safe_token(str(plan["remote"]), "remote"),
            f"HEAD:{safe_merge_ref(plan['merge_ref'])}",
            check=True,
        )
        sha = current_short_sha(repo)
        record_push(args, plan, sha)
        emit({**plan, "pushed": True, "forced": True})
        return 0

    die(f"unhandled push action: {action}")


# -- facts -------------------------------------------------------------------
def diffstat(repo: str, commit_hash: str | None, paths: list[str]) -> str:
    """The diffstat for whichever end state this run's files are actually in."""
    if commit_hash:
        _, out, _ = git(repo, "show", "--stat", "--format=", safe_ref(commit_hash), "--", *paths)
        # The summary line only ("N files changed, ..."): the summary renders one
        # row, and the per-file breakdown is already in the diff the user saw.
        return out.strip().splitlines()[-1].strip() if out.strip() else ""
    states = file_states(repo, paths)
    tracked = sorted(p for p, s in states.items() if s in ("staged", "modified"))
    fresh = sorted(p for p, s in states.items() if s == "untracked")
    parts = []
    if tracked and has_commits(repo):
        _, out, _ = git(repo, "diff", "HEAD", "--stat", "--", *tracked)
        if out:
            parts.append(out.strip().splitlines()[-1].strip())
    if fresh:
        lines = 0
        for rel in fresh:
            # Through the same no-follow reader as everywhere else, so a symlink
            # or FIFO cannot sneak in through the summary path.
            try:
                body = read_bytes_nofollow(os.path.join(repo, rel))
            except OSError:  # SymlinkRefused and NotARegularFile both subclass it
                continue
            # A final line without a trailing newline still counts.
            lines += body.count(b"\n") + (1 if body and not body.endswith(b"\n") else 0)
        parts.append(f"{len(fresh)} new file(s), {lines} lines")
    return "; ".join(parts)


def cmd_facts(args: argparse.Namespace) -> int:
    """Merge git-side facts into the JSON precommit.py produced.

    The only thing accepted from the caller here is --choice, which records a
    human answer no repository state can supply. Everything else is re-derived,
    including a --hash, which is verified rather than believed.
    """
    repo = args.dir
    refuse_facts_inside_repo(repo, args.facts, die)
    facts = load_facts(args.facts)
    paths = managed_paths(facts)
    if args.note:
        # Appended through the tool so the rest of the file is never rewritten
        # by hand -- a hand-merge is how computed fields get dropped.
        raw = facts.get("notes")
        prior = list(raw) if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
        facts["notes"] = [str(n) for n in prior] + args.note
    facts.setdefault("scan", {})["git_repo"] = is_repo(repo)
    # The choice is the user's answer, not a repository fact: record it even
    # when there is no repo, which is exactly when it says "not committed".
    commit = facts.setdefault("commit", {})
    if args.choice:
        commit["choice"] = args.choice
    if is_repo(repo):
        if args.hash:
            committed = commit_files(repo, args.hash)  # dies if the hash does not resolve
            extra = sorted(set(committed) - set(paths))
            if extra:
                die(
                    f"commit {args.hash} touches {extra} -- expected only "
                    f"{sorted(paths)}; refusing to record it as this run's commit"
                )
            # The same gate cmd_commit applies: a commit whose recorded content
            # is not what this run verified must not be presented as its result.
            for rel, expected in managed(facts):
                if not blob_matches_verified(repo, safe_ref(args.hash), rel, expected):
                    die(
                        f"commit {args.hash} recorded {rel} with content that is not "
                        "what this run wrote and verified; refusing to record it"
                    )
            _, raw_subject, _ = git(repo, "log", "-1", "--format=%s", safe_ref(args.hash))
            commit.setdefault("hash", args.hash)
            commit.setdefault("subject", clean(raw_subject))
            commit.setdefault("scope", commit_scope(paths))
        stat = diffstat(repo, args.hash, paths)
        if stat:
            facts.setdefault("net", {})["diffstat"] = stat

    save_facts(args.facts, facts)
    emit({"facts": args.facts, "merged": True})
    return 0


def main() -> int:
    # --dir is accepted both before and after the subcommand. Two parents,
    # because a subparser copy carrying its own default would silently overwrite
    # a value already parsed by the main parser.
    before = argparse.ArgumentParser(add_help=False)
    before.add_argument("--dir", default=".", help="repository root")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--dir", default=argparse.SUPPRESS, help="repository root"
    )  # SUPPRESS: only set when given, so the pre-subcommand value survives

    parser = argparse.ArgumentParser(description=__doc__, parents=[before], allow_abbrev=False)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def subcommand(name: str, **kwargs) -> argparse.ArgumentParser:
        """A subparser that inherits --dir and, crucially, allow_abbrev=False.

        add_parser does not inherit the parent's setting, so without this an
        abbreviated option would be accepted after the subcommand but not before.
        """
        return sub.add_parser(name, parents=[common], allow_abbrev=False, **kwargs)

    facts_help = "the run's facts JSON -- it names the files this run may touch"

    p = subcommand("status", help="which managed files changed, plus the real diff")
    p.add_argument("--facts", required=True, help=facts_help)

    p = subcommand("commit", help="commit ONLY this run's files from a message file")
    p.add_argument("--message-file", required=True)
    p.add_argument("--facts", required=True, help=facts_help)

    subcommand("push-plan", help="classify the push situation")

    p = subcommand("push", help="execute exactly what push-plan permits")
    p.add_argument("--confirm-force", action="store_true", help="required to force a diverged push")
    p.add_argument(
        "--expect-remote",
        metavar="SHA",
        help="the approved plan's upstream_sha; the force is leased against it",
    )
    p.add_argument("--remote", help="which remote, when the branch has no upstream")
    p.add_argument("--facts", help="also record the push line into this facts JSON")

    p = subcommand("facts", help="merge git facts into the run's facts JSON")
    p.add_argument("--facts", required=True, help=facts_help)
    p.add_argument(
        "--note",
        action="append",
        default=[],
        help="append a free-form note (repeatable); the only prose field",
    )
    p.add_argument("--hash", help="the commit this run produced (verified, not trusted)")
    p.add_argument(
        "--choice",
        choices=("commit + push", "commit only", "not committed"),
        help="the user's answer -- the one fact no repo state can supply",
    )

    args = parser.parse_args()
    if not os.path.isdir(args.dir):
        die(f"directory not found: {args.dir}")
    handlers = {
        "status": cmd_status,
        "commit": cmd_commit,
        "push-plan": cmd_push_plan,
        "push": cmd_push,
        "facts": cmd_facts,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
