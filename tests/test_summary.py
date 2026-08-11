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


def test_cli_refuses_a_symlinked_facts_path(tmp_path):
    """The facts path is agent-chosen, so it sits inside the same trust
    boundary every other read in the skill defends. summary.py used a plain
    open(), which follows the link and has no size bound."""
    secret = tmp_path / "secret"
    secret.write_text(json.dumps(FULL))
    link = tmp_path / "facts.json"
    link.symlink_to(secret)
    proc = run_script("summary.py", str(link))
    assert proc.returncode == 1
    assert "symlink" in proc.stderr
    assert "manage-precommit - run summary" not in proc.stdout


def test_cli_refuses_an_oversized_facts_file(tmp_path):
    big = tmp_path / "big.json"
    big.write_text('{"notes": ["' + "x" * 5_000_000 + '"]}')
    proc = run_script("summary.py", str(big))
    assert proc.returncode == 1
    assert "larger than" in proc.stderr


def test_a_forced_push_is_visible_in_the_summary():
    facts = json.loads(json.dumps(FULL))
    facts["commit"]["push"] = {
        "sha": "abc1234",
        "remote": "origin",
        "branch": "main",
        "forced": True,
        "dropped": 2,
    }
    text = render(facts)
    assert "FORCED" in text
    assert "dropped 2 remote commit(s)" in text


def test_an_ordinary_push_says_nothing_about_forcing():
    assert "FORCED" not in render(FULL)


def test_hooks_needing_manual_addition_are_shown():
    """SKILL.md makes this a "say so plainly" outcome, and the summary is the
    durable record -- but nothing asserted it was ever rendered."""
    facts = json.loads(json.dumps(FULL))
    facts["hooks"]["needs_manual"] = [
        "https://github.com/psf/black: present (rev 24.1.0) but its `hooks:` list is not "
        "a shape this tool can extend -- add black-jupyter by hand"
    ]
    text = render(facts)
    assert "add by hand" in text
    assert "black-jupyter" in text


def test_a_hook_that_never_saw_a_file_is_shown():
    """verify.unchecked was computed and persisted specifically to catch a
    green run that exercised nothing -- and then dropped before the summary,
    so a resumed Step 6 lost the warning entirely."""
    facts = json.loads(json.dumps(FULL))
    facts["verify"]["unchecked"] = ["markdownlint-cli2"]
    text = render(facts)
    assert "unchecked" in text
    assert "markdownlint-cli2" in text


def test_a_clean_verify_shows_no_unchecked_row():
    assert "unchecked" not in render(FULL)


def test_the_untouched_files_appear_in_the_summary():
    facts = json.loads(json.dumps(FULL))
    facts["commit"]["untouched_files"] = ["a.txt", "b.txt"]
    text = render(facts)
    assert "untouched" in text
    assert "a.txt, b.txt" in text


def test_a_declined_recommendation_says_so():
    """Without a disposition a reader cannot tell "the user said no" from
    "silently dropped" in the artefact that IS the closing summary."""
    facts = json.loads(json.dumps(FULL))
    facts["hooks"]["recommended"] = [
        {"name": "markdownlint", "reason": "README.md"},
        {"name": "gitleaks", "reason": "any repo -- secret scan"},
    ]
    facts["hooks"]["selected"] = ["gitleaks"]
    text = render(facts)
    assert "markdownlint" in text
    assert "(declined)" in text
    line = next(ln for ln in text.splitlines() if "markdownlint" in ln)
    assert "(declined)" in line
    gitleaks_line = next(ln for ln in text.splitlines() if "gitleaks" in ln and "<-" in ln)
    assert "(declined)" not in gitleaks_line


def test_a_present_but_disabled_entry_is_shown():
    facts = json.loads(json.dumps(FULL))
    facts["hooks"]["disabled"] = ["gitleaks"]
    text = render(facts)
    assert "present but off" in text
    assert "gitleaks" in text


def test_the_verify_scope_qualifies_what_passed():
    narrow = json.loads(json.dumps(FULL))
    narrow["verify"]["scope"] = "these-files"
    assert "says nothing about the rest" in render(narrow)

    wide = json.loads(json.dumps(FULL))
    wide["verify"]["scope"] = "all-files"
    assert "every tracked file" in render(wide)


# -- the shapes facts.json can arrive in --------------------------------------
#
# This file renders whatever the other four scripts wrote, and every optional
# key is a branch. Untested, a rename upstream turns a whole section into
# silence -- which reads exactly like "there was nothing to report".


