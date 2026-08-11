"""The git layer, tested against real repositories rather than mocks.

This is code that commits and pushes. A mock that agrees with a wrong
assumption is worse than no test, so every repository here is a real one and
every push goes to a real (local, bare) remote.
"""

from __future__ import annotations

import hashlib
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


# -- gates the round-1 panel found untested ----------------------------------


def install_hook(repo, body: str) -> None:
    """Install a real .git/hooks/pre-commit.

    make_git deliberately does NOT disable hooks -- a repo's hooks are part of
    how its owner wants commits made, and for this skill they are the subject
    matter. That also makes them the only honest way to drive the commit gates.
    """
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(0o755)


def test_a_commit_that_pulled_in_an_extra_file_is_refused(repo, written, tmp_path):
    """A hook that stages something else must not pass as this run's commit."""
    (repo / "sneaky.txt").write_text("not ours\n")
    install_hook(repo, f"{REAL_GIT} add sneaky.txt\nexit 0\n")
    proc = commit(repo, written, tmp_path)
    assert proc.returncode == 2
    got = out_json(proc)
    assert got["verdict"] == "touched-extra-files"
    assert got["only_managed"] is False
    assert got["record_choice"] == "not committed"
    assert "Do NOT push" in got["remedy"]


def test_a_commit_recording_different_content_is_refused(repo, written, tmp_path):
    """The file list is not the content: a hook can commit other bytes under
    the same path."""
    install_hook(
        repo,
        f"echo '# injected by a hook' >> .pre-commit-config.yaml\n"
        f"{REAL_GIT} add .pre-commit-config.yaml\nexit 0\n",
    )
    proc = commit(repo, written, tmp_path)
    assert proc.returncode == 2
    got = out_json(proc)
    assert got["verdict"] == "content-mismatch"
    assert got["only_managed"] is True
    assert got["content_matches"] is False


def test_a_failed_commit_leaves_the_index_as_it_was_found(repo, written, tmp_path):
    """`add` has already run by then, so bailing out would strand the files
    staged in a state the caller never created."""
    install_hook(repo, "exit 1\n")
    before = git_out(repo, "status", "--porcelain")
    proc = commit(repo, written, tmp_path)
    assert proc.returncode != 0
    assert "commit failed" in proc.stderr
    assert "unstaged again" in proc.stderr
    assert git_out(repo, "status", "--porcelain") == before


def test_status_refuses_rather_than_calling_everything_clean(repo, written, tmp_path, monkeypatch):
    """A failing `git status` must not read as 'nothing changed'.

    file_states seeds every path clean, so swallowing the error made `status`
    emit changed:false and sent the agent to the summary with 'no change' --
    silently discarding the files this run had just written.
    """
    fake = tmp_path / "brokengit"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "status" ]; then echo "fatal: index file corrupt" >&2; exit 128; fi\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    proc = run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written), stubs=fake)
    assert proc.returncode != 0
    assert "not a clean result" in proc.stderr


