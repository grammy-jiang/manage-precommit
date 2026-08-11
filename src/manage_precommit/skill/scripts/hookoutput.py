#!/usr/bin/env python3
"""Reading `pre-commit`'s output -- and nothing else.

Split out of precommit.py, which had grown two unrelated jobs: deciding what
belongs in a .pre-commit-config.yaml, and interpreting what the external
pre-commit binary printed afterwards. This half is pure: text in, verdict out,
no git, no filesystem, no network. That is what makes it worth having on its
own, and what makes it cheap to test directly.

The judgements here are the reason a program does this rather than a paragraph
of prose: two outcomes look like success and are not, and both are decided by
pattern-matching output that a human would skim straight past.
"""

from __future__ import annotations

import re

SKIPPED_NO_FILES = re.compile(r"\(no files to check\)\s*Skipped")
HOOK_RESULT_LINE = re.compile(r"\.{3,}.*\b(Passed|Failed|Skipped)\b")


def skipped_hooks(output: str) -> list[str]:
    r"""Hooks that ran but reported they had no files to check.

    is_vacuous() is all-or-nothing: one hook with something to do flips it off.
    But hygiene's hooks match any file, so they alone turn a run green while
    markdownlint and mermaid -- filtered to `\.(md|markdown)$`, and added
    precisely because a .md was detected -- are still checking nothing. That is
    a partial vacuity the whole-output verdict cannot express.
    """
    names = []
    for line in output.splitlines():
        if HOOK_RESULT_LINE.search(line) and SKIPPED_NO_FILES.search(line):
            names.append(re.split(r"\.{3,}", line, maxsplit=1)[0].strip())
    return names


def is_vacuous(output: str) -> bool:
    """True when every hook that ran reported it had nothing to check.

    `pre-commit run --all-files` covers only git-*tracked* files, so in a repo
    where the setup files are still untracked every hook prints "(no files to
    check) Skipped" and the command exits 0. That is a pass which tested
    nothing, and reporting it as success is the failure this catches.
    """
    results = [ln for ln in output.splitlines() if HOOK_RESULT_LINE.search(ln)]
    if not results:
        # No parseable result line at all -- no hooks configured, output in a
        # shape this cannot read, a wrapper that swallowed it. Whatever the
        # cause, nothing was observed to run, and reporting that as a clean
        # pass is precisely the false positive this function exists to catch.
        return True
    return all(SKIPPED_NO_FILES.search(ln) for ln in results)
