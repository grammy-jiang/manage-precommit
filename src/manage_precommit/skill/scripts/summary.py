#!/usr/bin/env python3
"""Render the manage-precommit skill's end-of-run summary.

Reads the run's facts file and prints a categorised, aligned summary. Colourised
on an interactive terminal; plain text (no ANSI) when piped or redirected, with
identical alignment either way.

Every value here was computed and written by precommit.py or gitwork.py. Nothing
in this file derives a number, and nothing upstream of it should hand-format one:
this output *is* the closing summary, and a second hand-written one beside it is
two answers to the same question.

The shape being read is ``shared.Facts``; that TypedDict is the contract, and
mypy checks both ends against it. Sections with nothing in them are skipped.

Colour is ON when stdout is a TTY, TERM != "dumb", and NO_COLOR is unset.
Override with --color=always|never|auto (default auto), FORCE_COLOR or NO_COLOR.

Usage:
  summary.py FACTS.json [--color auto|always|never]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from shared import (
    NotARegularFile,
    SymlinkRefused,
    TooLarge,
    clean,
    read_bytes_nofollow,
)


def use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"


class Pal:
    """ANSI palette; a no-op when colour is off, so alignment is identical."""

    def __init__(self, on: bool) -> None:
        self.on = on

    def _w(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def title(self, s: str) -> str:
        return self._w(s, "1;36")

    def rule(self, s: str) -> str:
        return self._w(s, "36")

    def hdr(self, s: str) -> str:
        return self._w(s, "1;37")

    def label(self, s: str) -> str:
        return self._w(s, "36")

    def dim(self, s: str) -> str:
        return self._w(s, "2")

    def hashc(self, s: str) -> str:
        return self._w(s, "33")

    def add(self, s: str) -> str:
        return self._w(s, "32")

    def rem(self, s: str) -> str:
        return self._w(s, "31")

    def ok(self, s: str) -> str:
        return self._w(s, "32")

    def warn(self, s: str) -> str:
        return self._w(s, "33")


def emit_section(lines: list[str], header: str, rows: list[tuple[str, object]], pal: Pal) -> None:
    kept = [(label, value) for label, value in rows if value is not None]
    if not kept:
        return
    lines.append("")
    lines.append(pal.hdr(header))
    width = max(len(label) for label, _ in kept)
    for label, value in kept:
        lines.append(f"  {pal.label(label.ljust(width))}  {value}")


def names(items: object, pal: Pal, kind: str | None = None) -> str:
    """A list of repo-derived names, neutralised and joined.

    Everything here came from a config file, a filename or a remote, so it all
    goes through `clean`: a name carrying an ESC or a bidi override could
    otherwise forge a row of this summary, and the summary is what the user
    reads to believe what happened.
    """
    if not isinstance(items, (list, tuple)):
        items = [items] if items else []
    if not items:
        return pal.dim("(none)")
    if kind == "add":
        return ", ".join(pal.add(clean(n)) for n in items)
    if kind == "dim":
        return ", ".join(pal.dim(clean(n)) for n in items)
    if kind == "rem":
        return ", ".join(pal.rem(clean(n)) for n in items)
    return ", ".join(clean(n) for n in items)


def color_diffstat(text: object, pal: Pal) -> str:
    """Colour the counts in a diffstat line.

    Targeted at the shapes gitwork.diffstat actually produces, which is the
    whole point: it colours git's summary line

        1 file changed, 4 insertions(+), 2 deletions(-)

    and the hand-built "N new file(s), M lines" for an untracked write. The
    original patterns looked for `+12` / `-3` -- a sign immediately before the
    digits -- and neither shape has ever contained one, so the feature was dead
    on every real invocation while looking implemented.
    """
    out = clean(text)
    out = re.sub(r"\d+(?= insertion| new file| line)", lambda m: pal.add(m.group()), out)
    out = re.sub(r"\d+(?= deletion)", lambda m: pal.rem(m.group()), out)
    return out


def push_line(push: object, pal: Pal) -> str | None:
    """Compose the push row from its pieces.

    gitwork records where a push landed as {sha, remote, branch} rather than a
    sentence, precisely so this file owns the wording -- the same reason the
    commit hash and subject are stored apart.
    """
    if not isinstance(push, dict) or not push.get("sha"):
        return None
    sha = pal.hashc(clean(push["sha"]))
    remote = clean(push.get("remote", ""))
    branch = clean(push.get("branch", ""))
    dest = f"{remote}/{branch}".strip("/")
    line = f"{sha} -> {dest}" if dest else sha
    if push.get("forced"):
        dropped = push.get("dropped") or 0
        line += f"  {pal.rem('FORCED')}"
        if dropped:
            line += pal.rem(f" -- dropped {dropped} remote commit(s)")
    return line


def render(facts: dict, pal: Pal) -> str:
    lines: list[str] = []
    title = clean(facts.get("title", "manage-precommit - run summary"))
    lines.append(pal.title(title))
    lines.append(pal.rule("=" * len(title)))

    raw_notes = facts.get("notes") or []
    if not isinstance(raw_notes, (list, tuple)):
        raw_notes = [raw_notes]
    notes = [clean(n) for n in raw_notes if n]
    if notes:
        lines.append("")
        lines.append(pal.hdr("NOTES"))
        for note in notes:
            lines.append(f"  {pal.dim('*')} {note}")

    scan = facts.get("scan") or {}
    if scan:
        config = scan.get("config", "none")
        if config == "existing":
            config_value = f"existing -- {scan.get('prev_repos', '?')} repos"
        elif config == "fresh":
            config_value = "none yet (fresh file)"
        else:
            config_value = "none"
        emit_section(
            lines,
            "SCAN",
            [
                ("repo", "git repository" if scan.get("git_repo") else pal.dim("not a git repo")),
                (".pre-commit", config_value),
                ("detected", names(scan.get("detected"), pal)),
            ],
            pal,
        )

    hooks = facts.get("hooks") or {}
    if hooks:
        recommended = hooks.get("recommended") or []
        rec_value = None
        if recommended:
            parts = []
            # Every recommendation needs a visible outcome. Without one a reader
            # cannot tell "the user said no" from "it was silently dropped" --
            # in the artefact SKILL.md calls the closing summary.
            chosen = {clean(n) for n in (hooks.get("selected") or [])}
            for item in recommended:
                if isinstance(item, dict):
                    name = clean(item.get("name", ""))
                    reason = item.get("reason")
                    part = f"{name}  {pal.dim('<- ' + clean(reason))}" if reason else name
                    if chosen and name not in chosen:
                        part += f"  {pal.dim('(declined)')}"
                    parts.append(part)
                else:
                    parts.append(clean(item))
            indent = " " * (2 + len("recommended") + 2)
            rec_value = f"\n{indent}".join(parts)
        versions = hooks.get("versions") or {}
        ver_value = ", ".join(f"{clean(k)}={clean(v)}" for k, v in versions.items()) or None
        emit_section(
            lines,
            "HOOKS",
            [
                ("added", names(hooks.get("added"), pal, "add")),
                ("left as-is", names(hooks.get("left_as_is"), pal, "dim")),
                (
                    "add by hand",
                    names(hooks["needs_manual"], pal, "rem") if hooks.get("needs_manual") else None,
                ),
                (
                    "present but off",
                    names(hooks["disabled"], pal, "rem") if hooks.get("disabled") else None,
                ),
                ("recommended", rec_value),
                ("versions", ver_value),
            ],
            pal,
        )

    files = facts.get("files") or {}
    if files:
        emit_section(
            lines,
            "FILES",
            [
                ("written", names(files.get("written"), pal, "add")),
                ("kept", names(files.get("kept"), pal, "dim")),
            ],
            pal,
        )

    verify = facts.get("verify") or {}
    if verify:
        run = verify.get("run")
        if run is None:
            run_value = None
        elif verify.get("vacuous"):
            # Not a pass and not a failure: the hooks never saw a file. Coloured
            # as a warning so it cannot be skimmed as green.
            run_value = pal.warn(clean(run))
        elif verify.get("run_ok", True):
            run_value = pal.ok(clean(run))
        else:
            run_value = pal.rem(clean(run))
        autofixed = verify.get("autofixed") or []
        emit_section(
            lines,
            "VERIFY",
            [
                ("install", clean(verify["install"]) if verify.get("install") else None),
                (
                    "scope",
                    {
                        "all-files": "every tracked file",
                        "these-files": "only this run's files -- says nothing about the rest",
                    }.get(str(verify.get("scope"))),
                ),
                ("run", run_value),
                (
                    "unchecked",
                    names(verify["unchecked"], pal, "rem") if verify.get("unchecked") else None,
                ),
                ("autofixed", names(autofixed, pal, "dim") if autofixed else None),
            ],
            pal,
        )

    commit = facts.get("commit") or {}
    if commit:
        rows: list[tuple[str, object]] = [("choice", clean(commit.get("choice", "not committed")))]
        if commit.get("hash"):
            subject = clean(commit.get("subject", ""))
            rows.append(("commit", f"{pal.hashc(clean(commit['hash']))}  {subject}"))
        if commit.get("scope"):
            untouched = commit.get("untouched")
            scope = clean(commit["scope"])
            if untouched:
                scope += f"  {pal.dim('(' + clean(untouched) + ' untouched)')}"
            rows.append(("scope", scope))
            if commit.get("untouched_files"):
                rows.append(("untouched", names(commit["untouched_files"], pal, "dim")))
        rows.append(("push", push_line(commit.get("push"), pal) or pal.dim("not pushed")))
        emit_section(lines, "COMMIT", rows, pal)

    net = facts.get("net") or {}
    if net:
        rows = []
        if net.get("prev_repos") is not None and net.get("new_repos") is not None:
            delta = clean(net.get("delta", ""))
            rows.append(
                ("repos", f"{net['prev_repos']} -> {net['new_repos']}  {pal.dim(delta)}".rstrip())
            )
        if net.get("diffstat"):
            rows.append(("diff", color_diffstat(net["diffstat"], pal)))
        emit_section(lines, "NET", rows, pal)

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render the manage-precommit run summary.", allow_abbrev=False
    )
    parser.add_argument("facts", nargs="?", help="path to the run's facts JSON (else stdin)")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    args = parser.parse_args()

    if args.facts:
        # Same guarded reader as the rest of the skill: this path is chosen by
        # the agent, so it is inside the trust boundary everything else is.
        try:
            raw = read_bytes_nofollow(args.facts).decode("utf-8", "replace")
        except (SymlinkRefused, NotARegularFile, TooLarge) as exc:
            print(f"summary: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"summary: cannot read {args.facts}: {exc}", file=sys.stderr)
            return 1
    else:
        # Explicit, like every other read of this JSON. Relying on the locale's
        # text decoding raises an uncaught UnicodeDecodeError in a non-UTF-8
        # locale, or silently mangles non-ASCII.
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        facts = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"summary: invalid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(facts, dict):
        print("summary: facts file must contain a JSON object", file=sys.stderr)
        return 1

    print(render(facts, Pal(use_color(args.color))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