@pytest.fixture
def remote_no_upstream(tmp_path, repo):
    """A remote exists, but this branch has never been pushed to it."""
    bare = tmp_path / "fresh.git"
    subprocess.run([REAL_GIT, "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "remote", "add", "origin", str(bare)], check=True)
    return bare


def test_no_upstream_with_one_remote_pushes_and_sets_it(
    repo, remote_no_upstream, written, tmp_path
):
    commit(repo, written, tmp_path)
    plan = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert plan["action"] == "no-upstream"
    assert plan["remote"] == "origin"
    assert plan["permits_push"] is True
    got = out_json(run("gitwork.py", "--dir", str(repo), "push", "--facts", str(written)))
    assert got["pushed"] is True
    assert git_out(repo, "rev-parse", "HEAD") == git_out(remote_no_upstream, "rev-parse", "main")


def test_no_upstream_with_several_remotes_and_no_origin_asks(repo, written, tmp_path):
    for name in ("alpha", "beta"):
        bare = tmp_path / f"{name}.git"
        subprocess.run([REAL_GIT, "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        subprocess.run([REAL_GIT, "-C", str(repo), "remote", "add", name, str(bare)], check=True)
    commit(repo, written, tmp_path)

    plan = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert plan["action"] == "no-upstream"
    assert plan["remote"] is None, "nothing may be chosen on the user's behalf"
    assert set(plan["remote_urls"]) == {"alpha", "beta"}
    assert "not settled yet" in plan["guidance"]

    proc = run("gitwork.py", "--dir", str(repo), "push", "--facts", str(written))
    assert proc.returncode == 5
    assert out_json(proc)["error"] == "ambiguous-remote"

    got = out_json(
        run("gitwork.py", "--dir", str(repo), "push", "--remote", "beta", "--facts", str(written))
    )
    assert got["pushed"] is True
    assert json.loads(written.read_text())["commit"]["push"]["remote"] == "beta"


def test_an_unknown_remote_is_named_not_guessed(repo, remote_no_upstream, written, tmp_path):
    commit(repo, written, tmp_path)
    proc = run(
        "gitwork.py", "--dir", str(repo), "push", "--remote", "nope", "--facts", str(written)
    )
    assert proc.returncode == 5
    assert out_json(proc)["error"] == "unknown-remote"


def test_a_detached_head_is_classified_not_crashed_on(repo, remote, written, tmp_path):
    commit(repo, written, tmp_path)
    subprocess.run([REAL_GIT, "-C", str(repo), "checkout", "-q", "--detach"], check=True)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["action"] == "stop-detached-head"
    assert got["permits_push"] is False
    proc = run("gitwork.py", "--dir", str(repo), "push", "--facts", str(written))
    assert proc.returncode == 3
    assert out_json(proc)["pushed"] is False


# -- classifier partitions and gates the round-2 panel found untested ---------


def stub_git(tmp_path, name: str, intercept: str):
    """A PATH dir whose `git` fails one subcommand and forwards the rest."""
    d = tmp_path / name
    d.mkdir()
    g = d / "git"
    g.write_text(
        "#!/bin/sh\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        f"  {intercept}\n"
        '  prev="$a"\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    return d


def test_a_failed_fetch_stops_rather_than_comparing_against_stale_data(
    repo, remote, written, tmp_path
):
    """Classifying ahead/behind against a stale remote-tracking ref would make
    every push decision after it unsound, so it is a hard stop."""
    commit(repo, written, tmp_path)
    fake = stub_git(
        tmp_path,
        "nofetch",
        'if [ "$a" = "fetch" ]; then echo "fatal: unreachable" >&2; exit 128; fi',
    )
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan", stubs=fake))
    assert got["action"] == "stop-fetch-failed"
    assert got["permits_push"] is False
    assert "could not reach the remote" in got["guidance"]

    proc = run("gitwork.py", "--dir", str(repo), "push", "--facts", str(written), stubs=fake)
    assert proc.returncode == 3
    assert out_json(proc)["pushed"] is False


def test_an_unreadable_ahead_behind_count_stops_the_decision(repo, remote, written, tmp_path):
    commit(repo, written, tmp_path)
    fake = stub_git(
        tmp_path,
        "nocount",
        'if [ "$prev" = "rev-list" ] && [ "$a" = "--left-right" ]; then exit 1; fi',
    )
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan", stubs=fake))
    assert got["action"] == "stop-compare-failed"
    assert got["permits_push"] is False


def test_push_plan_and_push_on_a_non_repo(tmp_path, written):
    plain = tmp_path / "plain"
    plain.mkdir()
    got = out_json(run("gitwork.py", "--dir", str(plain), "push-plan"))
    assert got["action"] == "stop-not-a-repo"
    assert got["permits_push"] is False

    proc = run("gitwork.py", "--dir", str(plain), "push", "--facts", str(written))
    assert proc.returncode == 3
    assert out_json(proc)["pushed"] is False


@pytest.fixture
def written_config_only(repo, keys_file, facts_path, stubs):
    """A run whose only managed file is the config -- hygiene ships no asset."""
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--templates-file",
        str(keys_file("hygiene")),
        "--facts-out",
        str(facts_path),
        stubs=stubs,
    )
    assert proc.returncode == 0, proc.stderr
    return facts_path


def test_a_single_file_run_says_so_in_the_scope(repo, written_config_only, tmp_path):
    """The plural branch was the only one ever reached."""
    commit(repo, written_config_only, tmp_path)
    scope = json.loads(written_config_only.read_text())["commit"]["scope"]
    assert scope == ".pre-commit-config.yaml only"


def test_facts_hash_refuses_content_that_is_not_what_was_verified(
    repo, written_config_only, tmp_path
):
    """cmd_facts has its own blob gate, distinct from the one in cmd_commit:
    the paths can match exactly while the bytes do not."""
    commit(repo, written_config_only, tmp_path)
    # A second commit touching the same single path, with different content.
    (repo / ".pre-commit-config.yaml").write_text("# replaced wholesale\nrepos: []\n")
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "rewrite"], check=True)
    bad = git_out(repo, "rev-parse", "--short", "HEAD")

    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "facts",
        "--facts",
        str(written_config_only),
        "--hash",
        bad,
    )
    assert proc.returncode != 0
    assert "not what this run wrote and verified" in proc.stderr


# -- the recorded outcome is derived, not typed -------------------------------


def test_the_choice_is_derived_from_what_was_recorded(repo, remote, written, tmp_path):
    """Pushed, so the outcome is 'commit + push' without anyone saying so."""
    got = out_json(commit(repo, written, tmp_path))
    run("gitwork.py", "--dir", str(repo), "push", "--facts", str(written))
    run("gitwork.py", "--dir", str(repo), "facts", "--facts", str(written), "--hash", got["hash"])
    assert json.loads(written.read_text())["commit"]["choice"] == "commit + push"


def test_a_commit_without_a_push_derives_commit_only(repo, written, tmp_path):
    got = out_json(commit(repo, written, tmp_path))
    run("gitwork.py", "--dir", str(repo), "facts", "--facts", str(written), "--hash", got["hash"])
    assert json.loads(written.read_text())["commit"]["choice"] == "commit only"


def test_nothing_committed_derives_not_committed(repo, written):
    run("gitwork.py", "--dir", str(repo), "facts", "--facts", str(written))
    assert json.loads(written.read_text())["commit"]["choice"] == "not committed"


def test_an_explicit_choice_still_wins(repo, written, tmp_path):
    """The user declining is not a repository fact, so it must be passable."""
    commit(repo, written, tmp_path)
    run(
        "gitwork.py",
        "--dir",
        str(repo),
        "facts",
        "--facts",
        str(written),
        "--choice",
        "not committed",
        "--note",
        "user declined",
    )
    assert json.loads(written.read_text())["commit"]["choice"] == "not committed"


# -- the suspicious-character signals, end to end -----------------------------

BIDI = chr(0x202E)  # right-to-left override, built from its codepoint


def test_status_flags_a_diff_that_may_not_read_as_it_says(repo, written, tmp_path):
    """A config is partly upstream- and partly repo-supplied, so a diff can
    render differently from the bytes it contains. Removing this call would
    otherwise pass the whole suite."""
    commit(repo, written, tmp_path)
    path = repo / ".pre-commit-config.yaml"
    path.write_text(path.read_text() + f"# note{BIDI} reversed\n")
    proc = run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written))
    got = out_json(proc)
    assert got["suspicious_characters"] is True
    assert "WARNING" in proc.stderr


