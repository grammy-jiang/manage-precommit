#!/usr/bin/env python3
"""Render the manage-precommit skill's end-of-run summary.

Reads a JSON facts file and prints a categorized, aligned summary. Colorized on
an interactive terminal; plain text (no ANSI) when piped/redirected.

Color is ON when: stdout is a TTY, TERM != "dumb", and NO_COLOR is unset.
Override with --color=always|never|auto (default auto) or FORCE_COLOR / NO_COLOR.

Usage:
  render-summary.py FACTS.json [--color auto|always|never]

FACTS schema (all fields optional; empty sections are skipped):
{
  "title": "manage-precommit - run summary",
  "notes": ["free-form context shown near the top"],
  "scan":  {"git_repo": true, "config": "existing|fresh|none",
            "prev_repos": 2, "detected": ["markdown (README.md)"]},
  "hooks": {"added": ["pre-commit-hooks (7)", "local mermaid-lint"],
            "left_as_is": ["markdownlint-cli2 (rev v0.14.0)", "psf/black"],
            "recommended": [{"name": "markdownlint", "reason": "*.md"}],
            "versions": {"hygiene": "v6.0.0"}},
  "files": {"written": [".pre-commit-config.yaml", ".yamllint.yaml"],
            "kept": [".markdownlint.yaml"]},
  "verify": {"install": "git hook installed", "run": "passed", "run_ok": true},
  "commit": {"choice": "commit + push", "hash": "abc1234", "subject": "chore: ...",
             "scope": ".pre-commit files only", "untouched": "other changes",
             "push": "abc..def -> origin/master"},
  "net":   {"prev_repos": 2, "new_repos": 5, "delta": "+hygiene +mermaid",
            "diffstat": "+30 / -0"}
}

Keep values short; put longer context in "notes".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys


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
    """ANSI palette; a no-op when color is off, so alignment is identical."""

    def __init__(self, on: bool) -> None:
        self.on = on

    def _w(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def title(self, s):
        return self._w(s, "1;36")

    def rule(self, s):
        return self._w(s, "36")

    def hdr(self, s):
        return self._w(s, "1;37")

    def label(self, s):
        return self._w(s, "36")

    def dim(self, s):
        return self._w(s, "2")

    def hashc(self, s):
        return self._w(s, "33")

    def add(self, s):
        return self._w(s, "32")

    def rem(self, s):
        return self._w(s, "31")

    def ok(self, s):
        return self._w(s, "32")


CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(value) -> str:
    """Stringify and strip control chars so facts text can't forge the output."""
    return CONTROL_CHARS.sub("", value if isinstance(value, str) else str(value))


def emit_section(lines, header, rows, pal):
    rows = [r for r in rows if r[1] is not None]
    if not rows:
        return
    lines.append("")
    lines.append(pal.hdr(header))
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        lines.append(f"  {pal.label(label.ljust(width))}  {value}")


def names(items, pal, kind=None):
    if not isinstance(items, (list, tuple)):
        items = [items] if items else []
    if not items:
        return pal.dim("(none)")
    if kind == "add":
        return ", ".join(pal.add(clean(n)) for n in items)
    if kind == "dim":
        return ", ".join(pal.dim(clean(n)) for n in items)
    return ", ".join(clean(n) for n in items)


def color_diffstat(text, pal):
    text = clean(text)
    text = re.sub(r"\+\d+", lambda m: pal.add(m.group()), text)
    text = re.sub(r"(?<!\w)-\d+", lambda m: pal.rem(m.group()), text)
    return text


def render(facts, pal):
    lines = []
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
            lines.append(f"  {pal.dim('•')} {note}")

    # SCAN
    scan = facts.get("scan", {})
    if scan:
        cfg = scan.get("config", "none")
        if cfg == "existing":
            cfg_val = f"existing — {scan.get('prev_repos', '?')} repos"
        elif cfg == "fresh":
            cfg_val = "none yet (fresh file)"
        else:
            cfg_val = "none"
        emit_section(
            lines, "SCAN",
            [
                ("repo", "git repository" if scan.get("git_repo") else pal.dim("not a git repo")),
                (".pre-commit", cfg_val),
                ("detected", names(scan.get("detected"), pal)),
            ],
            pal,
        )

    # HOOKS
    hooks = facts.get("hooks", {})
    if hooks:
        rec = hooks.get("recommended") or []
        rec_val = None
        if rec:
            parts = []
            for item in rec:
                if isinstance(item, dict):
                    reason = item.get("reason")
                    name = clean(item.get("name", ""))
                    parts.append(f"{name}  {pal.dim('← ' + clean(reason))}" if reason else name)
                else:
                    parts.append(clean(item))
            indent = " " * (2 + len("recommended") + 2)
            rec_val = f"\n{indent}".join(parts) if len(parts) > 1 else parts[0]
        versions = hooks.get("versions") or {}
        ver_val = ", ".join(f"{clean(k)}={clean(v)}" for k, v in versions.items()) or None
        emit_section(
            lines, "HOOKS",
            [
                ("added", names(hooks.get("added"), pal, "add")),
                ("left as-is", names(hooks.get("left_as_is"), pal, "dim")),
                ("recommended", rec_val),
                ("versions", ver_val),
            ],
            pal,
        )

    # FILES
    files = facts.get("files", {})
    if files:
        emit_section(
            lines, "FILES",
            [
                ("written", names(files.get("written"), pal, "add")),
                ("kept", names(files.get("kept"), pal, "dim")),
            ],
            pal,
        )

    # VERIFY
    verify = facts.get("verify", {})
    if verify:
        run = verify.get("run")
        if run is not None:
            run_val = pal.ok(clean(run)) if verify.get("run_ok", True) else pal.rem(clean(run))
        else:
            run_val = None
        emit_section(
            lines, "VERIFY",
            [
                ("install", clean(verify["install"]) if verify.get("install") else None),
                ("run", run_val),
            ],
            pal,
        )

    # COMMIT
    commit = facts.get("commit", {})
    if commit:
        rows = [("choice", clean(commit.get("choice", "not committed")))]
        if commit.get("hash"):
            rows.append(("commit", f"{pal.hashc(clean(commit['hash']))}  {clean(commit.get('subject', ''))}"))
        if commit.get("scope"):
            untouched = commit.get("untouched")
            scope = clean(commit["scope"]) + (f"  {pal.dim('(' + clean(untouched) + ' untouched)')}" if untouched else "")
            rows.append(("scope", scope))
        rows.append(("push", clean(commit["push"]) if commit.get("push") else pal.dim("not pushed")))
        emit_section(lines, "COMMIT", rows, pal)

    # NET
    net = facts.get("net", {})
    if net:
        rows = []
        if net.get("prev_repos") is not None and net.get("new_repos") is not None:
            delta = clean(net.get("delta", ""))
            rows.append(("repos", f"{net['prev_repos']} → {net['new_repos']}  {pal.dim(delta)}".rstrip()))
        if net.get("diffstat"):
            rows.append(("diff", color_diffstat(net["diffstat"], pal)))
        emit_section(lines, "NET", rows, pal)

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Render manage-precommit run summary.")
    parser.add_argument("facts", nargs="?", help="path to JSON facts file (else stdin)")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    args = parser.parse_args()

    raw = open(args.facts, encoding="utf-8").read() if args.facts else sys.stdin.read()
    try:
        facts = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"render-summary: invalid JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    print(render(facts, Pal(use_color(args.color))))


if __name__ == "__main__":
    main()
