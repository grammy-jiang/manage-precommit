"""Reading pre-commit's output.

Pure text in, verdict out -- which is why it is worth its own module and its own
tests. Two of these judgements decide whether a run that printed "Passed" is
reported as a pass at all.
"""

from __future__ import annotations

import pytest

import hookoutput as H

PASSED = "trailing-whitespace..................................................Passed"
FAILED = "gitleaks.............................................................Failed"
SKIPPED = "markdownlint-cli2...............................(no files to check)Skipped"


def test_a_run_where_every_hook_had_nothing_to_check_is_vacuous():
    """--all-files covers only TRACKED files, so in a repo where the setup files
    are still untracked every hook skips and the command exits 0. Reporting that
    as success is the failure this catches."""
    assert H.is_vacuous(f"{SKIPPED}\n{SKIPPED}\n") is True


def test_one_hook_with_something_to_do_is_enough():
    assert H.is_vacuous(f"{SKIPPED}\n{PASSED}\n") is False


def test_output_with_no_parseable_result_line_counts_as_vacuous():
    """No hooks configured, a wrapper that swallowed the output, a shape this
    cannot read: whatever the cause, nothing was OBSERVED to run."""
    assert H.is_vacuous("") is True
    assert H.is_vacuous("some unrelated chatter\n") is True


def test_a_failure_is_not_vacuous():
    assert H.is_vacuous(f"{FAILED}\n") is False


def test_skipped_hooks_names_only_the_ones_that_saw_nothing():
    names = H.skipped_hooks(f"{PASSED}\n{SKIPPED}\n{FAILED}\n")
    assert names == ["markdownlint-cli2"]


def test_skipped_hooks_is_empty_when_everything_ran():
    assert H.skipped_hooks(f"{PASSED}\n{FAILED}\n") == []


@pytest.mark.parametrize("line", ["", "no dots here Passed", "....Passed"])
def test_lines_that_are_not_hook_results_are_ignored(line):
    assert H.skipped_hooks(line) == []