def test_push_plan_flags_a_remote_whose_name_misrepresents_itself(repo, written, tmp_path):
    """Remote names come from repository config and are shown as a push
    destination the user approves."""
    bare = tmp_path / "sneaky.git"
    subprocess.run([REAL_GIT, "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(
        [REAL_GIT, "-C", str(repo), "remote", "add", f"ori{BIDI}gin", str(bare)], check=True
    )
    commit(repo, written, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["action"] == "no-upstream"
    assert got["suspicious_characters"] is True


def test_a_diverged_plan_flags_forged_commit_subjects(repo, remote, written, tmp_path):
    """These subject lines are what push-safety.md asks the operator to read
    before approving an irreversible force-push."""
    commit(repo, written, tmp_path)

    other = tmp_path / "other"
    subprocess.run([REAL_GIT, "clone", "-q", str(remote), str(other)], check=True)
    for key, value in (("user.email", "o@example.invalid"), ("user.name", "Other")):
        subprocess.run([REAL_GIT, "-C", str(other), "config", key, value], check=True)
    (other / "theirs.txt").write_text("their work\n")
    subprocess.run([REAL_GIT, "-C", str(other), "add", "-A"], check=True)
    msg = other / "msg.txt"
    msg.write_text(f"fix{BIDI} something\n")
    subprocess.run([REAL_GIT, "-C", str(other), "commit", "-q", "-F", str(msg)], check=True)
    subprocess.run([REAL_GIT, "-C", str(other), "push", "-q"], check=True)

    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["action"] == "diverged"
    assert got["suspicious_characters"] is True
    assert got["would_drop"], "the operator must still be shown what would be dropped"


def test_the_drop_list_names_who_wrote_each_commit(repo, remote, written, tmp_path):
    """A subject alone cannot say whose work a force-push would delete."""
    commit(repo, written, tmp_path)
    _diverge(repo, remote, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["action"] == "diverged"
    assert any("Other:" in line for line in got["would_drop"]), got["would_drop"]


# -- guards added after round 4 of the reviewer panel -------------------------


def _rewrite_facts(facts_file, **fields):
    facts = json.loads(facts_file.read_text())
    facts["internal"]["managed_files"][0].update(fields)
    facts_file.write_text(json.dumps(facts))


def test_a_managed_path_that_looks_like_an_option_is_refused(repo, written, tmp_path):
    """These paths reach `git add --` and `git commit --only --` as argv."""
    _rewrite_facts(written, path="-x")
    proc = commit(repo, written, tmp_path)
    assert proc.returncode != 0
    assert "looks like an option" in proc.stderr


@pytest.mark.parametrize("digest", ["deadbeef", "z" * 64, ""])
def test_a_managed_sha256_of_the_wrong_shape_is_refused(repo, written, tmp_path, digest):
    """The facts file sits on disk between steps; a rewritten one must not be
    able to weaken the content check by supplying a digest that cannot match."""
    _rewrite_facts(written, sha256=digest)
    proc = commit(repo, written, tmp_path)
    assert proc.returncode != 0
    # The specific refusal, not just any mention of sha256: without this guard
    # a malformed digest still fails later in the content check, whose message
    # also says "sha256" -- so a looser assertion passed either way.
    expected = "missing its path or sha256" if digest == "" else "unexpected shape"
    assert expected in proc.stderr, proc.stderr


def test_a_double_failure_says_the_index_may_still_be_staged(repo, written, tmp_path):
    """add succeeded, commit failed, and the cleanup reset failed too -- the one
    case where the index is NOT as it was found."""
    install_hook(repo, "exit 1\n")
    fake = tmp_path / "noreset"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "reset" ]; then echo "fatal: cannot reset" >&2; exit 128; fi\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "commit",
        "--message-file",
        str(msgfile(tmp_path)),
        "--facts",
        str(written),
        stubs=fake,
    )
    assert proc.returncode != 0
    assert "cleanup reset also failed" in proc.stderr
    assert "may still be staged" in proc.stderr


def test_a_multi_line_commit_message_is_refused(repo, written, tmp_path):
    """Only the subject is shown back and recorded, so a body would be
    committed having been neither reviewed nor reported."""
    msg = tmp_path / "long.txt"
    msg.write_text("chore: add hooks\n\nand a body nobody approved\n")
    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "commit",
        "--message-file",
        str(msg),
        "--facts",
        str(written),
    )
    assert proc.returncode != 0
    assert "single line" in proc.stderr


def test_facts_still_records_when_the_path_is_gone_from_the_commit(
    repo, written_config_only, tmp_path
):
    """blob_matches_verified cannot tell, so it must not block."""
    got = out_json(commit(repo, written_config_only, tmp_path))
    (repo / ".pre-commit-config.yaml").unlink()
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "remove"], check=True)
    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "facts",
        "--facts",
        str(written_config_only),
        "--hash",
        got["hash"],
    )
    assert proc.returncode == 0, proc.stderr


def test_the_consent_lines_are_neutralised_not_merely_flagged(repo, remote, written, tmp_path):
    """push-safety.md treats showing these lines as consent for an irreversible
    act, so they must be safe to print, not just accompanied by a warning."""
    commit(repo, written, tmp_path)
    other = tmp_path / "other2"
    subprocess.run([REAL_GIT, "clone", "-q", str(remote), str(other)], check=True)
    for key, value in (("user.email", "o@example.invalid"), ("user.name", "Other")):
        subprocess.run([REAL_GIT, "-C", str(other), "config", key, value], check=True)
    (other / "x.txt").write_text("x\n")
    subprocess.run([REAL_GIT, "-C", str(other), "add", "-A"], check=True)
    msg = other / "m.txt"
    msg.write_text("fix" + chr(0x202E) + " something\n")
    subprocess.run([REAL_GIT, "-C", str(other), "commit", "-q", "-F", str(msg)], check=True)
    subprocess.run([REAL_GIT, "-C", str(other), "push", "-q"], check=True)

    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["suspicious_characters"] is True
    joined = " ".join(got["would_drop"] + got["would_add"]) + got["guidance"]
    assert chr(0x202E) not in joined, "a text-reordering character reached the consent text"


