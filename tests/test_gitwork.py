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