@pytest.mark.parametrize(
    ("mode", "env", "expected"),
    [
        ("always", {}, True),
        ("always", {"NO_COLOR": "1"}, True),  # explicit beats the environment
        ("never", {}, False),
        ("never", {"FORCE_COLOR": "1"}, False),
        ("auto", {"NO_COLOR": "1"}, False),
        ("auto", {"NO_COLOR": ""}, False),  # NO_COLOR is set-or-not, not truthy
        ("auto", {"FORCE_COLOR": "1"}, True),
        ("auto", {}, False),  # not a tty under pytest
        ("auto", {"TERM": "dumb"}, False),
    ],
)
def test_when_colour_is_used(mode, env, expected, monkeypatch):
    for var in ("NO_COLOR", "FORCE_COLOR", "TERM"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    assert summary.use_color(mode) is expected


def test_a_section_whose_every_row_is_empty_prints_no_header():
    """Otherwise a bare "VERIFY" heading with nothing under it reads as a bug --
    and this section in particular is one a reader checks for reassurance."""
    assert "VERIFY" not in render({"verify": {"skipped": []}})


def test_a_single_note_need_not_be_a_list():
    assert "NOTES" in render({"notes": "the config was already up to date"})
    assert "up to date" in render({"notes": "the config was already up to date"})


def test_notes_are_listed():
    text = render({"notes": ["first thing", "second thing"]})
    assert "first thing" in text
    assert "second thing" in text


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ("existing", "existing -- 2 repos"),
        ("fresh", "none yet (fresh file)"),
        ("none", "none"),
        (None, "none"),
    ],
)
def test_the_prior_config_state_is_spelled_out(config, expected):
    scan = {"git_repo": True, "prev_repos": 2}
    if config is not None:
        scan["config"] = config
    assert expected in render({"scan": scan})


def test_a_directory_that_is_not_a_git_repository_says_so():
    assert "not a git repo" in render({"scan": {"git_repo": False}})


def test_a_recommendation_may_be_a_bare_name():
    """precommit.py writes dicts; nothing stops a caller writing strings, and a
    TypeError here would lose the whole summary after the work was done."""
    assert "markdownlint" in render({"hooks": {"recommended": ["markdownlint"]}})


def test_a_forced_push_that_dropped_nothing_says_only_FORCED():
    facts = json.loads(json.dumps(FULL))
    facts["commit"]["push"]["forced"] = True
    text = render(facts)
    assert "FORCED" in text
    assert "dropped" not in text


def test_a_forced_push_that_dropped_commits_says_how_many():
    facts = json.loads(json.dumps(FULL))
    facts["commit"]["push"].update({"forced": True, "dropped": 3})
    assert "dropped 3 remote commit(s)" in render(facts)


def test_a_verify_that_never_ran_shows_no_run_row():
    text = render({"verify": {"install": "git hook installed"}})
    assert "install" in text
    assert "\n  run " not in text  # the row, not the word in the title


def test_a_failed_verify_is_still_reported():
    assert "failed" in render({"verify": {"run": "failed", "run_ok": False}})


def test_a_commit_scope_with_nothing_left_untouched():
    text = render({"commit": {"choice": "commit", "scope": "1 pre-commit setup file only"}})
    assert "1 pre-commit setup file only" in text
    assert "untouched" not in text


def test_the_net_section_renders_either_half_alone():
    assert "diff" in render({"net": {"diffstat": "1 file changed"}})
    assert "1 -> 2" in render({"net": {"prev_repos": 1, "new_repos": 2}})


# -- the two ways SKILL.md invokes it -----------------------------------------


def test_facts_can_arrive_on_stdin():
    proc = run_script("summary.py", stdin=json.dumps(FULL))
    assert proc.returncode == 0, proc.stderr
    assert "HOOKS" in proc.stdout


def test_an_unreadable_facts_path_fails_loudly(tmp_path):
    proc = run_script("summary.py", str(tmp_path / "never-written.json"))
    assert proc.returncode == 1
    assert "cannot read" in proc.stderr


def test_the_diffstat_counts_are_actually_coloured():
    """The old patterns looked for `+12` / `-3` -- a sign immediately before the
    digits -- and neither shape gitwork produces has ever contained one, so the
    feature was dead on every real invocation while looking implemented."""
    pal = summary.Pal(True)
    coloured = summary.color_diffstat("1 file changed, 4 insertions(+), 2 deletions(-)", pal)
    assert "\033[32m4\033[0m" in coloured, coloured
    assert "\033[31m2\033[0m" in coloured, coloured

    fresh = summary.color_diffstat("2 new file(s), 30 lines", pal)
    assert "\033[32m2\033[0m" in fresh and "\033[32m30\033[0m" in fresh, fresh


def test_colour_off_leaves_the_diffstat_alone():
    plain = "1 file changed, 4 insertions(+), 2 deletions(-)"
    assert summary.color_diffstat(plain, summary.Pal(False)) == plain