def test_a_remote_name_is_taken_from_a_file_not_a_command_line(repo, written, tmp_path):
    """Git permits quotes, semicolons and $ in a remote name, and the name comes
    from repository config -- so it must never be interpolated into the shell
    command the agent runs, exactly like a commit message or a catalog list."""
    bare = tmp_path / "viaFile.git"
    subprocess.run([REAL_GIT, "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    for name in ("alpha", "beta"):
        b = tmp_path / f"{name}2.git"
        subprocess.run([REAL_GIT, "init", "-q", "--bare", "-b", "main", str(b)], check=True)
        subprocess.run([REAL_GIT, "-C", str(repo), "remote", "add", name, str(b)], check=True)
    commit(repo, written, tmp_path)

    chosen = tmp_path / "remote.txt"
    chosen.write_text("beta\n")
    got = out_json(
        run(
            "gitwork.py",
            "--dir",
            str(repo),
            "push",
            "--remote-file",
            str(chosen),
            "--facts",
            str(written),
        )
    )
    assert got["pushed"] is True
    assert json.loads(written.read_text())["commit"]["push"]["remote"] == "beta"


def test_remote_and_remote_file_are_mutually_exclusive(repo, written, tmp_path):
    chosen = tmp_path / "r.txt"
    chosen.write_text("origin\n")
    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "push",
        "--remote",
        "origin",
        "--remote-file",
        str(chosen),
        "--facts",
        str(written),
    )
    assert proc.returncode != 0
    assert "not both" in proc.stderr


def test_the_summary_records_that_a_push_was_forced(repo, remote, written, tmp_path):
    """Consent was given in the moment, but the closing summary is the durable
    record -- and it read identically whether the push fast-forwarded or
    rewrote history."""
    commit(repo, written, tmp_path)
    _diverge(repo, remote, tmp_path)
    plan = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
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
    push = json.loads(written.read_text())["commit"]["push"]
    assert push["forced"] is True
    assert push["dropped"] == 1


def test_an_ordinary_push_is_not_recorded_as_forced(repo, remote, written, tmp_path):
    commit(repo, written, tmp_path)
    run("gitwork.py", "--dir", str(repo), "push", "--facts", str(written))
    assert "forced" not in json.loads(written.read_text())["commit"]["push"]


def test_the_net_diffstat_survives_the_documented_no_hash_path(repo, written, tmp_path):
    """SKILL.md's ordinary path passes no --hash; diffstat then read a working
    tree the commit had already made clean, so the NET diff row vanished from
    every ordinary run."""
    commit(repo, written, tmp_path)
    run("gitwork.py", "--dir", str(repo), "facts", "--facts", str(written))
    net = json.loads(written.read_text())["net"]
    assert "files changed" in net["diffstat"], net


def test_the_shown_destination_is_the_url_git_will_actually_use(repo, written, tmp_path):
    """A literal read of remote.<name>.url does not apply url.insteadOf, so the
    consent line could name a repository the push never reaches."""
    real = tmp_path / "real.git"
    subprocess.run([REAL_GIT, "init", "-q", "--bare", "-b", "main", str(real)], check=True)
    decoy = f"{tmp_path}/decoy/"
    subprocess.run(
        [REAL_GIT, "-C", str(repo), "remote", "add", "origin", f"{decoy}real.git"], check=True
    )
    subprocess.run(
        [REAL_GIT, "-C", str(repo), "config", f"url.{tmp_path}/.insteadOf", decoy], check=True
    )
    commit(repo, written, tmp_path)

    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    url = got["remote_urls"]["origin"]
    assert "decoy" not in url, f"showed the unrewritten URL: {url}"
    assert str(real) in url


def test_local_config_that_can_run_a_program_is_surfaced(repo, remote, written, tmp_path):
    """core.sshCommand and credential.helper are settable in a checked-out
    repo's own config and are not covered by the protocol.* hardening."""
    subprocess.run(
        [REAL_GIT, "-C", str(repo), "config", "core.sshCommand", "/tmp/anything"], check=True
    )
    subprocess.run(
        [REAL_GIT, "-C", str(repo), "config", "credential.helper", "/tmp/grab"], check=True
    )
    commit(repo, written, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["local_overrides"] == ["core.sshCommand", "credential.helper"]


def test_an_ordinary_repo_reports_no_local_overrides(repo, remote, written, tmp_path):
    commit(repo, written, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["local_overrides"] == []


def test_the_destination_is_offered_without_the_verdict(repo, remote, written, tmp_path):
    """Before a commit exists a synced branch's guidance reads "nothing to
    push", which must not be what the agent quotes beside Commit + push."""
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["action"] == "stop-up-to-date"
    assert "nothing to push" in got["guidance"]
    assert "nothing to push" not in got["destination"]
    assert "origin/main" in got["destination"]
    assert str(remote) in got["destination"]


def test_the_undo_hint_fits_a_first_commit(tmp_path, keys_file, facts_path, stubs):
    """`<ref>^` does not exist for a first commit -- and setting up pre-commit
    as the first act in a new repo is exactly when the advice matters."""
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    subprocess.run([REAL_GIT, "init", "-q", "-b", "main", str(fresh)], check=True)
    for key, value in (("user.email", "t@example.invalid"), ("user.name", "Test")):
        subprocess.run([REAL_GIT, "-C", str(fresh), "config", key, value], check=True)
    proc = run(
        "precommit.py",
        "--dir",
        str(fresh),
        "--templates-file",
        str(keys_file("hygiene")),
        "--facts-out",
        str(facts_path),
        stubs=stubs,
    )
    assert proc.returncode == 0, proc.stderr

    (fresh / "sneaky.txt").write_text("not ours\n")
    install_hook(fresh, f"{REAL_GIT} add sneaky.txt\nexit 0\n")
    got = out_json(commit(fresh, facts_path, tmp_path))
    assert got["verdict"] == "touched-extra-files"
    assert "update-ref -d HEAD" in got["remedy"], got["remedy"]


def test_several_remotes_with_origin_settles_on_origin(repo, written, tmp_path):
    """The fork case -- origin plus upstream -- short-circuits neither arm the
    other two tests cover."""
    for name in ("origin", "upstream"):
        bare = tmp_path / f"{name}3.git"
        subprocess.run([REAL_GIT, "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        subprocess.run([REAL_GIT, "-C", str(repo), "remote", "add", name, str(bare)], check=True)
    commit(repo, written, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["action"] == "no-upstream"
    assert got["remote"] == "origin"
    assert got["permits_push"] is True


def test_a_staged_managed_file_reads_as_staged(repo, written, tmp_path):
    commit(repo, written, tmp_path)
    path = repo / ".pre-commit-config.yaml"
    path.write_text(path.read_text() + "# staged edit\n")
    subprocess.run([REAL_GIT, "-C", str(repo), "add", ".pre-commit-config.yaml"], check=True)
    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert got["states"][".pre-commit-config.yaml"] == "staged"


def test_staged_plus_a_further_edit_still_reads_as_staged(repo, written, tmp_path):
    """The documented MM case: reported as staged so the diff shown is the one
    that would be committed."""
    commit(repo, written, tmp_path)
    path = repo / ".pre-commit-config.yaml"
    path.write_text(path.read_text() + "# staged edit\n")
    subprocess.run([REAL_GIT, "-C", str(repo), "add", ".pre-commit-config.yaml"], check=True)
    path.write_text(path.read_text() + "# and an unstaged one\n")
    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert got["states"][".pre-commit-config.yaml"] == "staged"


def test_hooks_this_skill_did_not_install_are_reported(repo, written, tmp_path):
    """They run during our own commit and push and appear in no diff."""
    for name in ("pre-push", "commit-msg"):
        h = repo / ".git" / "hooks" / name
        h.write_text("#!/bin/sh\nexit 0\n")
        h.chmod(0o755)
    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert got["native_hooks"] == ["pre-push", "commit-msg"]


def test_the_installed_pre_commit_hook_is_not_reported_as_foreign(repo, written):
    """It is the one this skill puts there; flagging it would be noise."""
    h = repo / ".git" / "hooks" / "pre-commit"
    h.write_text("#!/bin/sh\nexit 0\n")
    h.chmod(0o755)
    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert got["native_hooks"] == []


def test_an_external_differ_cannot_produce_the_reviewed_diff(repo, written, tmp_path):
    """diff.external replaces the diff the user approves, so it could fabricate
    one outright -- which checking the output afterwards would never catch."""
    subprocess.run([REAL_GIT, "-C", str(repo), "config", "diff.external", "/bin/false"], check=True)
    commit(repo, written, tmp_path)
    path = repo / ".pre-commit-config.yaml"
    path.write_text(path.read_text() + "# a real edit\n")
    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert "# a real edit" in got["diff"], "the hostile differ produced the reviewed diff"


def test_a_hostile_fsmonitor_never_runs(repo, written, tmp_path):
    """core.fsmonitor fires on the FIRST git status this tool makes, before any
    question has been asked.

    The assertion is on a marker the hook would leave behind, not on stderr:
    git prints its complaint but still exits 0, and gitwork swallows git's
    stderr on success -- so an stderr assertion could never observe this.
    What matters is whether the program ran at all.
    """
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\n: > {marker}\nexit 0\n")
    hook.chmod(0o755)
    subprocess.run([REAL_GIT, "-C", str(repo), "config", "core.fsmonitor", str(hook)], check=True)

    proc = run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written))
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists(), "core.fsmonitor executed a program"


def test_the_fsmonitor_marker_test_can_actually_fail(repo, tmp_path):
    """The control for the test above: without the hardening, git really does
    run it -- otherwise that assertion would prove nothing."""
    marker = tmp_path / "control-ran"
    hook = tmp_path / "control.sh"
    hook.write_text(f"#!/bin/sh\n: > {marker}\nexit 0\n")
    hook.chmod(0o755)
    subprocess.run([REAL_GIT, "-C", str(repo), "config", "core.fsmonitor", str(hook)], check=True)
    subprocess.run(
        [REAL_GIT, "-C", str(repo), "status", "--porcelain"], capture_output=True, check=False
    )
    assert marker.exists(), "plain git did not invoke core.fsmonitor on this version"


def test_hooks_path_is_reported_as_a_local_override(repo, remote, written, tmp_path):
    subprocess.run([REAL_GIT, "-C", str(repo), "config", "core.hooksPath", ".ci/hooks"], check=True)
    commit(repo, written, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert "core.hooksPath" in got["local_overrides"]


def test_an_undeterminable_disclosure_is_not_reported_as_nothing(repo, written, tmp_path):
    """porcelain() in this same file already refuses to call a failed check a
    clean result; these two consent disclosures did exactly that."""
    fake = tmp_path / "nocfg"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "config" ] && [ "$a" = "--local" ]; then exit 128; fi\n'
        '  prev="$a"\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    got = out_json(
        run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written), stubs=fake)
    )
    assert got["local_overrides"], "a failed check came back as nothing to disclose"
    assert "could not determine" in got["local_overrides"][0]


def test_a_ref_update_hook_is_disclosed(repo, remote, written, tmp_path):
    """reference-transaction fires on every ref update since git 2.28 --
    including push-plan's own fetch, which runs in Step 5 to describe the
    destination, before the commit question and even on a run that ends in
    "Don't commit"."""
    h = repo / ".git" / "hooks" / "reference-transaction"
    h.write_text("#!/bin/sh\nexit 0\n")
    h.chmod(0o755)
    commit(repo, written, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert "reference-transaction" in got["native_hooks"]


def test_status_says_how_to_discard_each_file(repo, written, tmp_path):
    """A pure function of the state the program already computed, so the doc
    does not carry a table that can drift from it."""
    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert got["states"][".pre-commit-config.yaml"] == "untracked"
    assert got["discards"][".pre-commit-config.yaml"] == "rm -- .pre-commit-config.yaml"

    commit(repo, written, tmp_path)
    path = repo / ".pre-commit-config.yaml"
    path.write_text(path.read_text() + "# edit\n")
    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert got["discards"][".pre-commit-config.yaml"] == ("git checkout -- .pre-commit-config.yaml")

    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert got["discards"][".pre-commit-config.yaml"] == (
        "git restore --staged --worktree -- .pre-commit-config.yaml"
    )


def test_a_malformed_managed_files_entry_is_reported_not_crashed_on(repo, written, tmp_path):
    facts = json.loads(written.read_text())
    facts["internal"]["managed_files"] = ["just-a-string"]
    written.write_text(json.dumps(facts))
    proc = commit(repo, written, tmp_path)
    assert proc.returncode != 0
    assert "not a JSON object" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_a_push_the_server_rejects_is_not_recorded_as_landed(repo, remote, written, tmp_path):
    """push-plan and push are two round-trips, so a rejection between them is
    ordinary. A failed push must not leave a commit.push behind for summary.py
    to render as though it landed."""
    commit(repo, written, tmp_path)
    fake = tmp_path / "nopush"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "push" ]; then echo "remote: rejected" >&2; exit 1; fi\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    proc = run("gitwork.py", "--dir", str(repo), "push", "--facts", str(written), stubs=fake)
    assert proc.returncode != 0
    assert "push" in proc.stderr
    assert "push" not in json.loads(written.read_text()).get("commit", {})


def test_a_failed_log_does_not_produce_an_empty_consent_list(repo, remote, written, tmp_path):
    """push-safety.md rests the whole force-push consent on would_drop. An
    unchecked git log meant a transient failure showed "would drop N commits"
    beside zero lines -- approval asked for destroying work never shown."""
    commit(repo, written, tmp_path)
    _diverge(repo, remote, tmp_path)
    fake = tmp_path / "nolog"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "log" ]; then echo "fatal: bad revision" >&2; exit 128; fi\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan", stubs=fake))
    assert got["action"] != "diverged", "a force-push was offered with no evidence"
    assert got["permits_push"] is False


