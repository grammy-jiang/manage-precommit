"""The git layer, tested against real repositories rather than mocks.

This is code that commits and pushes. A mock that agrees with a wrong
assumption is worse than no test, so every repository here is a real one and
every push goes to a real (local, bare) remote.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from conftest import REAL_GIT, git_out, out_json, run


@pytest.fixture
def written(repo, keys_file, facts_path, stubs):
    """A repo where a run has written its files and recorded its facts."""
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--templates-file",
        str(keys_file("hygiene", "yamllint")),
        "--facts-out",
        str(facts_path),
        stubs=stubs,
    )
    assert proc.returncode == 0, proc.stderr
    return facts_path


def msgfile(tmp_path, text="chore: add pre-commit hooks"):
    p = tmp_path / "msg.txt"
    p.write_text(text + "\n")
    return p


def commit(repo, facts, tmp_path, **kw):
    return run(
        "gitwork.py",
        "--dir",
        str(repo),
        "commit",
        "--message-file",
        str(msgfile(tmp_path, **kw)),
        "--facts",
        str(facts),
    )


# -- status ------------------------------------------------------------------


def test_status_reports_a_state_per_managed_file(repo, written):
    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert set(got["files"]) == {".pre-commit-config.yaml", ".yamllint.yaml"}
    assert set(got["states"].values()) == {"untracked"}
    assert got["changed"] is True
    assert got["diff"], "a first run must still show the user something to approve"


def test_status_diffs_a_tracked_file_against_head(repo, written, tmp_path):
    commit(repo, written, tmp_path)
    (repo / ".pre-commit-config.yaml").write_text(
        (repo / ".pre-commit-config.yaml").read_text() + "# later edit\n"
    )
    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert got["states"][".pre-commit-config.yaml"] == "modified"
    assert any("diff HEAD" in c for c in got["diff_commands"])
    assert "# later edit" in got["diff"]


def test_status_on_a_non_repo_says_so_without_dying(tmp_path, repo, written):
    plain = tmp_path / "plain"
    plain.mkdir()
    got = out_json(run("gitwork.py", "--dir", str(plain), "status", "--facts", str(written)))
    assert got["is_repo"] is False


# -- commit ------------------------------------------------------------------


def test_commit_takes_only_this_runs_files(repo, written, tmp_path):
    (repo / "unrelated.txt").write_text("someone else's work\n")
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "unrelated.txt"], check=True)

    got = out_json(commit(repo, written, tmp_path))
    assert got["verdict"] == "ok"
    assert got["only_managed"] is True
    assert got["content_matches"] is True
    assert set(got["files"]) == {".pre-commit-config.yaml", ".yamllint.yaml"}

    landed = git_out(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert set(landed) == {".pre-commit-config.yaml", ".yamllint.yaml"}
    # `--only` must leave the rest of the index exactly as it was found.
    assert "A  unrelated.txt" in git_out(repo, "status", "--porcelain")


def test_commit_refuses_a_file_edited_since_it_was_verified(repo, written, tmp_path):
    (repo / ".pre-commit-config.yaml").write_text("# swapped out from under us\n")
    proc = commit(repo, written, tmp_path)
    assert proc.returncode != 0
    assert "changed since it was written and verified" in proc.stderr
    assert git_out(repo, "log", "--oneline").count("\n") == 0  # still just the initial commit


def test_commit_refuses_a_missing_file(repo, written, tmp_path):
    (repo / ".yamllint.yaml").unlink()
    proc = commit(repo, written, tmp_path)
    assert proc.returncode != 0
    assert "missing" in proc.stderr


def test_commit_refuses_an_empty_message(repo, written, tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n")
    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "commit",
        "--message-file",
        str(empty),
        "--facts",
        str(written),
    )
    assert proc.returncode != 0
    assert "empty" in proc.stderr


def test_commit_refuses_a_facts_file_naming_a_path_outside_the_repo(repo, written, tmp_path):
    facts = json.loads(written.read_text())
    facts["internal"]["managed_files"][0]["path"] = "../../etc/passwd"
    written.write_text(json.dumps(facts))
    proc = commit(repo, written, tmp_path)
    assert proc.returncode != 0
    assert "outside the repository" in proc.stderr


def test_commit_refuses_a_facts_file_with_no_managed_files(repo, written, tmp_path):
    facts = json.loads(written.read_text())
    facts["internal"]["managed_files"] = []
    written.write_text(json.dumps(facts))
    proc = commit(repo, written, tmp_path)
    assert proc.returncode != 0
    assert "nothing this run may commit" in proc.stderr


def test_commit_records_the_untouched_count(repo, written, tmp_path):
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    got = out_json(commit(repo, written, tmp_path))
    assert got["untouched"] == "2 other files"
    assert json.loads(written.read_text())["commit"]["untouched"] == "2 other files"


def test_commit_writes_the_hash_and_subject_into_the_facts(repo, written, tmp_path):
    got = out_json(commit(repo, written, tmp_path, text="chore: pin the hooks"))
    recorded = json.loads(written.read_text())["commit"]
    assert recorded["hash"] == got["hash"]
    assert recorded["subject"] == "chore: pin the hooks"
    assert recorded["scope"] == "2 pre-commit setup files only"


# -- push --------------------------------------------------------------------


@pytest.fixture
def remote(tmp_path, repo):
    """A real bare remote, with the branch already tracking it."""
    bare = tmp_path / "remote.git"
    # -b main, or the bare repo's HEAD points at refs/heads/master while this
    # repo is on main: a clone of it then checks out nothing, and _diverge's
    # commit lands on the wrong branch without ever diverging anything.
    subprocess.run([REAL_GIT, "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "remote", "add", "origin", str(bare)], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "push", "-q", "-u", "origin", "main"], check=True)
    return bare


def test_push_plan_with_no_remote_permits_nothing(repo):
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["action"] == "stop-no-remote"
    assert got["permits_push"] is False
    assert "nowhere to go" in got["guidance"]


def test_push_plan_names_the_url_not_just_the_nickname(repo, remote, written, tmp_path):
    commit(repo, written, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["action"] == "fast-forward"
    assert got["permits_push"] is True
    assert str(remote) in got["guidance"]


def test_push_fast_forwards_and_records_where_it_landed(repo, remote, written, tmp_path):
    commit(repo, written, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push", "--facts", str(written)))
    assert got["pushed"] is True
    assert got["forced"] is False
    recorded = json.loads(written.read_text())["commit"]["push"]
    assert recorded["remote"] == "origin"
    assert recorded["branch"] == "main"
    assert git_out(repo, "rev-parse", "HEAD") == git_out(remote, "rev-parse", "main")


def test_up_to_date_is_a_success_not_a_failure(repo, remote, written, tmp_path):
    commit(repo, written, tmp_path)
    run("gitwork.py", "--dir", str(repo), "push", "--facts", str(written))
    proc = run("gitwork.py", "--dir", str(repo), "push", "--facts", str(written))
    assert proc.returncode == 0
    got = out_json(proc)
    assert got["action"] == "stop-up-to-date"
    assert got["pushed"] is False


def _diverge(repo, remote, tmp_path):
    """Put a commit on the remote that the local branch does not have."""
    other = tmp_path / "other"
    subprocess.run([REAL_GIT, "clone", "-q", str(remote), str(other)], check=True)
    for key, value in (("user.email", "o@example.invalid"), ("user.name", "Other")):
        subprocess.run([REAL_GIT, "-C", str(other), "config", key, value], check=True)
    (other / "theirs.txt").write_text("their work\n")
    subprocess.run([REAL_GIT, "-C", str(other), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(other), "commit", "-qm", "theirs"], check=True)
    subprocess.run([REAL_GIT, "-C", str(other), "push", "-q"], check=True)


def test_a_diverged_push_is_refused_without_confirmation(repo, remote, written, tmp_path):
    commit(repo, written, tmp_path)
    _diverge(repo, remote, tmp_path)
    plan = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert plan["action"] == "diverged"
    assert plan["behind"] == 1
    assert plan["would_drop"], "the user must be shown what a force would delete"

    proc = run("gitwork.py", "--dir", str(repo), "push", "--facts", str(written))
    assert proc.returncode == 4
    assert out_json(proc)["pushed"] is False


def test_a_force_needs_the_sha_the_user_actually_approved(repo, remote, written, tmp_path):
    commit(repo, written, tmp_path)
    _diverge(repo, remote, tmp_path)
    proc = run("gitwork.py", "--dir", str(repo), "push", "--confirm-force", "--facts", str(written))
    assert proc.returncode == 6
    assert out_json(proc)["error"] == "missing-expect-remote"


def test_a_force_is_refused_when_the_remote_moved_since_approval(repo, remote, written, tmp_path):
    """The commits a force would drop are no longer the ones that were shown."""
    commit(repo, written, tmp_path)
    _diverge(repo, remote, tmp_path)
    stale = git_out(repo, "rev-parse", "HEAD")  # deliberately not the upstream sha
    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "push",
        "--confirm-force",
        "--expect-remote",
        stale,
        "--facts",
        str(written),
    )
    assert proc.returncode == 4
    assert out_json(proc)["error"] == "remote-moved"
    # and nothing was dropped
    assert "theirs" in git_out(remote, "log", "--oneline", "main")


def test_a_force_with_the_approved_sha_goes_through(repo, remote, written, tmp_path):
    commit(repo, written, tmp_path)
    _diverge(repo, remote, tmp_path)
    plan = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    got = out_json(
        run(
            "gitwork.py",
            "--dir",
            str(repo),
            "push",
            "--confirm-force",
            "--expect-remote",
            plan["upstream_sha"],
            "--facts",
            str(written),
        )
    )
    assert got["pushed"] is True
    assert got["forced"] is True
    assert git_out(repo, "rev-parse", "HEAD") == git_out(remote, "rev-parse", "main")


def test_behind_only_never_offers_a_force(repo, remote, written, tmp_path):
    """A force here would delete remote commits and contribute none."""
    _diverge(repo, remote, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["action"] == "stop-behind-only"
    assert got["permits_push"] is False


# -- facts -------------------------------------------------------------------


def test_facts_verifies_a_hash_rather_than_believing_it(repo, written, tmp_path):
    commit(repo, written, tmp_path)
    (repo / "stray.txt").write_text("x\n")
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "unrelated"], check=True)
    bad = git_out(repo, "rev-parse", "--short", "HEAD")

    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "facts",
        "--facts",
        str(written),
        "--choice",
        "commit only",
        "--hash",
        bad,
    )
    assert proc.returncode != 0
    assert "expected only" in proc.stderr


def test_facts_records_the_choice_and_a_diffstat(repo, written, tmp_path):
    got = out_json(commit(repo, written, tmp_path))
    run(
        "gitwork.py",
        "--dir",
        str(repo),
        "facts",
        "--facts",
        str(written),
        "--choice",
        "commit only",
        "--hash",
        got["hash"],
        "--note",
        "no upstream configured",
    )
    facts = json.loads(written.read_text())
    assert facts["commit"]["choice"] == "commit only"
    assert facts["notes"] == ["no upstream configured"]
    assert "files changed" in facts["net"]["diffstat"]


def test_facts_records_a_choice_even_with_no_repo(tmp_path, written):
    plain = tmp_path / "plain"
    plain.mkdir()
    run(
        "gitwork.py",
        "--dir",
        str(plain),
        "facts",
        "--facts",
        str(written),
        "--choice",
        "not committed",
        "--note",
        "not a git repo",
    )
    facts = json.loads(written.read_text())
    assert facts["commit"]["choice"] == "not committed"
    assert facts["scan"]["git_repo"] is False


def test_facts_refuses_a_path_inside_the_repo(repo, written):
    inside = repo / "facts.json"
    inside.write_text(written.read_text())
    proc = run(
        "gitwork.py", "--dir", str(repo), "facts", "--facts", str(inside), "--choice", "commit only"
    )
    assert proc.returncode != 0
    assert "outside the repository" in proc.stderr
