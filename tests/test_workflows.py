"""The workflow files, held to the two properties that are easy to lose.

Both were one-time cleanups until this file existed. A cleanup with nothing
watching it drifts back the first time somebody adds a job or an action.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))
SHA = re.compile(r"^[0-9a-f]{40}$")


def test_there_are_workflows_to_check():
    """A glob that matches nothing makes every test below vacuously true."""
    assert WORKFLOWS, "no workflow files found"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit(workflow):
    """A tag is a mutable ref, and these jobs gate a publish to PyPI. The pin is
    the only part an attacker cannot move out from under the release."""
    unpinned = []
    for line in workflow.read_text().splitlines():
        match = re.match(r"\s*-?\s*uses:\s*(\S+)", line)
        if not match:
            continue
        ref = match.group(1)
        if ref.startswith("./"):
            continue  # a local reusable workflow: same repository, same commit
        _, _, version = ref.partition("@")
        if not SHA.match(version):
            unpinned.append(ref)
    assert not unpinned, f"not pinned to a commit SHA: {unpinned}"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_pin_still_says_which_version_it_was(workflow):
    """A bare 40-character hex string is unreadable. The trailing comment is how
    a human reviews the diff when Dependabot proposes a bump."""
    for line in workflow.read_text().splitlines():
        if re.match(r"\s*-?\s*uses:\s*\S+@[0-9a-f]{40}", line):
            assert "#" in line.split("@", 1)[1], f"pin with no version comment: {line.strip()}"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_that_runs_on_a_runner_is_time_bounded(workflow):
    """An unbounded job is a runner that hangs until GitHub's own limit. The
    suite here drives real git repositories and external binaries, which is
    exactly the shape that hangs rather than fails."""
    text = workflow.read_text()
    runners = text.count("\n    runs-on:")
    bounds = text.count("\n    timeout-minutes:")
    assert runners == bounds, f"{runners} jobs with a runner, {bounds} with a timeout"