def test_the_untouched_files_are_named_not_just_counted(repo, written, tmp_path):
    """A bare count beside VERIFY's named autofix list left the reader unable
    to tell whether the two describe the same files."""
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    commit(repo, written, tmp_path)
    recorded = json.loads(written.read_text())["commit"]
    assert recorded["untouched"] == "2 other files"
    assert recorded["untouched_files"] == ["a.txt", "b.txt"]


def test_a_textconv_driver_cannot_forge_the_reviewed_diff(repo, written, tmp_path):
    """textconv is a different mechanism from diff.external: a .gitattributes
    mapping plus a local driver, whose output git substitutes as file content.
    --no-ext-diff does nothing against it."""
    (repo / ".gitattributes").write_text(".pre-commit-config.yaml diff=forge\n")
    subprocess.run(
        [REAL_GIT, "-C", str(repo), "config", "diff.forge.textconv", "/bin/echo"], check=True
    )
    commit(repo, written, tmp_path)
    path = repo / ".pre-commit-config.yaml"
    path.write_text(path.read_text() + "# a real edit\n")
    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert "# a real edit" in got["diff"], "the textconv driver produced the reviewed diff"


def test_a_per_driver_config_key_is_disclosed(repo, remote, written, tmp_path):
    """The driver name is the repo's to choose, so an exact-key lookup can
    never see filter.<name>.clean -- which git runs during `git add`."""
    subprocess.run(
        [REAL_GIT, "-C", str(repo), "config", "filter.sneaky.clean", "/bin/cat"], check=True
    )
    subprocess.run(
        [REAL_GIT, "-C", str(repo), "config", "diff.sneaky.textconv", "/bin/cat"], check=True
    )
    commit(repo, written, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert "filter.sneaky.clean" in got["local_overrides"], got["local_overrides"]
    assert "diff.sneaky.textconv" in got["local_overrides"], got["local_overrides"]


def test_an_ordinary_repo_still_reports_no_per_driver_keys(repo, remote, written, tmp_path):
    commit(repo, written, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["local_overrides"] == []


def test_a_file_whose_hash_cannot_be_taken_is_not_silently_unverified(repo, written, tmp_path):
    """Skipping it dropped that path out of the post-commit content check while
    the result still said content_matches: true."""
    fake = tmp_path / "nohash"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$a" = "hash-object" ]; then echo "fatal: nope" >&2; exit 128; fi\n'
        '  prev="$a"\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "commit",
        "--message-file",
        str(msgfile(tmp_path)),
        "--facts",
        str(written),
        stubs=fake,
    )
    assert proc.returncode != 0
    assert "refusing to commit unverified" in proc.stderr


@pytest.mark.parametrize(
    "hostile",
    [":evil", "+refs/heads/other", "refs/heads/a b", "--upload-pack=x", "refs/heads/a:b"],
)
def test_a_hostile_upstream_ref_never_builds_a_refspec(hostile):
    """branch.<name>.merge comes from repository config and is interpolated into
    `HEAD:<ref>` and into the --force-with-lease bound push-safety.md relies on.

    Driven directly: a value this malformed also stops git resolving `@{u}`, so
    push_plan falls to the no-upstream branch and the guard is never reached
    through the CLI -- which is why it is defence in depth, and why it still has
    to hold.
    """
    import gitwork

    with pytest.raises(SystemExit):
        gitwork.safe_merge_ref(hostile)


@pytest.mark.parametrize("ok", ["refs/heads/main", "refs/heads/feature/x"])
def test_an_ordinary_upstream_ref_passes(ok):
    import gitwork

    assert gitwork.safe_merge_ref(ok) == ok


@pytest.mark.parametrize("flag", ["--facts", "--message-file"])
def test_commit_refuses_a_symlinked_input(repo, written, tmp_path, flag):
    """gitwork is the script that commits and pushes, and its three file inputs
    had no end-to-end symlink test -- a regression to plain open() at any of
    them would have passed the whole suite."""
    secret = tmp_path / "secret"
    secret.write_text("id_rsa\n")
    link = tmp_path / f"link-{flag.strip('-')}"
    link.symlink_to(secret)
    args = {"--facts": str(written), "--message-file": str(msgfile(tmp_path))}
    args[flag] = str(link)
    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "commit",
        "--message-file",
        args["--message-file"],
        "--facts",
        args["--facts"],
    )
    assert proc.returncode != 0
    assert "symlink" in proc.stderr


