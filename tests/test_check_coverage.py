"""The per-file coverage gate.

It decides whether the build passes, and had no tests: an inverted comparison or
a broken JSON path would have silently stopped gating while still printing a
reassuring table.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_coverage as CC


def fake_report(files: dict[str, float]) -> str:
    return json.dumps({"files": {p: {"summary": {"percent_covered": v}} for p, v in files.items()}})


@pytest.fixture
def measured(monkeypatch):
    """Replace the `coverage json` subprocess with a canned report."""

    def _set(files: dict[str, float], returncode: int = 0):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, returncode, fake_report(files), "boom")

        monkeypatch.setattr(CC.subprocess, "run", fake_run)

    return _set


def run_main(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr("sys.argv", ["check_coverage.py", *argv])
    return CC.main()


def test_every_file_at_or_above_the_floor_passes(measured, monkeypatch, capsys):
    measured({"a.py": 100.0, "b.py": 90.0})
    assert run_main(monkeypatch, ["--min", "90"]) == 0
    assert "every file at or above 90%" in capsys.readouterr().out


def test_exactly_at_the_floor_is_not_below_it(measured, monkeypatch):
    """The boundary is the whole point of naming the number."""
    measured({"a.py": 90.0})
    assert run_main(monkeypatch, ["--min", "90"]) == 0


def test_one_file_below_the_floor_fails(measured, monkeypatch, capsys):
    measured({"good.py": 99.0, "thin.py": 89.9})
    assert run_main(monkeypatch, ["--min", "90"]) == 1
    out = capsys.readouterr()
    assert "thin.py" in out.err
    assert "FAIL" in out.out
    assert "1 file(s) below 90%" in out.err


def test_an_empty_report_is_a_failure_not_a_pass(measured, monkeypatch):
    """Almost always MP_COVER_SUBPROCESS unset -- the suite runs the scripts as
    subprocesses, which a plain coverage run cannot see at all. Reporting that
    as success would gate nothing while looking green."""
    measured({})
    with pytest.raises(SystemExit) as exit_info:
        run_main(monkeypatch, ["--min", "90"])
    assert "did the suite run?" in str(exit_info.value)


def test_a_failed_coverage_json_stops_rather_than_guesses(measured, monkeypatch):
    measured({"a.py": 100.0}, returncode=1)
    with pytest.raises(SystemExit) as exit_info:
        run_main(monkeypatch, ["--min", "90"])
    assert "coverage json` failed" in str(exit_info.value)


def test_the_default_floor_is_ninety(measured, monkeypatch):
    measured({"a.py": 89.0})
    assert run_main(monkeypatch, []) == 1
