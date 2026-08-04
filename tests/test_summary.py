"""The closing summary.

Everything rendered here came from a config file, a filename or a remote, so
the interesting tests are the ones where that text is hostile.
"""

from __future__ import annotations

import json

import pytest

import summary
from conftest import out_json, run  # noqa: F401  (run is used below)
from conftest import run as run_script

FULL = {
    "scan": {
        "git_repo": True,
        "config": "existing",
        "prev_repos": 2,
        "detected": ["markdown (README.md)"],
    },
    "hooks": {
        "added": ["https://github.com/gitleaks/gitleaks: added (rev v8.30.1)"],
        "left_as_is": ["https://github.com/psf/black: already present -- left as-is"],
        "recommended": [{"name": "markdownlint", "reason": "README.md"}],
        "versions": {"gitleaks": "v8.30.1"},
    },
    "files": {"written": [".pre-commit-config.yaml"], "kept": [".yamllint.yaml"]},
    "verify": {"install": "git hook installed", "run": "passed", "run_ok": True, "vacuous": False},
    "commit": {
        "choice": "commit + push",
        "hash": "abc1234",
        "subject": "chore: add pre-commit hooks",
        "scope": "1 pre-commit setup file only",
        "untouched": "2 other files",
        "push": {"sha": "abc1234", "remote": "origin", "branch": "main"},
    },
    "net": {"prev_repos": 2, "new_repos": 3, "delta": "+gitleaks", "diffstat": "1 file changed"},
}


def render(facts: dict) -> str:
    return summary.render(facts, summary.Pal(False))


def test_renders_every_section():
    text = render(FULL)
    for header in ("SCAN", "HOOKS", "FILES", "VERIFY", "COMMIT", "NET"):
        assert header in text


def test_empty_sections_are_skipped():
    text = render({"commit": {"choice": "not committed"}})
    assert "COMMIT" in text
    assert "SCAN" not in text
    assert "HOOKS" not in text


def test_the_push_row_is_composed_from_its_pieces():
    """gitwork stores {sha, remote, branch} rather than a sentence precisely so
    this file owns the wording."""
    assert "abc1234 -> origin/main" in render(FULL)


def test_no_push_reads_as_not_pushed():
    facts = json.loads(json.dumps(FULL))
    del facts["commit"]["push"]
    assert "not pushed" in render(facts)


def test_a_vacuous_verify_is_not_reported_as_a_pass():
    facts = json.loads(json.dumps(FULL))
    facts["verify"] = {
        "install": "git hook installed",
        "run": "vacuous pass -- every hook reported (no files to check).",
        "run_ok": False,
        "vacuous": True,
    }
    coloured = summary.render(facts, summary.Pal(True))
    # 33 is the warning colour; 32 would be the green used for a real pass.
    assert "\033[33m" in coloured
    assert "\033[32mvacuous" not in coloured


# -- forgery -----------------------------------------------------------------


def test_a_newline_in_a_value_cannot_forge_an_extra_row():
    """A repo can name a hook anything. Without neutralising, this would print
    a second, entirely fabricated `push` row."""
    facts = {
        "hooks": {"added": ["evil\n  push        FORGED -> attacker/main"]},
        "commit": {"choice": "not committed"},
    }
    text = render(facts)
    push_rows = [ln for ln in text.splitlines() if ln.strip().startswith("push")]
    assert len(push_rows) == 1
    assert "not pushed" in push_rows[0]
    assert "FORGED" in text  # shown, but flattened onto the row it belongs to


def test_an_escape_sequence_never_reaches_the_output():
    facts = {"hooks": {"added": ["evil\x1b[31mred\x1b[0m"]}}
    text = render(facts)
    assert "\x1b" not in text


def test_colour_off_and_on_produce_the_same_layout():
    plain = render(FULL)
    coloured = summary.render(FULL, summary.Pal(True))
    import re

    stripped = re.sub(r"\033\[[0-9;]*m", "", coloured)
    assert stripped == plain


# -- the CLI -----------------------------------------------------------------


def test_cli_renders_a_facts_file(tmp_path):
    p = tmp_path / "facts.json"
    p.write_text(json.dumps(FULL))
    proc = run_script("summary.py", str(p))
    assert proc.returncode == 0
    assert "manage-precommit - run summary" in proc.stdout
    assert "\x1b" not in proc.stdout  # not a TTY, so no colour


def test_cli_rejects_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    proc = run_script("summary.py", str(p))
    assert proc.returncode == 1
    assert "invalid JSON" in proc.stderr


def test_cli_rejects_a_json_scalar(tmp_path):
    p = tmp_path / "scalar.json"
    p.write_text("42")
    proc = run_script("summary.py", str(p))
    assert proc.returncode == 1
    assert "JSON object" in proc.stderr


@pytest.mark.parametrize("value", ["1", "true"])
def test_no_color_env_disables_colour(monkeypatch, value):
    monkeypatch.setenv("NO_COLOR", value)
    assert summary.use_color("auto") is False