def test_push_refuses_a_symlinked_remote_file(repo, written, tmp_path):
    secret = tmp_path / "secret2"
    secret.write_text("origin\n")
    link = tmp_path / "remote-link.txt"
    link.symlink_to(secret)
    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "push",
        "--remote-file",
        str(link),
        "--facts",
        str(written),
    )
    assert proc.returncode != 0
    assert "symlink" in proc.stderr


def test_an_undeterminable_hook_listing_is_not_reported_as_none(repo, written, tmp_path):
    """The sibling disclosure has this test; this one did not, so reverting its
    guard to `return []` would have passed."""
    fake = tmp_path / "nohooksdir"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--git-path" ] && [ "$a" = "hooks" ]; then exit 128; fi\n'
        '  prev="$a"\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    got = out_json(
        run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written), stubs=fake)
    )
    assert got["native_hooks"], "a failed check came back as nothing to disclose"
    assert "could not determine" in got["native_hooks"][0]


def test_every_push_url_is_disclosed(repo, written, tmp_path):
    """remote.<name>.pushurl can be set more than once and git push sends the
    ref update to every one -- force included."""
    first = tmp_path / "one.git"
    second = tmp_path / "two.git"
    for bare in (first, second):
        subprocess.run([REAL_GIT, "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "remote", "add", "origin", str(first)], check=True)
    subprocess.run(
        [REAL_GIT, "-C", str(repo), "config", "--add", "remote.origin.pushurl", str(first)],
        check=True,
    )
    subprocess.run(
        [REAL_GIT, "-C", str(repo), "config", "--add", "remote.origin.pushurl", str(second)],
        check=True,
    )
    commit(repo, written, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    shown = got["remote_urls"]["origin"]
    assert str(first) in shown and str(second) in shown, shown


# -- round 15 ----------------------------------------------------------------


def _with_upstream(repo, written, tmp_path, name="bare"):
    """A repo whose branch has a real upstream, so push-plan takes that path."""
    bare = tmp_path / f"{name}.git"
    subprocess.run([REAL_GIT, "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "remote", "add", "origin", str(bare)], check=True)
    commit(repo, written, tmp_path)
    subprocess.run([REAL_GIT, "-C", str(repo), "push", "-q", "-u", "origin", "main"], check=True)
    return bare


@pytest.mark.parametrize("poison", ["url", "remove-section"])
def test_a_poisoned_branch_remote_degrades_to_the_branch_that_checks(
    repo, written, tmp_path, poison
):
    """Why push_plan's upstream path needs no allowlist of its own.

    A review round called for one: branch.<branch>.remote comes from
    .git/config, `git push` accepts a raw URL in that position, and
    refuse_option_like only rejects a leading dash. But that path is reached
    only when `git rev-parse @{u}` has already succeeded, and git resolves @{u}
    only for a CONFIGURED remote with a live remote-tracking ref. Both ways of
    poisoning it make @{u} fail, which routes to the no-upstream branch -- and
    that branch enumerates `git remote` and refuses anything not on it.

    This test is the evidence for that claim, and the thing that will notice if
    a future git makes @{u} resolve in either case. Then the guard becomes
    necessary after all.
    """
    _with_upstream(repo, written, tmp_path)
    elsewhere = tmp_path / "attacker.git"
    subprocess.run([REAL_GIT, "init", "-q", "--bare", "-b", "main", str(elsewhere)], check=True)
    if poison == "url":
        subprocess.run(
            [REAL_GIT, "-C", str(repo), "config", "branch.main.remote", str(elsewhere)],
            check=True,
        )
    else:
        subprocess.run(
            [REAL_GIT, "-C", str(repo), "config", "--remove-section", "remote.origin"],
            check=True,
        )

    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["action"] in ("no-upstream", "stop-no-remote"), got["action"]

    # The outcome differs by case and neither is a failure worth asserting: with
    # the remote section removed there is nothing to push to, and with a URL in
    # branch.main.remote the no-upstream path falls back to the one CONFIGURED
    # remote and pushes there. What matters is the same either way.
    run("gitwork.py", "--dir", str(repo), "push", "--facts", str(written))
    landed = subprocess.run(
        [REAL_GIT, "-C", str(elsewhere), "rev-list", "--all", "--count"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert landed.stdout.strip() == "0", "the push reached the destination nobody was shown"


def test_a_configured_upstream_remote_still_plans_normally(repo, written, tmp_path):
    """The ordinary case must keep working."""
    _with_upstream(repo, written, tmp_path)
    got = out_json(run("gitwork.py", "--dir", str(repo), "push-plan"))
    assert got["action"] in ("stop-up-to-date", "fast-forward"), got["action"]
    assert got["remote"] == "origin"


def test_status_names_what_else_is_uncommitted(repo, written, tmp_path):
    """Step 4's --all-files question is about exactly these files: the
    autofixing hooks rewrite whatever they are pointed at, and this run's guard
    covers only this run's files. Asked in the abstract the risk is accepted
    blind, and the concrete state only appeared in Step 5.1, after the rewrite."""
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "base"], check=True)
    (repo / "README.md").write_text("edited by the user\n")
    untracked = repo / "scratch.txt"
    untracked.write_text("not tracked\n")

    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert "README.md" in got["dirty_elsewhere"]
    assert "scratch.txt" not in got["dirty_elsewhere"], (
        "--all-files covers tracked files only, so an untracked one is not at risk"
    )
    for owned in got["files"]:
        assert owned not in got["dirty_elsewhere"], "this run's own files are not 'elsewhere'"


def test_a_clean_tree_reports_nothing_dirty_elsewhere(repo, written, tmp_path):
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "base"], check=True)
    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert got["dirty_elsewhere"] == []


def test_untouched_filenames_are_cleaned_before_they_are_shown(repo, written, tmp_path):
    """Pins the PROPERTY, not a particular mechanism.

    Two things could be doing the work here: core.quotePath (now forced), which
    makes git C-quote a hostile filename in its own output, and the clean() on
    this list. Deleting the clean() does not fail this test, so the forced
    quoting is what is load-bearing; the clean() is belt-and-braces, kept for
    consistency with every sibling display list rather than because a test
    distinguishes it. Deleting core.quotePath IS caught, by test_shared.
    """
    bidi = chr(0x202E)
    hostile = repo / f"notes{bidi}.txt"
    hostile.write_text("theirs\n")
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "base"], check=True)
    # Tracked AND modified: that is what lands in the untouched list. Newly
    # added is not the same thing, and testing that shape reached nothing.
    hostile.write_text("theirs, edited\n")

    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "commit",
        "--message-file",
        str(msgfile(tmp_path)),
        "--facts",
        str(written),
    )
    assert bidi not in proc.stdout, "a text-reordering character reached the untouched list"
    assert bidi not in proc.stderr
    assert bidi not in written.read_text()


def test_committed_filenames_are_cleaned_before_they_are_shown(repo, written, tmp_path):
    """commit_files() output carries the 'commit touched extra files' refusal --
    one of the two lines here most meant to stop somebody.

    Same caveat as the test above: with core.quotePath forced, git quotes the
    name before this code ever sees it, so the clean() here cannot be observed
    by any test. The property is what is pinned."""
    bidi = chr(0x202E)
    stray = repo / f"notes{bidi}.txt"
    stray.write_text("x\n")
    # Staged BEFORE the commit step, so `commit --only <managed>` still records
    # it: an index entry this run did not add is exactly the scope violation
    # commit_files exists to report.
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "stray"], check=True)

    proc = run("gitwork.py", "--dir", str(repo), "facts", "--facts", str(written), "--hash", "HEAD")
    assert proc.returncode != 0
    assert "notes" in proc.stderr, "the offending file was not even named"
    assert bidi not in proc.stderr, "a text-reordering character reached the refusal"
    assert bidi not in proc.stdout


# -- round 16 ----------------------------------------------------------------


def test_a_failed_status_during_commit_is_not_reported_as_nothing_untouched(
    repo, written, tmp_path
):
    """SKILL.md Step 5 makes this load-bearing: "say what else this run touched
    ... otherwise the user finds out from the summary, after the fact." A failed
    check used to come back as "" and so as ZERO untouched files. Silence is the
    one answer that must not be produced by an error."""
    fake = tmp_path / "failstatus"
    fake.mkdir()
    g = fake / "git"
    # Fails the repo-wide `status` (no pathspec) that builds the untouched list,
    # and forwards everything else -- including the `status ... -- <paths>` form
    # the managed-file check uses -- to the real binary.
    g.write_text(
        "#!/bin/sh\n"
        "wide=0\n"
        'for a in "$@"; do\n'
        '  [ "$a" = "status" ] && wide=1\n'
        '  [ "$a" = "--" ] && wide=0\n'
        "done\n"
        'if [ "$wide" = "1" ]; then echo "fatal: index file corrupt" >&2; exit 128; fi\n'
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)

    proc = run(
        "gitwork.py",
        "--dir",
        str(repo),
        "commit",
        "--message-file",
        str(msgfile(tmp_path)),
        "--facts",
        str(written),
        stubs=fake,
    )
    assert proc.returncode != 0
    assert "a failed check is not a clean result" in proc.stderr


# -- round 17 ----------------------------------------------------------------


def test_a_discard_command_is_safe_to_paste():
    """SKILL.md relays these as literal, copy-pasteable shell commands. Today's
    managed names are tame, but the set grows with the catalog, and a filename
    with a space turns `rm -- my file.yaml` into two arguments: data loss for
    the rm, a silent no-op for the git ones."""
    import gitwork as G

    out = G.discards({"my file.yaml": "modified", "plain.yaml": "modified"})
    assert out["my file.yaml"] == "git checkout -- 'my file.yaml'"
    assert out["plain.yaml"] == "git checkout -- plain.yaml", "no needless quoting"

    assert G.discards({"a b.txt": "untracked"})["a b.txt"] == "rm -- 'a b.txt'"
    assert (
        G.discards({"a b.txt": "staged"})["a b.txt"]
        == "git restore --staged --worktree -- 'a b.txt'"
    )


def test_the_fresh_diffstat_value_is_asserted(repo, written, tmp_path):
    """The untracked branch ("N new file(s), M lines") ran in the suite but its
    value was never read -- only that some other field came out right. The line
    count has a genuine off-by-one in it (a file not ending in a newline still
    has a last line), so it is worth pinning."""
    paths = json.loads(written.read_text())["internal"]["managed_files"]
    expected_lines = 0
    for entry in paths:
        body = (repo / entry["path"]).read_bytes()
        expected_lines += body.count(b"\n") + (1 if body and not body.endswith(b"\n") else 0)

    run("gitwork.py", "--dir", str(repo), "facts", "--facts", str(written))
    stat = json.loads(written.read_text())["net"]["diffstat"]
    assert stat == f"{len(paths)} new file(s), {expected_lines} lines", stat


def test_a_working_tree_file_that_became_unreadable_does_not_fail_the_commit(
    repo, written, tmp_path
):
    """blob_matches_verified compares the COMMITTED object first; if that
    matches, a working-tree read that then fails is a race, not a mismatch --
    the commit is already correct.

    This pins the OUTCOME (a commit whose object is right is still accepted when
    the working-tree copy has since become unreadable) but not the branch: every
    way of making read_bytes_nofollow fail from the CLI also makes the earlier
    `git hash-object` fail, which short-circuits first. Mutating the `except
    OSError` does not fail this test. Recorded rather than dressed up.
    """
    commit(repo, written, tmp_path)
    managed = json.loads(written.read_text())["internal"]["managed_files"]
    target = repo / managed[0]["path"]
    target.unlink()
    target.symlink_to(repo / "does-not-exist")  # O_NOFOLLOW refuses, hash-object still matches

    proc = run("gitwork.py", "--dir", str(repo), "facts", "--facts", str(written), "--hash", "HEAD")
    assert proc.returncode == 0, proc.stderr


def test_the_destination_line_never_shows_an_empty_url():
    """remote_push_url returns "" when both lookups fail, and remote_urls carries
    a key for every remote name regardless -- so testing membership rendered
    "origin ()" in the sentence the user approves a push against."""
    import gitwork as G

    assert G.destination({"remote": "origin", "remote_urls": {"origin": ""}}) == "origin"
    assert (
        G.destination({"remote": "origin", "remote_urls": {"origin": "git@h:/r.git"}})
        == "origin (git@h:/r.git)"
    )
    several = G.destination({"remote_urls": {"a": "", "b": "git@h:/b.git"}})
    assert "a (url unknown)" in several
    assert "b (git@h:/b.git)" in several


def test_a_diff_command_with_a_spaced_path_is_safe_to_paste(repo, written, tmp_path):
    """diff_commands is emitted verbatim for a caller to relay or paste. A path
    with a space produced a command that silently diffs two other things."""
    spaced = repo / "with space.yaml"
    spaced.write_text("x: 1\n")
    facts = json.loads(written.read_text())
    facts["internal"]["managed_files"] = [
        {"path": "with space.yaml", "sha256": hashlib.sha256(spaced.read_bytes()).hexdigest()}
    ]
    written.write_text(json.dumps(facts))

    got = out_json(run("gitwork.py", "--dir", str(repo), "status", "--facts", str(written)))
    assert got["diff_commands"], got
    assert any("'with space.yaml'" in c for c in got["diff_commands"]), got["diff_commands"]


def test_a_reverified_hash_supersedes_an_already_recorded_one(repo, written, tmp_path):
    """Reaching the assignment means the hash just passed the extra-files and
    content checks, so it is the verified answer. setdefault made it a no-op
    while diffstat below still preferred the NEW hash -- so the summary could
    show one commit's hash beside another commit's diffstat."""
    commit(repo, written, tmp_path)
    facts = json.loads(written.read_text())
    facts.setdefault("commit", {})["hash"] = "0000000"
    facts["commit"]["subject"] = "a stale subject"
    written.write_text(json.dumps(facts))

    real = git_out(repo, "rev-parse", "--short", "HEAD")
    proc = run("gitwork.py", "--dir", str(repo), "facts", "--facts", str(written), "--hash", real)
    assert proc.returncode == 0, proc.stderr
    recorded = json.loads(written.read_text())["commit"]
    assert recorded["hash"] == real, recorded
    assert recorded["subject"] != "a stale subject"
