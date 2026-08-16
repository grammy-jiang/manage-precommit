"""The engine, driven as a subprocess the way SKILL.md drives it."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess

import pytest

from conftest import NPM_VERSION, REAL_GIT, SKILL, out_json, run, stub_calls


def generate(repo, keys_file, facts_path, stubs, *names, force=False, scripts=None):
    args = ["--dir", str(repo), "--templates-file", str(keys_file(*names))]
    if force:
        args.append("--force")
    args += ["--facts-out", str(facts_path)]
    return run("precommit.py", *args, stubs=stubs, scripts=scripts)


# -- recommend ---------------------------------------------------------------


def test_recommend_names_the_file_that_triggered_each_entry(repo, stubs):
    (repo / "docs").mkdir()
    (repo / "docs" / "arch.md").write_text("# arch\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))

    assert got["always_on"] == ["hygiene", "yamllint"]
    by_name = {r["name"]: r["reason"] for r in got["recommended"]}
    assert by_name["markdownlint"].endswith(".md")
    assert by_name["mermaid"] == "docs/arch.md"
    assert "gitleaks" in by_name
    assert got["config"] == "none"


def test_recommend_skips_mermaid_without_a_fence(repo, stubs):
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid" not in {r["name"] for r in got["recommended"]}


def test_recommend_does_not_re_offer_what_the_config_already_has(
    repo, keys_file, facts_path, stubs
):
    generate(repo, keys_file, facts_path, stubs, "hygiene", "gitleaks")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "gitleaks" in got["previous"]
    assert "gitleaks" not in {r["name"] for r in got["recommended"]}
    assert "hygiene" not in got["proposed"]


# -- generate ----------------------------------------------------------------


def test_generate_pins_the_newest_version_tag_only(repo, keys_file, facts_path, stubs):
    """v10.0.1 beats v2.30.4 numerically, and `nightly` is not a release."""
    proc = generate(repo, keys_file, facts_path, stubs, "hygiene")
    assert proc.returncode == 0, proc.stderr
    assert out_json(proc)["versions"]["hygiene"] == "v10.0.1"
    assert "rev: v10.0.1" in (repo / ".pre-commit-config.yaml").read_text()


def test_generate_refuses_when_no_release_tag_exists(repo, keys_file, facts_path, no_tags_stub):
    proc = generate(repo, keys_file, facts_path, no_tags_stub, "hygiene")
    assert proc.returncode == 1
    assert "no version tags" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_generate_pins_the_npm_dependency_for_mermaid(repo, keys_file, facts_path, stubs):
    generate(repo, keys_file, facts_path, stubs, "mermaid")
    text = (repo / ".pre-commit-config.yaml").read_text()
    assert f"@mermaid-js/mermaid-cli@{NPM_VERSION}" in text
    assert "__NPM__" not in text


def test_generate_copies_assets_and_records_them(repo, keys_file, facts_path, stubs):
    generate(repo, keys_file, facts_path, stubs, "yamllint", "mermaid")
    assert (repo / ".yamllint.yaml").exists()
    assert (repo / "scripts" / "lint-mermaid.mjs").exists()
    facts = json.loads(facts_path.read_text())
    assert set(facts["files"]["written"]) == {
        ".pre-commit-config.yaml",
        ".yamllint.yaml",
        "scripts/lint-mermaid.mjs",
    }


def test_generate_never_overwrites_an_asset_that_is_already_there(
    repo, keys_file, facts_path, stubs
):
    (repo / ".yamllint.yaml").write_text("# mine, hands off\n")
    generate(repo, keys_file, facts_path, stubs, "yamllint")
    assert (repo / ".yamllint.yaml").read_text() == "# mine, hands off\n"
    facts = json.loads(facts_path.read_text())
    assert facts["files"]["kept"] == [".yamllint.yaml"]
    assert ".yamllint.yaml" not in facts["files"]["written"]


EXISTING = """\
# my own header comment, must survive
exclude: '^third_party/'

repos:
- repo: https://github.com/psf/black
  rev: 24.1.0          # pinned deliberately, do not bump
  hooks:
  - id: black
- repo: https://github.com/pre-commit/pre-commit-hooks
  rev: v4.0.0
  hooks:
  - id: trailing-whitespace
"""


def test_merging_leaves_every_pre_existing_byte_alone(repo, keys_file, facts_path, stubs):
    (repo / ".pre-commit-config.yaml").write_text(EXISTING)
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "cfg"], check=True)

    proc = generate(repo, keys_file, facts_path, stubs, "hygiene", "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr
    after = (repo / ".pre-commit-config.yaml").read_text()

    for line in EXISTING.splitlines():
        assert line in after.splitlines(), f"lost or altered: {line!r}"
    assert "rev: 24.1.0          # pinned deliberately, do not bump" in after
    assert after.count("- repo: https://github.com/psf/black") == 1


def test_merging_adds_missing_hooks_to_an_existing_repo_entry(repo, keys_file, facts_path, stubs):
    (repo / ".pre-commit-config.yaml").write_text(EXISTING)
    generate(repo, keys_file, facts_path, stubs, "hygiene", force=True)
    after = (repo / ".pre-commit-config.yaml").read_text()
    assert "- id: end-of-file-fixer" in after
    assert after.count("- id: trailing-whitespace") == 1  # not duplicated
    assert after.count("rev: v4.0.0") == 1  # the user's rev is not bumped


def test_merging_adopts_the_files_indent_convention(repo, keys_file, facts_path, stubs):
    """Otherwise the result violates `indent-sequences: consistent` and the
    skill's own yamllint hook rejects the file it just wrote."""
    (repo / ".pre-commit-config.yaml").write_text(EXISTING)
    generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    after = (repo / ".pre-commit-config.yaml").read_text()
    assert "\n  - id: gitleaks" in after
    assert "\n    - id: gitleaks" not in after


def test_generate_is_idempotent(repo, keys_file, facts_path, stubs):
    generate(repo, keys_file, facts_path, stubs, "hygiene", "gitleaks")
    once = (repo / ".pre-commit-config.yaml").read_text()
    proc = generate(repo, keys_file, facts_path, stubs, "hygiene", "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr
    assert (repo / ".pre-commit-config.yaml").read_text() == once
    assert any("left as-is" in line for line in out_json(proc)["report"])


def test_generate_needs_force_over_an_existing_config(repo, keys_file, facts_path, stubs):
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks")
    assert proc.returncode == 1
    assert "--force" in proc.stderr


# -- refusals ----------------------------------------------------------------


def test_unknown_key_exits_3_and_offers_the_near_match(repo, keys_file, facts_path, stubs):
    proc = generate(repo, keys_file, facts_path, stubs, "hygiene", "gitleeks")
    assert proc.returncode == 3
    assert "did you mean gitleaks" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_refuses_to_start_when_a_managed_file_is_already_dirty(repo, keys_file, facts_path, stubs):
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "cfg"], check=True)
    (repo / ".pre-commit-config.yaml").write_text(
        (repo / ".pre-commit-config.yaml").read_text() + "# my edit\n"
    )

    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode == 4
    assert "uncommitted change" in proc.stderr
    assert "# my edit" in (repo / ".pre-commit-config.yaml").read_text()


def test_untracked_is_not_dirty(repo, keys_file, facts_path, stubs):
    """An untracked config is this run's to own; only a *tracked* pending edit
    belongs to the user."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr


def test_refuses_a_config_it_cannot_read(repo, keys_file, facts_path, stubs):
    (repo / ".pre-commit-config.yaml").write_text(
        "defaults: &d\n  rev: v1\nrepos:\n  - <<: *d\n    repo: https://x/y\n"
    )
    proc = generate(repo, keys_file, facts_path, stubs, "hygiene", force=True)
    assert proc.returncode == 5
    assert "by hand" in proc.stderr


def test_refuses_a_symlinked_config(repo, keys_file, facts_path, stubs, tmp_path):
    victim = tmp_path / "elsewhere.yaml"
    victim.write_text("repos: []\n")
    (repo / ".pre-commit-config.yaml").symlink_to(victim)
    proc = generate(repo, keys_file, facts_path, stubs, "hygiene", force=True)
    assert proc.returncode != 0
    assert "symlink" in proc.stderr
    assert victim.read_text() == "repos: []\n"


def test_refuses_a_facts_path_inside_the_repo(repo, keys_file, stubs):
    inside = repo / "facts.json"
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--templates-file",
        str(keys_file("hygiene")),
        "--facts-out",
        str(inside),
        stubs=stubs,
    )
    assert proc.returncode != 0
    assert "outside the repository" in proc.stderr


# -- the facts it records ----------------------------------------------------


def test_facts_bind_each_written_file_to_its_sha256(repo, keys_file, facts_path, stubs):
    import hashlib

    generate(repo, keys_file, facts_path, stubs, "yamllint")
    managed = json.loads(facts_path.read_text())["internal"]["managed_files"]
    assert {m["path"] for m in managed} == {".pre-commit-config.yaml", ".yamllint.yaml"}
    for entry in managed:
        on_disk = (repo / entry["path"]).read_bytes()
        assert hashlib.sha256(on_disk).hexdigest() == entry["sha256"]


def test_detect_reports_what_was_written(repo, keys_file, facts_path, stubs):
    generate(repo, keys_file, facts_path, stubs, "hygiene", "mermaid")
    got = out_json(run("precommit.py", "--dir", str(repo), "--detect", stubs=stubs))
    assert got["config"] == "existing"
    assert set(got["present"]) == {"hygiene", "mermaid"}
    assert any(r["repo"] == "local" and "mermaid-lint" in r["hooks"] for r in got["repos"])


def test_catalog_lists_every_key(stubs):
    proc = run("precommit.py", "--catalog", stubs=stubs)
    keys = {line.split("\t")[0] for line in proc.stdout.splitlines() if line.strip()}
    assert keys == {"hygiene", "yamllint", "markdownlint", "mermaid", "gitleaks"}


# -- verify ------------------------------------------------------------------


def test_verify_calls_a_no_files_run_vacuous(repo, keys_file, facts_path, stubs, tmp_path):
    """`pre-commit run --all-files` exits 0 having checked nothing when the
    files are untracked. Reporting that as success is the failure this catches."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    pc = fake / "pre-commit"
    pc.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "install" ]; then echo "pre-commit installed"; exit 0; fi\n'
        'echo "trailing-whitespace..............(no files to check) Skipped"\n'
        'echo "check-yaml.......................(no files to check) Skipped"\n'
        "exit 0\n"
    )
    pc.chmod(pc.stat().st_mode | stat.S_IEXEC)
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--verify",
        "--facts",
        str(facts_path),
        stubs=fake,
    )
    got = out_json(proc)
    assert got["vacuous"] is True
    assert got["run_ok"] is False
    assert proc.returncode != 0
    assert json.loads(facts_path.read_text())["verify"]["vacuous"] is True


def test_verify_accepts_a_real_pass(repo, keys_file, facts_path, tmp_path, stubs):
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = tmp_path / "fakebin2"
    fake.mkdir()
    pc = fake / "pre-commit"
    pc.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "install" ]; then exit 0; fi\n'
        'echo "trailing-whitespace..............................Passed"\n'
        "exit 0\n"
    )
    pc.chmod(pc.stat().st_mode | stat.S_IEXEC)
    got = out_json(
        run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake)
    )
    assert got["vacuous"] is False
    assert got["run_ok"] is True
    assert got["run"] == "passed"


def test_verify_reruns_once_after_an_autofix(repo, keys_file, facts_path, tmp_path, stubs):
    """The autofixing hooks rewrite files and exit non-zero on the first run; a
    clean second pass is the success."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = tmp_path / "fakebin3"
    fake.mkdir()
    marker = tmp_path / "ran-once"
    pc = fake / "pre-commit"
    pc.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "install" ]; then exit 0; fi\n'
        f'if [ ! -f "{marker}" ]; then\n'
        f'  : > "{marker}"\n'
        f'  echo "fixed" >> "{repo}/README.md"\n'
        '  echo "trailing-whitespace......................Failed"\n'
        "  exit 1\n"
        "fi\n"
        'echo "trailing-whitespace......................Passed"\n'
        "exit 0\n"
    )
    pc.chmod(pc.stat().st_mode | stat.S_IEXEC)
    got = out_json(
        run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake)
    )
    assert got["run_ok"] is True
    assert got["autofixed"] == ["README.md"]
    assert "second run" in got["run"]


@pytest.mark.parametrize("mode", ["--detect", "--recommend"])
def test_read_only_modes_write_nothing(repo, stubs, mode):
    before = sorted(p.name for p in repo.iterdir())
    run("precommit.py", "--dir", str(repo), mode, stubs=stubs)
    assert sorted(p.name for p in repo.iterdir()) == before


# -- guards added after round 1 of the reviewer panel -------------------------


def test_an_asset_is_never_written_through_a_symlinked_directory(
    repo, keys_file, facts_path, stubs, tmp_path
):
    """Pins the arbitrary-write hole found in review.

    copy_assets used os.makedirs + shutil.copyfile, which follow a symlink at
    every component. A repo shipping a `scripts` symlink plus any Markdown file
    with a mermaid fence (enough for detect_markers to recommend the entry) got
    lint-mermaid.mjs written wherever that symlink pointed -- in Step 3, before
    any diff, commit or push confirmation.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "scripts").symlink_to(outside)

    proc = generate(repo, keys_file, facts_path, stubs, "mermaid")
    assert proc.returncode != 0
    assert "symlink" in proc.stderr
    assert list(outside.iterdir()) == [], "wrote through the symlink"


def test_an_asset_is_never_written_outside_the_repo(repo, keys_file, facts_path, stubs, tmp_path):
    outside = tmp_path / "escape"
    outside.mkdir()
    (repo / "scripts").symlink_to(outside)
    generate(repo, keys_file, facts_path, stubs, "mermaid")
    assert not (outside / "lint-mermaid.mjs").exists()


def test_a_failed_dirty_check_is_not_treated_as_clean(repo, keys_file, facts_path, stubs, tmp_path):
    """`git status` failing is not the same as finding nothing dirty.

    Returning on a non-zero status silently merged into whatever the user had
    in progress -- the exact thing refuse_if_dirty exists to prevent.
    """
    fake = tmp_path / "brokengit"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "status" ]; then echo "fatal: index file corrupt" >&2; exit 128; fi\n'
        '  if [ "$a" = "ls-remote" ]; then printf \'a\\trefs/tags/v1.0.0\\n\'; exit 0; fi\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    proc = generate(repo, keys_file, facts_path, fake, "hygiene")
    assert proc.returncode == 4
    assert "not a clean result" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_recommend_seeds_the_facts_file(repo, facts_path, stubs):
    """The worked example documents SCAN detected and HOOKS recommended rows;
    before this they were unproducible, because --recommend wrote no facts."""
    run(
        "precommit.py",
        "--dir",
        str(repo),
        "--recommend",
        "--facts-out",
        str(facts_path),
        stubs=stubs,
    )
    facts = json.loads(facts_path.read_text())
    assert facts["scan"]["detected"]
    assert any(r["name"] == "markdownlint" for r in facts["hooks"]["recommended"])


def test_generate_merges_into_the_facts_recommend_seeded(repo, keys_file, facts_path, stubs):
    run(
        "precommit.py",
        "--dir",
        str(repo),
        "--recommend",
        "--facts-out",
        str(facts_path),
        stubs=stubs,
    )
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    facts = json.loads(facts_path.read_text())
    assert facts["scan"]["detected"], "the scan rows were clobbered by the write step"
    assert facts["hooks"]["recommended"], "the recommendation rows were clobbered"
    assert facts["hooks"]["added"], "the write step's own rows are missing"
    assert facts["internal"]["managed_files"]


# -- guards added after round 2 of the reviewer panel -------------------------


def test_a_symlinked_markdown_file_is_never_read(repo, stubs, tmp_path):
    """Pins the read-side twin of the round-1 write hole.

    walk_repo lists symlinks -- git tracks them as ordinary blobs -- and
    detect_markers opened each candidate with builtin open(), which follows the
    link. A tracked `notes.md -> ~/.ssh/id_rsa` was read and scanned during
    --recommend, the first and entirely unconfirmed step of a run.
    """
    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN PRIVATE KEY-----\n```mermaid\ngraph TD;\n```\n")
    (repo / "notes.md").symlink_to(secret)

    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    # The fence exists only inside the symlink's target, so recommending
    # mermaid would prove the target had been read.
    assert "mermaid" not in {r["name"] for r in got["recommended"]}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_named_pipe_markdown_file_does_not_hang_the_scan(repo, stubs):
    """Reading a FIFO with no writer blocks forever -- a one-file denial of
    service on the first step of a run."""
    os.mkfifo(repo / "pipe.md")
    proc = run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs)
    assert proc.returncode == 0
    assert out_json(proc)["always_on"]


def test_verify_rehashes_files_its_own_hooks_rewrote(repo, keys_file, facts_path, stubs, tmp_path):
    """Step 4's autofixers act on whole-file content, so this run's own files
    can be among what they rewrite -- and the commit gate would then refuse a
    change this run itself caused, indistinguishably from tampering."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    before = json.loads(facts_path.read_text())["internal"]["managed_files"][0]["sha256"]

    fake = tmp_path / "fixer"
    fake.mkdir()
    pc = fake / "pre-commit"
    pc.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "install" ]; then exit 0; fi\n'
        'echo "# autofixed" >> .pre-commit-config.yaml\n'
        'echo "end-of-file-fixer.....Passed"\n'
        "exit 0\n"
    )
    pc.chmod(pc.stat().st_mode | stat.S_IEXEC)
    run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake)

    after = json.loads(facts_path.read_text())["internal"]["managed_files"][0]["sha256"]
    assert after != before, "the recorded hash still describes the pre-autofix file"
    assert after == hashlib.sha256((repo / ".pre-commit-config.yaml").read_bytes()).hexdigest()


def test_verify_is_not_vacuous_when_only_some_hooks_had_no_files(
    repo, keys_file, facts_path, stubs, tmp_path
):
    """A mutant turning all() into any() would otherwise stay green."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = tmp_path / "mixed"
    fake.mkdir()
    pc = fake / "pre-commit"
    pc.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "install" ]; then exit 0; fi\n'
        'echo "trailing-whitespace..........................Passed"\n'
        'echo "check-json...........(no files to check) Skipped"\n'
        "exit 0\n"
    )
    pc.chmod(pc.stat().st_mode | stat.S_IEXEC)
    got = out_json(
        run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake)
    )
    assert got["vacuous"] is False
    assert got["run_ok"] is True


def test_generate_extends_a_user_authored_empty_repos_list(repo, keys_file, facts_path, stubs):
    """The one documented exception to never touching a byte outside an
    inserted block: a flow-style empty list cannot be appended to."""
    (repo / ".pre-commit-config.yaml").write_text(
        'minimum_pre_commit_version: "4.0.0"\nrepos: []\n'
    )
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "cfg"], check=True)

    proc = generate(repo, keys_file, facts_path, stubs, "hygiene", force=True)
    assert proc.returncode == 0, proc.stderr
    after = (repo / ".pre-commit-config.yaml").read_text()
    assert "repos: []" not in after
    assert "- id: trailing-whitespace" in after
    assert "normalised" in proc.stderr


# -- guards added after round 3 of the reviewer panel -------------------------

BIDI = chr(0x202E)  # right-to-left override, built from its codepoint


def test_generate_asks_about_the_right_repo_for_each_catalog_key(
    repo, keys_file, facts_path, stubs
):
    """A stub that answers the same thing for every URL cannot catch a swapped
    rev_repo, which is precisely what version pinning exists to get right."""
    generate(repo, keys_file, facts_path, stubs, "hygiene", "gitleaks", "mermaid")
    calls = stub_calls(stubs)
    assert "https://github.com/pre-commit/pre-commit-hooks" in calls
    assert "https://github.com/gitleaks/gitleaks" in calls
    # By content, not by argument order: npm accepts its flags anywhere, and an
    # assertion that pins their position fails on a rearrangement that changed
    # nothing. The isolated cache itself has its own test.
    npm_view = [ln for ln in calls.splitlines() if ln.startswith("npm view ")]
    assert len(npm_view) == 1
    assert "@mermaid-js/mermaid-cli" in npm_view[0]
    assert "--cache" in npm_view[0]
    # And the pinned values are the ones that repo offered, not another's.
    versions = json.loads(facts_path.read_text())["hooks"]["versions"]
    assert versions["hygiene"] == "v10.0.1"
    assert versions["gitleaks"] == "v8.30.1"


def test_a_crlf_config_keeps_its_line_endings(repo, keys_file, facts_path, stubs):
    """splitlines() drops the terminator, so rejoining with a hardcoded "\\n"
    rewrote every line of a CRLF file while verify_additive -- which compares
    the already-stripped lines -- still called the merge purely additive."""
    original = "exclude: 'vendor/'\r\nrepos:\r\n- repo: https://github.com/psf/black\r\n  rev: 24.1.0\r\n  hooks:\r\n  - id: black\r\n"
    (repo / ".pre-commit-config.yaml").write_bytes(original.encode())
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "cfg"], check=True)

    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr
    after = (repo / ".pre-commit-config.yaml").read_bytes()
    assert b"\r\n" in after
    assert b"\n" not in after.replace(b"\r\n", b""), "a bare LF was introduced"
    assert b"gitleaks" in after


def test_a_config_without_a_trailing_newline_does_not_gain_one(repo, keys_file, facts_path, stubs):
    (repo / ".pre-commit-config.yaml").write_bytes(b"repos:\n- repo: local\n  hooks:\n  - id: x")
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr
    assert not (repo / ".pre-commit-config.yaml").read_bytes().endswith(b"\n\n")


def test_verify_reports_a_non_utf8_facts_file_cleanly(repo, keys_file, facts_path, stubs):
    """The decode used to sit outside the try, so this was a raw traceback."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    facts_path.write_bytes(b"\xff\xfe not utf-8")
    proc = run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path))
    assert proc.returncode != 0
    assert "cannot read facts file" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_a_forged_character_this_run_would_insert_stops_the_write(
    repo, keys_file, facts_path, stubs, skill_copy
):
    """The check is that WE never introduce one.

    It used to scan the whole merged file, which meant a single pre-existing
    zero-width joiner -- the character that holds a compound emoji together in
    an ordinary comment -- refused every future run permanently, with no line
    number and no entry in the error table. A character the user already had is
    theirs; `--detect` reports it instead.

    Corrupts a COPY of the skill tree. This used to write the forged bytes over
    the real shipped templates/gitleaks.yaml and restore in a `finally` -- so
    any interruption in between (SIGKILL, an xdist worker crash, --timeout,
    Ctrl-C) left a poisoned template in the working tree, one `git add -A` from
    being shipped into other people's repositories.
    """
    fragment = skill_copy / "templates" / "gitleaks.yaml"
    original = fragment.read_bytes()
    fragment.write_bytes(original.replace(b"gitleaks\n", "gitleaks\u202e\n".encode(), 1))

    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", scripts=skill_copy / "scripts")
    assert proc.returncode != 0
    assert "insert" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists()
    assert (SKILL / "templates" / "gitleaks.yaml").read_bytes() == original, (
        "the real shipped template was touched"
    )


def test_a_pre_existing_forged_character_is_reported_not_blocked(
    repo, keys_file, facts_path, stubs
):
    """Refusing here locked the user out of their own repository forever."""
    zwj = chr(0x200D)
    (repo / ".pre-commit-config.yaml").write_text(
        f"# by the maintainer {zwj}\nrepos:\n- repo: local\n  hooks:\n  - id: x\n"
    )
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr
    assert "gitleaks" in (repo / ".pre-commit-config.yaml").read_text()

    got = out_json(run("precommit.py", "--dir", str(repo), "--detect", stubs=stubs))
    assert got["suspicious_characters"] is True, "it should still be reported"


def test_a_hooks_list_that_cannot_be_extended_reaches_the_facts(repo, keys_file, facts_path, stubs):
    """This outcome printed to stderr but never reached facts, so the closing
    summary silently omitted that a hook needs adding by hand."""
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n- repo: https://github.com/pre-commit/pre-commit-hooks\n  rev: v4.0.0\n  hooks:\n"
    )
    proc = generate(repo, keys_file, facts_path, stubs, "hygiene", force=True)
    assert proc.returncode == 0, proc.stderr
    needs = json.loads(facts_path.read_text())["hooks"]["needs_manual"]
    assert needs and "by hand" in needs[0]


def test_hooks_added_to_an_existing_entry_reach_the_facts(repo, keys_file, facts_path, stubs):
    """Classified by substring-matching prose, this outcome matched neither
    bucket and vanished from the summary."""
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n- repo: https://github.com/pre-commit/pre-commit-hooks\n"
        "  rev: v4.0.0\n  hooks:\n  - id: trailing-whitespace\n"
    )
    generate(repo, keys_file, facts_path, stubs, "hygiene", force=True)
    added = json.loads(facts_path.read_text())["hooks"]["added"]
    assert any("added hooks" in line for line in added)


def _fake_bin(tmp_path, name, script):
    d = tmp_path / name
    d.mkdir()
    exe = d / ("npm" if name.startswith("npm") else "pre-commit")
    exe.write_text(script)
    exe.chmod(0o755)
    return d


def test_a_failing_npm_stops_the_run(repo, keys_file, facts_path, tmp_path, stubs):
    fake = _fake_bin(tmp_path, "npmfail", '#!/bin/sh\necho "boom" >&2\nexit 1\n')
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode != 0
    assert "npm view" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_a_garbage_npm_version_is_refused(repo, keys_file, facts_path, tmp_path, stubs):
    """An unchecked value would be substituted straight into the config."""
    fake = _fake_bin(tmp_path, "npmjunk", "#!/bin/sh\necho not-a-version\n")
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode != 0
    assert "unexpected version" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_mermaid_pin_uses_an_isolated_temporary_npm_cache(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """Changing cwd does not change where npm caches.

    A sandbox that hands the process `NPM_CONFIG_CACHE=/root/.npm` and no way to
    create it turned the version pin into `ENOENT mkdir`, and the pin happens
    before anything is written -- so one unwritable directory aborted the whole
    run. The stub proves all of it at once: the hostile value really was
    inherited (or it exits 4), the proxy variables the sandbox needs to reach
    the registry survived, and a warning on stderr with exit 0 is not a failure.
    """
    fake = _fake_bin(
        tmp_path,
        "npminspect",
        "#!/bin/sh\n"
        "set -eu\n"
        'cache=""\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--cache" ]; then cache="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        '[ -n "$cache" ] || { echo "missing --cache" >&2; exit 2; }\n'
        '[ "${HTTPS_PROXY:-}" = "http://proxy.example:8080" ] || {\n'
        '  echo "proxy env missing" >&2; exit 3;\n'
        "}\n"
        '[ "${NPM_CONFIG_CACHE:-}" = "/root/.npm" ] || {\n'
        '  echo "expected inherited cache var for test" >&2; exit 4;\n'
        "}\n"
        'mkdir -p "$cache"\n'
        'touch "$cache/probe"\n'
        'printf "%s" "$cache" > "$MP_CACHE_LOG"\n'
        'echo "npm warn Unknown env config \\"http-proxy\\". This will stop working in the next major version of npm." >&2\n'
        "echo 11.99.0\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    cache_log = tmp_path / "cache-path.txt"
    monkeypatch.setenv("MP_CACHE_LOG", str(cache_log))
    monkeypatch.setenv("NPM_CONFIG_CACHE", "/root/.npm")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")

    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 0, proc.stderr
    cache = cache_log.read_text()
    assert cache
    assert cache != "/root/.npm"
    assert not os.path.exists(cache), "temporary npm cache should be cleaned up"
    assert (
        f"@mermaid-js/mermaid-cli@{NPM_VERSION}" in (repo / ".pre-commit-config.yaml").read_text()
    )


def test_the_temporary_npm_cache_is_removed_when_the_pin_fails(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """The failing path is the one that leaks: a cache left behind on every
    refused pin fills the disk of exactly the machine that could not write to
    the ordinary one."""
    fake = _fake_bin(
        tmp_path,
        "npmleak",
        "#!/bin/sh\n"
        "set -eu\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--cache" ]; then printf "%s" "$a" > "$MP_CACHE_LOG"; mkdir -p "$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'echo "registry unreachable" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    cache_log = tmp_path / "leaked-path.txt"
    monkeypatch.setenv("MP_CACHE_LOG", str(cache_log))

    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode != 0
    assert not (repo / ".pre-commit-config.yaml").exists()
    cache = cache_log.read_text()
    assert cache
    assert not os.path.exists(cache), "the cache outlived a failed pin"


ANSWERING_NPM = (
    '#!/bin/sh\necho "npm $*" >> "$MP_NPM_LOG"\n'
    'if [ "$1" = "config" ]; then printf "%s\\n" "$MP_NPM_CACHE"; exit 0; fi\n'
    "exit 9\n"
)


def _verify_bin(tmp_path, name, stubs, npm=ANSWERING_NPM):
    """A PATH whose `pre-commit` reports the npm cache it was handed.

    Its `npm` answers `config get cache` and refuses everything else: --verify
    pins no versions, so any other call is a stray one and should be loud.
    `npm=None` leaves npm off this PATH entirely.
    """
    d = tmp_path / name
    d.mkdir()
    pc = d / "pre-commit"
    pc.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "install" ]; then exit 0; fi\n'
        'printf "%s" "${NPM_CONFIG_CACHE-<unset>}" > "$MP_ENV_LOG"\n'
        '[ -z "${MP_ENV_COUNT-}" ] || env | grep -ic "^npm_config_cache=" > "$MP_ENV_COUNT"\n'
        '[ -z "${NPM_CONFIG_CACHE-}" ] || mkdir -p "$NPM_CONFIG_CACHE" 2>/dev/null\n'
        'echo "mermaid-lint.............................................Passed"\n'
        "exit 0\n"
    )
    pc.chmod(0o755)
    if npm is not None:
        exe = d / "npm"
        exe.write_text(npm)
        exe.chmod(0o755)
    (d / "git").symlink_to(stubs / "git")
    return d


def test_verify_hands_precommit_a_writable_npm_cache_when_the_inherited_one_is_not(
    repo, keys_file, facts_path, stubs, tmp_path, monkeypatch
):
    """The version pin was only the first npm call.

    `mermaid` is a `language: node` hook with additional_dependencies, so
    pre-commit builds its environment by running `npm install` -- and pre-commit
    sets npm_config_prefix and unsets npm_config_userconfig, but never the
    cache. The same unwritable directory therefore killed the hook install one
    step later, with the config already written. Blocked here by a path under a
    regular file rather than by permissions, so it holds when the suite runs as
    root.

    The inherited value is set in both spellings on purpose: npm lower-cases
    `npm_config_*` environment names, so a new `NPM_CONFIG_CACHE` left beside an
    inherited `npm_config_cache` gives npm two values for one key and hands the
    outcome to process.env's ordering. The child must see exactly one.
    """
    generate(repo, keys_file, facts_path, stubs, "mermaid")
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    poisoned = str(blocker / ".npm")
    env_log = tmp_path / "child-cache.txt"
    env_count = tmp_path / "child-cache-count.txt"
    fake = _verify_bin(tmp_path, "verifyblocked", stubs)
    monkeypatch.setenv("MP_ENV_LOG", str(env_log))
    monkeypatch.setenv("MP_ENV_COUNT", str(env_count))
    monkeypatch.setenv("MP_NPM_LOG", str(tmp_path / "npm-calls.txt"))
    monkeypatch.setenv("MP_NPM_CACHE", poisoned)
    monkeypatch.setenv("NPM_CONFIG_CACHE", poisoned)
    monkeypatch.setenv("npm_config_cache", poisoned)

    proc = run(
        "precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake
    )
    assert proc.returncode == 0, proc.stderr
    handed = env_log.read_text()
    assert handed not in (poisoned, "<unset>")
    assert not os.path.exists(handed), "the scratch npm cache should be cleaned up"
    assert env_count.read_text().strip() == "1", "npm was left two values for one key"


def test_verify_leaves_a_usable_npm_cache_alone(
    repo, keys_file, facts_path, stubs, tmp_path, monkeypatch
):
    """Redirecting unconditionally would discard a warm cache on every run and
    re-download the CLI to gain nothing."""
    generate(repo, keys_file, facts_path, stubs, "mermaid")
    warm = tmp_path / "warm-npm-cache"
    warm.mkdir()
    env_log = tmp_path / "child-cache.txt"
    fake = _verify_bin(tmp_path, "verifywarm", stubs)
    monkeypatch.setenv("MP_ENV_LOG", str(env_log))
    monkeypatch.setenv("MP_NPM_LOG", str(tmp_path / "npm-calls.txt"))
    monkeypatch.setenv("MP_NPM_CACHE", str(warm))
    monkeypatch.setenv("NPM_CONFIG_CACHE", str(warm))

    proc = run(
        "precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake
    )
    assert proc.returncode == 0, proc.stderr
    assert env_log.read_text() == str(warm)


@pytest.mark.parametrize(
    "npm",
    [
        pytest.param(
            '#!/bin/sh\necho "npm $*" >> "$MP_NPM_LOG"\nexit 1\n',
            id="npm refuses the question",
        ),
        pytest.param(
            "#!/nonexistent/interpreter\n",
            id="npm cannot be executed",
        ),
        pytest.param(None, id="npm is not installed"),
    ],
)
def test_verify_inherits_the_npm_cache_when_npm_will_not_say_where_it_is(
    repo, keys_file, facts_path, stubs, tmp_path, monkeypatch, npm
):
    """Unanswered is not the same as unusable.

    Redirecting on a guess would discard a warm cache every run on any machine
    whose npm answers oddly, to fix a problem that may not exist. The inherited
    setting is the status quo, and the status quo is what an undiagnosed run
    keeps.

    `only_path`, for the unrunnable case especially: exec walks PATH and moves
    on to the next candidate when one will not start, so with the real npm
    still behind the stub this test would pass by asking the real one.
    """
    generate(repo, keys_file, facts_path, stubs, "mermaid")
    warm = tmp_path / "warm-npm-cache"
    warm.mkdir()
    env_log = tmp_path / "child-cache.txt"
    fake = _verify_bin(tmp_path, "verifymute", stubs, npm=npm)
    monkeypatch.setenv("MP_ENV_LOG", str(env_log))
    monkeypatch.setenv("MP_NPM_LOG", str(tmp_path / "npm-calls.txt"))
    monkeypatch.setenv("NPM_CONFIG_CACHE", str(warm))

    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--verify",
        "--facts",
        str(facts_path),
        stubs=fake,
        only_path=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert env_log.read_text() == str(warm)


def test_verify_replaces_an_npm_cache_answer_that_is_not_an_absolute_path(
    repo, keys_file, facts_path, stubs, tmp_path, monkeypatch
):
    """A relative answer would be judged against the wrong directory.

    It resolves against *this* process's cwd, not the cwd npm will run with, so
    a writable match there says nothing about the directory npm would use --
    which is why this run has a real, writable `warm` sitting in its own cwd and
    must still refuse it.
    """
    generate(repo, keys_file, facts_path, stubs, "mermaid")
    warm = tmp_path / "warm-npm-cache"
    warm.mkdir()
    env_log = tmp_path / "child-cache.txt"
    fake = _verify_bin(tmp_path, "verifyrelative", stubs)
    monkeypatch.setenv("MP_ENV_LOG", str(env_log))
    monkeypatch.setenv("MP_NPM_LOG", str(tmp_path / "npm-calls.txt"))
    monkeypatch.setenv("MP_NPM_CACHE", warm.name)

    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--verify",
        "--facts",
        str(facts_path),
        stubs=fake,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    handed = env_log.read_text()
    assert handed not in (warm.name, str(warm), "<unset>")
    assert os.path.isabs(handed)


def test_verify_asks_npm_nothing_for_a_config_with_no_npm_backed_entry(
    repo, keys_file, facts_path, stubs, tmp_path, monkeypatch
):
    """A repo with no node hook should not pay for a subprocess, and should not
    need npm installed at all to be verified."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    npm_log = tmp_path / "npm-calls.txt"
    fake = _verify_bin(tmp_path, "verifynonode", stubs)
    monkeypatch.setenv("MP_ENV_LOG", str(tmp_path / "child-cache.txt"))
    monkeypatch.setenv("MP_NPM_LOG", str(npm_log))
    monkeypatch.setenv("MP_NPM_CACHE", "/tmp")

    proc = run(
        "precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake
    )
    assert proc.returncode == 0, proc.stderr
    assert not npm_log.exists(), f"npm was asked: {npm_log.read_text()}"


def test_verify_reports_hooks_that_keep_failing(repo, keys_file, facts_path, stubs, tmp_path):
    """The 'your hooks are broken' outcome the tool exists to report."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = tmp_path / "alwaysfail"
    fake.mkdir()
    pc = fake / "pre-commit"
    pc.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "install" ]; then exit 0; fi\n'
        'echo "gitleaks.................................Failed"\n'
        "exit 1\n"
    )
    pc.chmod(0o755)
    proc = run(
        "precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake
    )
    got = out_json(proc)
    assert got["run_ok"] is False
    assert got["vacuous"] is False
    assert got["run"] == "failed (exit 1)"
    assert got["autofixed"] == []
    assert proc.returncode != 0


def test_a_config_with_a_utf8_bom_is_read_not_refused(repo, keys_file, facts_path, stubs):
    """A file saved as "UTF-8 with BOM" folded the mark into its first key, so
    `repos:` read as a different key and the config was refused for having no
    `repos:` at all."""
    body = "repos:\n- repo: local\n  hooks:\n  - id: mine\n"
    (repo / ".pre-commit-config.yaml").write_bytes(b"\xef\xbb\xbf" + body.encode())
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr
    assert "gitleaks" in (repo / ".pre-commit-config.yaml").read_text()


def test_verify_separates_the_file_list_from_pre_commits_own_options(
    repo, keys_file, facts_path, stubs, tmp_path
):
    """`--files` values are argv for pre-commit, which reads a leading dash as
    one of its own options. argparse blocks the obvious injection at this
    script's own boundary, so what actually has to hold is the `--` terminator
    in the command pre-commit receives -- checked here by having it log argv.
    """
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = tmp_path / "logargs"
    fake.mkdir()
    log = fake / "argv.log"
    pc_stub = fake / "pre-commit"
    pc_stub.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        'if [ "$1" = "install" ]; then exit 0; fi\n'
        'echo "trailing-whitespace......Passed"\n'
        "exit 0\n"
    )
    pc_stub.chmod(0o755)
    run(
        "precommit.py",
        "--dir",
        str(repo),
        "--verify",
        "--facts",
        str(facts_path),
        "--files",
        ".pre-commit-config.yaml",
        stubs=fake,
    )
    invocation = log.read_text()
    assert "run --files -- .pre-commit-config.yaml" in invocation, invocation


def test_generate_works_in_a_directory_that_is_not_a_repo(tmp_path, keys_file, facts_path, stubs):
    """The first step of the documented is_repo:false workflow. refuse_if_dirty's
    "nothing can be pending" branch had never been taken by a test."""
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = run(
        "precommit.py",
        "--dir",
        str(plain),
        "--templates-file",
        str(keys_file("hygiene")),
        "--facts-out",
        str(facts_path),
        stubs=stubs,
    )
    assert proc.returncode == 0, proc.stderr
    assert (plain / ".pre-commit-config.yaml").exists()
    assert json.loads(facts_path.read_text())["scan"]["git_repo"] is False


@pytest.mark.parametrize("mode", ["--detect", "--recommend"])
def test_read_only_modes_work_outside_a_repo(tmp_path, stubs, mode):
    plain = tmp_path / "plain2"
    plain.mkdir()
    proc = run("precommit.py", "--dir", str(plain), mode, stubs=stubs)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("bad", ["/etc/hosts", "../escape.txt", "../../etc/hosts"])
def test_verify_refuses_a_files_value_outside_the_repo(repo, keys_file, facts_path, stubs, bad):
    """pre-commit resolves --files against cwd, so an absolute path or a ../
    traversal points the autofixing hooks at a file outside the tree (which
    they rewrite) and gitleaks at one it reads and prints."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--verify",
        "--facts",
        str(facts_path),
        "--files",
        bad,
        stubs=stubs,
    )
    assert proc.returncode != 0
    assert "outside the repository" in proc.stderr


def test_an_asset_destination_that_escapes_the_repo_is_refused(tmp_path):
    """The containment half of the asset guard, reached directly.

    The symlink test above trips the per-component islink check first, so this
    branch never ran. No catalog entry has a traversing relative path today --
    the guard is there so one added later cannot quietly write outside the
    tree, and an unreachable guard with no test is indistinguishable from one
    that does not work.
    """
    import precommit

    repo = tmp_path / "r"
    repo.mkdir()
    with pytest.raises(SystemExit) as exc:
        precommit.refuse_path_escaping_repo(str(repo), "../../escaped.mjs")
    assert exc.value.code != 0


# -- guards added after round 6 of the reviewer panel -------------------------


def _pre_commit_stub(tmp_path, name, lines, exit_code=0):
    d = tmp_path / name
    d.mkdir()
    exe = d / "pre-commit"
    body = "".join(f'echo "{ln}"\n' for ln in lines)
    exe.write_text(f'#!/bin/sh\nif [ "$1" = "install" ]; then exit 0; fi\n{body}exit {exit_code}\n')
    exe.chmod(0o755)
    return d


def test_a_green_run_is_not_a_pass_if_a_file_scoped_hook_saw_nothing(
    repo, keys_file, facts_path, stubs, tmp_path
):
    """is_vacuous is all-or-nothing, so hygiene's hooks alone flip a run green
    while markdownlint -- added because a .md was detected -- sat idle."""
    (repo / "doc.md").write_text("# hi\n")
    generate(repo, keys_file, facts_path, stubs, "hygiene", "markdownlint")
    fake = _pre_commit_stub(
        tmp_path,
        "partial",
        [
            "trailing-whitespace..............................Passed",
            "markdownlint-cli2....(no files to check) Skipped",
        ],
    )
    got = out_json(
        run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake)
    )
    assert got["vacuous"] is False, "one hook did run, so the whole output is not vacuous"
    assert got["unchecked"] == ["markdownlint-cli2"]
    assert got["run_ok"] is False
    assert "never exercised" in got["run"]


def test_a_quiet_hygiene_hook_is_not_a_coverage_gap(repo, keys_file, facts_path, stubs, tmp_path):
    """check-json having nothing to do in a repo with no JSON is ordinary --
    flagging it would make almost every run non-green."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = _pre_commit_stub(
        tmp_path,
        "quiet",
        [
            "trailing-whitespace..............................Passed",
            "check-json...........(no files to check) Skipped",
        ],
    )
    got = out_json(
        run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake)
    )
    assert got["unchecked"] == []
    assert got["run_ok"] is True


def test_generate_records_which_added_hooks_are_file_scoped(repo, keys_file, facts_path, stubs):
    (repo / "doc.md").write_text("# hi\n")
    generate(repo, keys_file, facts_path, stubs, "hygiene", "markdownlint")
    hooks = json.loads(facts_path.read_text())["hooks"]
    assert "trailing-whitespace" in hooks["added_ids"]
    assert hooks["scoped_ids"] == ["markdownlint-cli2"]


def test_vendored_content_does_not_drive_the_recommendation(repo, stubs):
    """SKIP_DIRS exists so a vendored .md cannot decide what this repo needs."""
    for skipped in ("node_modules/pkg", ".venv/lib", "vendor/dep"):
        d = repo / skipped
        d.mkdir(parents=True)
        (d / "README.md").write_text("# vendored\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    (repo / "README.md").unlink()

    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    names = {r["name"] for r in got["recommended"]}
    assert "markdownlint" not in names
    assert "mermaid" not in names
    assert got["detected"] == []


def test_a_top_level_markdown_file_still_drives_it(repo, stubs):
    """The companion to the exclusion above: it must not exclude everything."""
    (repo / "doc.md").write_text("# hi\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    names = {r["name"] for r in got["recommended"]}
    assert {"markdownlint", "mermaid"} <= names


def test_recommend_reports_bare_paths_alongside_the_prose(repo, stubs, facts_path):
    """`detected` is for a human; `detected_paths` is what can be passed to a
    command. Passing the prose made pre-commit look for a file called
    "markdown (README.md)" and quietly check nothing."""
    (repo / "doc.md").write_text("# hi\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    got = out_json(
        run(
            "precommit.py",
            "--dir",
            str(repo),
            "--recommend",
            "--facts-out",
            str(facts_path),
            stubs=stubs,
        )
    )
    assert any("(" in m for m in got["detected"]), "detected should stay human-readable"
    assert all("(" not in p for p in got["detected_paths"])
    assert set(got["detected_paths"]) <= {"README.md", "doc.md"}
    for path in got["detected_paths"]:
        assert (repo / path).exists()
    assert json.loads(facts_path.read_text())["scan"]["detected_paths"] == got["detected_paths"]


def test_verify_reads_the_file_list_from_a_file(repo, keys_file, facts_path, stubs, tmp_path):
    """Repo filenames can carry backticks and semicolons, so they must not be
    typed into the command the agent runs."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    listing = tmp_path / "paths.txt"
    listing.write_text(".pre-commit-config.yaml\nREADME.md\n")
    fake = tmp_path / "logargs2"
    fake.mkdir()
    log = fake / "argv.log"
    pc_stub = fake / "pre-commit"
    pc_stub.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        'if [ "$1" = "install" ]; then exit 0; fi\n'
        'echo "trailing-whitespace......Passed"\n'
        "exit 0\n"
    )
    pc_stub.chmod(0o755)
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--verify",
        "--facts",
        str(facts_path),
        "--files-file",
        str(listing),
        stubs=fake,
    )
    assert proc.returncode == 0, proc.stderr
    assert "run --files -- .pre-commit-config.yaml README.md" in log.read_text()


def test_files_and_files_file_are_mutually_exclusive(repo, keys_file, facts_path, stubs, tmp_path):
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    listing = tmp_path / "p.txt"
    listing.write_text("README.md\n")
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--verify",
        "--facts",
        str(facts_path),
        "--files",
        "README.md",
        "--files-file",
        str(listing),
        stubs=stubs,
    )
    assert proc.returncode != 0
    assert "not both" in proc.stderr


def test_detect_neutralises_what_it_relays(repo, stubs):
    """Step 0 relays this JSON straight to the user, before summary.py ever
    sanitises anything."""
    bidi = chr(0x202E)
    (repo / ".pre-commit-config.yaml").write_text(
        f"repos:\n  - repo: 'local{bidi}'\n    hooks:\n      - id: 'x{bidi}y'\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--detect", stubs=stubs))
    blob = json.dumps(got)
    assert bidi not in blob, "a text-reordering character reached the relayed JSON"
    assert got["suspicious_characters"] is True


def test_a_pre_existing_exclude_has_its_pattern_shown(repo, keys_file, facts_path, stubs):
    """A config arriving with `exclude: '.*'` switches every hook off. The line
    is pre-existing, so it appears in no inserted hunk and the Step 5 diff never
    shows it -- if the report does not say it, nobody sees it."""
    (repo / ".pre-commit-config.yaml").write_text(
        "exclude: '.*'\nrepos:\n  - repo: local\n    hooks:\n      - id: x\n"
    )
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr
    kept = json.loads(facts_path.read_text())["hooks"]["left_as_is"]
    line = next(k for k in kept if k.startswith("exclude:"))
    assert "pattern: .*" in line, line
    assert "EVERY hook" in line


def test_detect_reports_the_exclude_pattern(repo, stubs):
    (repo / ".pre-commit-config.yaml").write_text(
        "exclude: '^vendor/'\nrepos:\n  - repo: local\n    hooks:\n      - id: x\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--detect", stubs=stubs))
    assert got["exclude"] == "^vendor/"


def test_detect_reports_no_exclude_when_there_is_none(repo, stubs):
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: x\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--detect", stubs=stubs))
    assert got["exclude"] is None


# -- guards added after round 9 of the reviewer panel -------------------------


def test_a_foreign_file_at_the_executed_asset_path_stops_the_run(
    repo, keys_file, facts_path, stubs
):
    """The mermaid fragment hardcodes `entry: node scripts/lint-mermaid.mjs`, so
    a file already at that path is not data the hook reads -- it is the program
    the hook runs. Keeping it wires attacker-authored JS into every commit, and
    because the file is unchanged it shows up in no diff at all."""
    (repo / "scripts").mkdir()
    (repo / "scripts" / "lint-mermaid.mjs").write_text("// not ours\n")
    proc = generate(repo, keys_file, facts_path, stubs, "mermaid")
    assert proc.returncode != 0
    assert "NOT the file this skill ships" in proc.stderr
    assert (repo / "scripts" / "lint-mermaid.mjs").read_text() == "// not ours\n"


def test_an_identical_file_at_that_path_is_fine(repo, keys_file, facts_path, stubs):
    """Re-running must not trip over the asset the previous run wrote."""
    generate(repo, keys_file, facts_path, stubs, "mermaid")
    proc = generate(repo, keys_file, facts_path, stubs, "mermaid", force=True)
    assert proc.returncode == 0, proc.stderr


def test_a_customised_linter_config_is_still_kept(repo, keys_file, facts_path, stubs):
    """The refusal above is scoped to executed assets. A user's own
    .yamllint.yaml is data a linter reads, and keeping it is the point."""
    (repo / ".yamllint.yaml").write_text("# mine, hands off\n")
    proc = generate(repo, keys_file, facts_path, stubs, "yamllint")
    assert proc.returncode == 0, proc.stderr
    assert (repo / ".yamllint.yaml").read_text() == "# mine, hands off\n"
    assert json.loads(facts_path.read_text())["files"]["kept"] == [".yamllint.yaml"]


def test_a_present_but_disabled_hook_is_not_reported_as_coverage(
    repo, keys_file, facts_path, stubs
):
    """`stages: [manual]` keeps a hook off the commit path entirely, so an
    entry can carry the right id and never fire."""
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        stages: [manual]\n"
    )
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr
    kept = json.loads(facts_path.read_text())["hooks"]["left_as_is"]
    line = next(k for k in kept if "gitleaks" in k)
    assert "will NOT run on commit" in line, line
    assert "stages: [manual]" in line


def test_recommend_names_a_present_but_disabled_entry(repo, stubs):
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        stages: [manual]\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "gitleaks" in got["previous"]
    assert "gitleaks" in got["disabled"]


def test_an_ordinary_present_entry_is_not_flagged_as_disabled(repo, stubs):
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["disabled"] == {}


def test_a_bom_survives_the_merge(repo, keys_file, facts_path, stubs):
    """utf-8-sig decodes the mark away, so without recording it the rebuild
    writes plain UTF-8 and a byte this run never inserted quietly disappears --
    which verify_additive cannot see, because it compares decoded lines."""
    body = "repos:\n- repo: local\n  hooks:\n  - id: mine\n"
    (repo / ".pre-commit-config.yaml").write_bytes(b"\xef\xbb\xbf" + body.encode())
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr
    after = (repo / ".pre-commit-config.yaml").read_bytes()
    assert after.startswith(b"\xef\xbb\xbf"), "the BOM was stripped"
    assert b"gitleaks" in after


def test_a_config_without_a_bom_does_not_gain_one(repo, keys_file, facts_path, stubs):
    (repo / ".pre-commit-config.yaml").write_text("repos:\n- repo: local\n  hooks:\n  - id: m\n")
    generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert not (repo / ".pre-commit-config.yaml").read_bytes().startswith(b"\xef\xbb\xbf")


def test_the_vacuous_remedy_names_the_safe_flag(repo, keys_file, facts_path, stubs, tmp_path):
    """The tool's own message is printed verbatim by summary.py, so naming
    --files there told the user to run the command the doc forbids."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = _pre_commit_stub(
        tmp_path, "vac", ["trailing-whitespace......(no files to check) Skipped"]
    )
    got = out_json(
        run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake)
    )
    assert "--files-file" in got["run"]
    assert "with --files." not in got["run"]


# -- guards added after round 10 of the reviewer panel ------------------------


def test_a_foreign_executed_asset_is_caught_before_anything_is_written(
    repo, keys_file, facts_path, stubs
):
    """The config being produced wires `entry: node scripts/lint-mermaid.mjs`,
    so discovering the collision after the write left a live config pointing at
    someone else's program on a run that reported failure."""
    (repo / "scripts").mkdir()
    (repo / "scripts" / "lint-mermaid.mjs").write_text("// not ours\n")
    proc = generate(repo, keys_file, facts_path, stubs, "mermaid")
    assert proc.returncode != 0
    assert "Nothing has been written" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists(), "a half-applied config was left"
    assert not facts_path.exists()


def test_a_corrupt_facts_file_is_caught_before_anything_is_written(
    repo, keys_file, facts_path, stubs
):
    """Reading it only at merge time meant the run died after mutating the repo."""
    facts_path.write_text("{not json")
    proc = generate(repo, keys_file, facts_path, stubs, "hygiene")
    assert proc.returncode != 0
    assert "cannot read facts file" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists(), "the config was written anyway"


def test_always_run_overrides_a_narrow_files_filter(repo, stubs):
    """config.py captures always_run precisely because it decides this, and
    looks_disabled was ignoring it -- so a hook that does run was reported as
    one that does not."""
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        files: '\\.txt$'\n"
        "        always_run: true\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]


def test_a_narrow_files_filter_without_always_run_is_flagged(repo, stubs):
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        files: '\\.txt$'\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "gitleaks" in got["disabled"]


def test_a_failed_status_does_not_become_an_empty_autofix_list(
    repo, keys_file, facts_path, stubs, tmp_path
):
    """Step 5 promises to disclose what the hooks rewrote. A failed check
    returning "nothing dirty" either invents autofixes or drops the disclosure
    entirely, and the user approves believing their tree is cleaner than it is."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = tmp_path / "brokenstatus"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "status" ]; then echo "fatal: index corrupt" >&2; exit 128; fi\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    pc_stub = fake / "pre-commit"
    pc_stub.write_text('#!/bin/sh\nif [ "$1" = "install" ]; then exit 0; fi\nexit 0\n')
    pc_stub.chmod(0o755)
    proc = run(
        "precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake
    )
    assert proc.returncode != 0
    assert "not a clean result" in proc.stderr


def _disabled_for(repo, stubs, hook_body):
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n" + hook_body
    )
    return out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))


def test_a_block_list_stage_restriction_is_seen(repo, stubs):
    """The flow form was caught; the everyday block form was blind, so the user
    was told the secret scanner was already there while it never fires."""
    got = _disabled_for(repo, stubs, "        stages:\n          - manual\n")
    assert "gitleaks" in got["disabled"], got["disabled"]


def test_a_commit_msg_only_hook_is_not_mistaken_for_coverage(repo, stubs):
    """ "commit" is a substring of "commit-msg", so a substring test read a hook
    that never scans content on an ordinary commit as running on every one."""
    got = _disabled_for(repo, stubs, "        stages: [commit-msg]\n")
    assert "gitleaks" in got["disabled"], got["disabled"]


@pytest.mark.parametrize("stages", ["[commit]", "[pre-commit]", "[pre-commit, manual]"])
def test_a_hook_that_does_run_on_commit_is_not_flagged(repo, stubs, stages):
    got = _disabled_for(repo, stubs, f"        stages: {stages}\n")
    assert got["disabled"] == {}, got["disabled"]


def test_renaming_the_local_hook_id_stays_consistent(repo, keys_file, facts_path, stubs):
    """Three separate literals meant a rename left present_keys re-offering
    mermaid forever and verify_written failing every successful write."""
    import precommit

    assert precommit.CATALOG["mermaid"]["local_hook_id"] == "mermaid-lint"
    generate(repo, keys_file, facts_path, stubs, "mermaid")
    got = out_json(run("precommit.py", "--dir", str(repo), "--detect", stubs=stubs))
    assert "mermaid" in got["present"]


@pytest.mark.parametrize(
    "output",
    ["", "No hooks configured for this repository.\n"],
    ids=["empty", "no-result-lines"],
)
def test_output_with_no_hook_result_lines_is_not_a_pass(
    repo, keys_file, facts_path, stubs, tmp_path, output
):
    """A run whose output carries no parseable result line told us nothing.
    Reporting it as a clean pass is the false positive is_vacuous exists for."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = tmp_path / "silent"
    fake.mkdir()
    pc_stub = fake / "pre-commit"
    body = "".join(f'echo "{ln}"\n' for ln in output.splitlines())
    pc_stub.write_text('#!/bin/sh\nif [ "$1" = "install" ]; then exit 0; fi\n' + body + "exit 0\n")
    pc_stub.chmod(0o755)
    got = out_json(
        run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake)
    )
    assert got["run_ok"] is False, "a run that reported nothing is not a pass"


def test_copy_assets_notices_a_file_planted_after_the_pre_check(repo):
    """foreign_assets() gates the write, but the file can appear in the window
    between that check and the copy. copy_assets computes the answer either
    way; discarding it reopened exactly the hole the pre-check closes."""
    import precommit

    (repo / "scripts").mkdir()
    (repo / "scripts" / "lint-mermaid.mjs").write_text("// planted\n")
    wrote, kept, alien = precommit.copy_assets("mermaid", str(repo))
    assert alien == ["scripts/lint-mermaid.mjs"], "copy_assets did not notice the plant"
    assert kept == ["scripts/lint-mermaid.mjs"]
    assert wrote == []
    assert (repo / "scripts" / "lint-mermaid.mjs").read_text() == "// planted\n"


def test_generate_stops_when_the_asset_appears_after_the_pre_check(
    repo, keys_file, facts_path, stubs, monkeypatch
):
    """The branch that acts on copy_assets' answer.

    The real race is between foreign_assets() and the copy, which cannot be hit
    reliably from outside -- so the pre-check is made to report nothing (what it
    would have seen a moment earlier) while the file is already on disk.
    """
    import precommit

    (repo / "scripts").mkdir()
    (repo / "scripts" / "lint-mermaid.mjs").write_text("// planted\n")
    monkeypatch.setattr(precommit, "foreign_assets", lambda keys, directory: [])
    monkeypatch.setattr(precommit, "latest_tag", lambda url: "v1.0.0")
    monkeypatch.setattr(precommit, "npm_latest", lambda pkg: "1.2.3")

    keys = keys_file("mermaid")
    args = precommit.argparse.Namespace(
        dir=str(repo), templates_file=str(keys), facts_out=str(facts_path), force=True
    )
    with pytest.raises(SystemExit) as exc:
        precommit.cmd_generate(args)
    assert exc.value.code != 0
    assert (repo / "scripts" / "lint-mermaid.mjs").read_text() == "// planted\n"


def test_an_accented_autofixed_filename_is_reported_readably(
    repo, keys_file, facts_path, stubs, tmp_path
):
    """dirty_paths covers the whole tree, so an accented pre-existing filename
    the autofixers touch was reported as its escaped form -- unfindable."""
    accented = "caf" + chr(0xE9) + ".md"
    (repo / accented).write_text("# hi\n")
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "add"], check=True)
    generate(repo, keys_file, facts_path, stubs, "hygiene")

    fake = tmp_path / "toucher"
    fake.mkdir()
    pcs = fake / "pre-commit"
    pcs.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "install" ]; then exit 0; fi\n'
        f'echo "fixed" >> "{repo}/{accented}"\n'
        'echo "end-of-file-fixer.....Failed"\n'
        "exit 1\n"
    )
    pcs.chmod(0o755)
    run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake)
    autofixed = json.loads(facts_path.read_text())["verify"]["autofixed"]
    assert accented in autofixed, autofixed


def test_a_failing_install_stops_before_any_json(repo, keys_file, facts_path, stubs, tmp_path):
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = tmp_path / "badinstall"
    fake.mkdir()
    pcs = fake / "pre-commit"
    pcs.write_text('#!/bin/sh\necho "cannot write hook" >&2\nexit 1\n')
    pcs.chmod(0o755)
    proc = run(
        "precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake
    )
    assert proc.returncode != 0
    assert "pre-commit install failed" in proc.stderr
    assert proc.stdout.strip() == "", "no JSON should be emitted on this path"


def test_a_failing_ls_remote_is_reported(repo, keys_file, facts_path, tmp_path):
    fake = tmp_path / "badremote"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "ls-remote" ]; then echo "fatal: unreachable" >&2; exit 128; fi\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    proc = generate(repo, keys_file, facts_path, fake, "hygiene")
    assert proc.returncode != 0
    assert "ls-remote failed" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_a_malformed_managed_entry_does_not_crash_verify(
    repo, keys_file, facts_path, stubs, tmp_path
):
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    facts = json.loads(facts_path.read_text())
    facts["internal"]["managed_files"] = ["just-a-string"]
    facts_path.write_text(json.dumps(facts))
    fake = _pre_commit_stub(tmp_path, "ok13", ["trailing-whitespace......Passed"])
    proc = run(
        "precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake
    )
    assert proc.returncode != 0
    assert "not a JSON object" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_generate_records_which_keys_were_selected(repo, keys_file, facts_path, stubs):
    """The summary marks a declined recommendation from this. Without it every
    recommendation reads as though it was taken."""
    (repo / "doc.md").write_text("# hi\n")
    generate(repo, keys_file, facts_path, stubs, "hygiene", "gitleaks")
    hooks = json.loads(facts_path.read_text())["hooks"]
    assert hooks["selected"] == ["hygiene", "gitleaks"]
    # markdownlint was recommended by the scan and not chosen.
    assert "markdownlint" not in hooks["selected"]


# -- guards added after round 14 of the reviewer panel ------------------------


def test_verify_without_facts_is_refused(repo, keys_file, facts_path, stubs):
    """Without facts there are no scoped_ids, so `unchecked` is always empty and
    a run whose new hooks saw nothing still reports run_ok -- and the whole
    VERIFY section never reaches the summary."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    proc = run("precommit.py", "--dir", str(repo), "--verify", stubs=stubs)
    assert proc.returncode != 0
    assert "--verify needs --facts" in proc.stderr


def test_stages_exclusion_is_not_overridden_by_always_run(repo, stubs):
    """The order of the two checks is deliberate: always_run makes a hook fire
    whatever `files:` says, but it cannot put it back on a stage it was
    excluded from. Swapping the blocks passed every earlier test."""
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        stages: [manual]\n"
        "        always_run: true\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "gitleaks" in got["disabled"], got["disabled"]


def test_a_hostile_rev_cannot_forge_the_relayed_report(repo, keys_file, facts_path, stubs):
    """The rev: scalar comes from the target repo's own config, and Step 3 tells
    the agent to relay this report verbatim -- before summary.py, which cleans,
    ever runs."""
    bidi = chr(0x202E)
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n"
        f"    rev: 'v8.0.0{bidi}forged'\n    hooks:\n      - id: gitleaks\n"
    )
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr
    assert bidi not in proc.stderr, "a text-reordering character reached the relayed report"
    assert bidi not in json.dumps(out_json(proc))


def test_the_verify_scope_is_recorded(repo, keys_file, facts_path, stubs, tmp_path):
    """ "passed" means two very different things depending on what ran."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = _pre_commit_stub(tmp_path, "scoped", ["trailing-whitespace......Passed"])
    got = out_json(
        run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake)
    )
    assert got["scope"] == "all-files"
    assert json.loads(facts_path.read_text())["verify"]["scope"] == "all-files"

    listing = tmp_path / "paths.txt"
    listing.write_text(".pre-commit-config.yaml\n")
    got = out_json(
        run(
            "precommit.py",
            "--dir",
            str(repo),
            "--verify",
            "--facts",
            str(facts_path),
            "--files-file",
            str(listing),
            stubs=fake,
        )
    )
    assert got["scope"] == "these-files"


def test_a_disabled_entry_reaches_the_facts(repo, facts_path, stubs):
    """It was reported live and then forgotten, so the durable summary said
    nothing about a secret scanner configured never to run."""
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        stages: [manual]\n"
    )
    run(
        "precommit.py",
        "--dir",
        str(repo),
        "--recommend",
        "--facts-out",
        str(facts_path),
        stubs=stubs,
    )
    assert json.loads(facts_path.read_text())["hooks"]["disabled"] == ["gitleaks"]


# -- arguments are checked before anything is installed or run ----------------
#
# These run with pre-commit genuinely absent from PATH (only_path=True replaces
# it with the stub directory, which holds git and npm and nothing else). That is
# how the bug showed up: `pre-commit install` ran first, so on a machine without
# pre-commit the run died there and the guards below were never reached at all.
# Every developer here has pre-commit installed; CI does not, which is what
# caught it.


@pytest.mark.parametrize("bad", ["/etc/hosts", "../escape.txt"])
def test_a_files_value_is_refused_before_the_git_hook_is_installed(
    repo, keys_file, facts_path, stubs, bad
):
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert not hook.exists(), "fixture already installed the hook; the test proves nothing"

    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--verify",
        "--facts",
        str(facts_path),
        "--files",
        bad,
        stubs=stubs,
        only_path=True,
    )
    assert proc.returncode != 0
    assert "outside the repository" in proc.stderr
    assert not hook.exists(), "the repository was mutated before the argument was checked"


def test_two_file_sources_are_refused_before_the_git_hook_is_installed(
    repo, keys_file, facts_path, stubs, tmp_path
):
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    listing = tmp_path / "paths.txt"
    listing.write_text("README.md\n")
    hook = repo / ".git" / "hooks" / "pre-commit"

    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--verify",
        "--facts",
        str(facts_path),
        "--files",
        "README.md",
        "--files-file",
        str(listing),
        stubs=stubs,
        only_path=True,
    )
    assert proc.returncode != 0
    assert "not both" in proc.stderr
    assert not hook.exists(), "the repository was mutated before the argument was checked"


# -- round 15 ----------------------------------------------------------------


def test_a_selected_key_missing_from_the_written_file_refuses_after_the_write(
    repo, keys_file, facts_path, stubs, skill_copy
):
    """SKILL.md Step 3 names this as one of two exits that happen AFTER the
    config is written ('a live .pre-commit-config.yaml is now in their tree'),
    and nothing tested it -- deleting the verify_written call failed no test."""
    fragment = skill_copy / "templates" / "gitleaks.yaml"
    fragment.write_bytes(
        fragment.read_bytes().replace(
            b"https://github.com/gitleaks/gitleaks", b"https://example.invalid/other"
        )
    )
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", scripts=skill_copy / "scripts")
    assert proc.returncode != 0
    assert "is not in it" in proc.stderr
    assert proc.stdout.strip() == "", "a post-write failure must not also report success"
    assert (repo / ".pre-commit-config.yaml").exists(), (
        "the premise of this refusal is that the file is already on disk"
    )


def test_a_manual_only_hook_written_at_its_key_s_column_is_still_seen_as_disabled(repo, stubs):
    """End to end for the scanner fix: this indentation style used to report a
    secret scanner confined to `pre-commit run --hook-stage manual` as active
    coverage."""
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: https://github.com/gitleaks/gitleaks\n"
        "    rev: v8.0.0\n"
        "    hooks:\n"
        "      - id: gitleaks\n"
        "        stages:\n"
        "        - manual\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "gitleaks" in got["disabled"], got["disabled"]


def test_the_recommend_payload_carries_every_field_its_type_declares(repo, stubs):
    """RecommendReport was declared and never applied, and had already drifted
    two fields behind the payload while reading as though mypy were watching.

    The expected set is READ FROM the TypedDict rather than written out again --
    a hand-listed tuple is a second copy of the same fact, and it had already
    fallen a field behind (local_repo_sources) by the next round."""
    import shared

    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    for field in shared.RecommendReport.__annotations__:
        assert field in got, field


def test_a_broad_exclude_is_reported_with_its_pattern(repo, keys_file, facts_path, stubs):
    """Both readers of the top-level exclude: now go through one public call."""
    (repo / ".pre-commit-config.yaml").write_text(
        "exclude: '.*'\nrepos:\n"
        "  - repo: https://github.com/psf/black\n    rev: 24.1.0\n"
        "    hooks:\n      - id: black\n"
    )
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr
    assert "pattern: .*" in proc.stderr
    assert "EVERY hook" in proc.stderr


# -- round 16 ----------------------------------------------------------------


def test_an_unrelated_local_hook_is_not_attributed_to_mermaid(repo, stubs):
    """`repo: local` is a bucket anybody's hooks sit in. Matching the URL alone
    reported every disabled local hook as mermaid's -- the mirror of the
    false-coverage bug: instead of calling a dead hook live, it calls somebody
    else's dead hook ours."""
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
        "      - id: my-custom-hook\n        name: my-custom-hook\n"
        "        entry: ./x.sh\n        language: script\n        stages: [manual]\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid" not in got["disabled"], got["disabled"]


def test_a_disabled_mermaid_hook_is_still_attributed_to_mermaid(repo, stubs):
    """The narrowing must not silence the real case."""
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
        "        stages: [manual]\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid" in got["disabled"], got["disabled"]


def test_a_symlinked_markdown_file_is_not_named_as_the_lint_target(repo, stubs, tmp_path):
    """trigger_paths is written to a file and passed as --files-file, so
    `pre-commit run --files` points the autofixing hooks at whatever is named --
    and gitleaks reads and prints it. The mermaid probe already refuses to READ
    through a symlink; naming one as the target was the same threat unguarded."""
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY\n")
    for existing in repo.glob("*.md"):
        existing.unlink()
    # The ONLY markdown in the tree, so the outcome does not depend on walk
    # order: unguarded it is necessarily the chosen trigger.
    (repo / "notes.md").symlink_to(secret)

    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "notes.md" not in got["detected_paths"], got["detected_paths"]
    assert "markdownlint" not in [r["name"] for r in got["recommended"]], got["recommended"]


def test_a_repo_cloned_off_this_disk_is_disclosed(repo, stubs):
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: file:///tmp/hooks\n    rev: v1\n    hooks:\n      - id: a\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--detect", stubs=stubs))
    assert got["local_repo_sources"] == ["file:///tmp/hooks"]
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["local_repo_sources"] == ["file:///tmp/hooks"]


# -- round 17 ----------------------------------------------------------------


def test_verify_written_checks_hook_ids_not_just_the_repo_url():
    """The commonest merge inserts missing hook IDS into a repo entry whose URL
    the file already carries -- hygiene has seven ids under one URL. Checking
    the URL alone passed trivially in exactly that case, so a splice that
    dropped the new hook block still reported success, while SKILL.md Step 3
    promises the merged file is re-scanned and every entry confirmed present."""
    import config as C
    import precommit as P

    after = C.scan(
        "repos:\n  - repo: https://github.com/pre-commit/pre-commit-hooks\n"
        "    rev: v6.0.0\n    hooks:\n      - id: trailing-whitespace\n"
    )
    # The URL is present, so the old check passed. One declared id is not.
    with pytest.raises(SystemExit) as exit_info:
        P.verify_written(
            "cfg", ["hygiene"], after, {"hygiene": {"trailing-whitespace", "end-of-file-fixer"}}
        )
    assert exit_info.value.code != 0

    # And it does not cry wolf when everything declared really is there.
    P.verify_written("cfg", ["hygiene"], after, {"hygiene": {"trailing-whitespace"}})


@pytest.mark.parametrize(
    ("reason", "setup"),
    [
        ("unknown-key", "unknown"),
        ("dirty", "dirty"),
        ("config-refused", "refused"),
    ],
)
def test_a_classified_exit_says_why_in_json(repo, keys_file, facts_path, stubs, reason, setup):
    """gitwork's non-1 exits always carry a machine-checkable object; this
    file's 3/4/5 handed the caller an English sentence and nothing else -- and
    EXIT_DIRTY covers two different causes that could only be told apart by
    reading the prose."""
    if setup == "unknown":
        args = ["--templates-file", str(keys_file("nosuchkey"))]
    elif setup == "dirty":
        (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
        subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
        subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "base"], check=True)
        (repo / ".pre-commit-config.yaml").write_text("repos: []\n# my edit\n")
        args = ["--templates-file", str(keys_file("gitleaks"))]
    else:
        (repo / ".pre-commit-config.yaml").write_text("repos:\n  - repo: local\n    hooks: *x\n")
        args = ["--templates-file", str(keys_file("gitleaks"))]

    proc = run(
        "precommit.py", "--dir", str(repo), *args, "--facts-out", str(facts_path), stubs=stubs
    )
    assert proc.returncode != 0
    got = out_json(proc)
    assert got["ok"] is False
    assert got["reason"] == reason, got
    assert got["exit"] == proc.returncode


def test_hook_output_is_neutralised_before_the_agent_reads_it(
    repo, keys_file, facts_path, stubs, tmp_path
):
    """The hooks echo the paths they check, gitleaks prints match context, and a
    `repo: local` block supplies its own hook name -- so this is a channel from
    the repository straight to the agent, and it was the one that skipped
    clean()."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    bidi = chr(0x202E)
    fake = _pre_commit_stub(tmp_path, "hostile", [f"trailing-whitespace{bidi}......Passed"])
    proc = run(
        "precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake
    )
    assert bidi not in proc.stderr, "a text-reordering character reached the agent"
    assert "WARNING" in proc.stderr


# -- round 17 remainder ------------------------------------------------------


def test_a_hook_level_exclude_is_reported_as_disabled(repo, stubs):
    """Every sibling gating key had a regression test; hook-level `exclude:`
    did not, so deleting its branch failed nothing."""
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        exclude: '.*'\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "gitleaks" in got["disabled"], got["disabled"]
    assert "exclude" in got["disabled"]["gitleaks"][0]


def test_verify_written_covers_the_local_branch_too():
    """verify_written picks its haystack two ways: hook ids under a repo URL,
    or the `repo: local` bucket. The rev_repo side had a test; the local side
    (mermaid, the only entry with no rev_repo) had none."""
    import config as C
    import precommit as P

    after = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: something-else\n"
        "        name: x\n        entry: ./x.sh\n        language: script\n"
    )
    with pytest.raises(SystemExit) as exit_info:
        P.verify_written("cfg", ["mermaid"], after, {"mermaid": {"mermaid-lint"}})
    assert exit_info.value.code != 0

    after_ok = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: mermaid-lint\n"
        "        name: x\n        entry: ./x.sh\n        language: node\n"
    )
    P.verify_written("cfg", ["mermaid"], after_ok, {"mermaid": {"mermaid-lint"}})


def test_the_scan_stops_at_the_depth_bound(repo, stubs):
    """The walk is depth-bounded so a monorepo hangs rather than answers.
    Nothing tested the bound, so widening or losing it was invisible."""
    for existing in repo.glob("*.md"):
        existing.unlink()
    deep = repo / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "buried.md").write_text("# buried\n")

    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    joined = " ".join(got["detected"]) + " ".join(got["detected_paths"])
    assert "buried.md" not in joined, "the depth bound did not hold"
    assert "markdownlint" not in [r["name"] for r in got["recommended"]]

    # And the bound is a bound, not a blanket refusal: a shallow one is seen.
    (repo / "shallow.md").write_text("# shallow\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "shallow.md" in " ".join(got["detected_paths"])


def test_both_top_matter_lines_are_inserted_in_order(repo, keys_file, facts_path, stubs):
    """minimum_pre_commit_version and exclude land at the same line, so
    merge_same_position folds them into one block -- in list order. Several
    tests drove the fold incidentally; none asserted what came out."""
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode == 0, proc.stderr
    text = (repo / ".pre-commit-config.yaml").read_text()
    assert "minimum_pre_commit_version" in text
    assert "exclude:" in text
    assert text.index("minimum_pre_commit_version") < text.index("exclude:"), text


def test_missing_npm_is_reported_cleanly(repo, keys_file, facts_path, stubs, tmp_path):
    """Split from a parametrized test that branched on its own parameter and ran
    a different arrange/act per branch -- two scenarios sharing one docstring,
    so a failure trace could not say which was being exercised."""
    empty = tmp_path / "no-npm"
    empty.mkdir()
    (empty / "git").symlink_to(stubs / "git")  # git stays reachable; npm does not
    # only_path REPLACES PATH: prepending would leave the real npm findable
    # behind the stub, and the test would prove nothing.
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--templates-file",
        str(keys_file("mermaid")),
        "--facts-out",
        str(facts_path),
        stubs=empty,
        only_path=True,
    )
    assert proc.returncode != 0
    assert "npm not found" in proc.stderr


def test_missing_pre_commit_is_reported_cleanly(repo, keys_file, facts_path, stubs):
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--verify",
        "--facts",
        str(facts_path),
        stubs=stubs,
        only_path=True,
    )
    assert proc.returncode != 0
    assert "pre-commit not found on PATH" in proc.stderr


# -- the failure paths ---------------------------------------------------------


def test_a_directory_that_does_not_exist_is_named(tmp_path, facts_path, keys_file, stubs):
    proc = run("precommit.py", "--dir", str(tmp_path / "nope"), "--detect", stubs=stubs)
    assert proc.returncode != 0
    assert "directory not found" in proc.stderr


def test_an_empty_templates_file_is_refused(repo, facts_path, tmp_path, stubs):
    """An empty selection is not "generate nothing" -- it is a caller that lost
    its list between steps."""
    empty = tmp_path / "keys.txt"
    empty.write_text("\n\n")
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--templates-file",
        str(empty),
        "--facts-out",
        str(facts_path),
        stubs=stubs,
    )
    assert proc.returncode != 0
    assert "no catalog keys" in proc.stderr


def test_an_empty_files_file_is_refused(repo, keys_file, facts_path, stubs, tmp_path):
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    empty = tmp_path / "paths.txt"
    empty.write_text("\n")
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--verify",
        "--facts",
        str(facts_path),
        "--files-file",
        str(empty),
        stubs=stubs,
    )
    assert proc.returncode != 0
    assert "no paths in" in proc.stderr


def test_a_config_that_is_not_utf8_is_named_rather_than_mangled(repo, stubs):
    """errors="replace" here would hand the scanner text the file does not
    contain, and the additive writer would then "preserve" bytes that were
    never there."""
    (repo / ".pre-commit-config.yaml").write_bytes(b"repos: []\n\xff\xfe\n")
    proc = run("precommit.py", "--dir", str(repo), "--detect", stubs=stubs)
    assert proc.returncode != 0
    assert "not valid UTF-8" in proc.stderr


def test_hook_output_longer_than_the_cap_is_truncated(repo, keys_file, facts_path, stubs, tmp_path):
    """A hook can print a whole file. The agent has to read this to judge the
    run, so it is bounded the way git's stderr is."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    fake = _pre_commit_stub(tmp_path, "chatty", ["x" * 40000, "trailing-whitespace......Passed"])
    proc = run(
        "precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake
    )
    assert "(truncated" in proc.stderr
    assert len(proc.stderr) < 40000


class TestAMalformedCatalogFragmentStopsTheRun:
    """Our OWN templates, checked on every run. A fragment that stops scanning
    is a packaging or edit error, and it must not become a merge."""

    def test_an_unfilled_placeholder_is_caught(
        self, repo, keys_file, facts_path, stubs, skill_copy
    ):
        fragment = skill_copy / "templates" / "gitleaks.yaml"
        # __NPM__, not __REV__: gitleaks HAS a rev_repo, so __REV__ is
        # substituted and never reaches the check. In a comment, so the failure
        # is the placeholder and not the YAML.
        fragment.write_text(fragment.read_text() + "# left over: __NPM__\n")
        proc = generate(
            repo, keys_file, facts_path, stubs, "gitleaks", scripts=skill_copy / "scripts"
        )
        assert proc.returncode != 0
        assert "unfilled placeholder" in proc.stderr

    def test_a_fragment_that_does_not_scan_is_caught(
        self, repo, keys_file, facts_path, stubs, skill_copy
    ):
        fragment = skill_copy / "templates" / "gitleaks.yaml"
        fragment.write_text("- repo: https://github.com/gitleaks/gitleaks\n  hooks: *alias\n")
        proc = generate(
            repo, keys_file, facts_path, stubs, "gitleaks", scripts=skill_copy / "scripts"
        )
        assert proc.returncode != 0
        assert "malformed" in proc.stderr

    def test_a_fragment_declaring_two_entries_is_caught(
        self, repo, keys_file, facts_path, stubs, skill_copy
    ):
        fragment = skill_copy / "templates" / "gitleaks.yaml"
        fragment.write_text(
            fragment.read_text() + "- repo: https://github.com/psf/black\n  rev: v1\n"
            "  hooks:\n    - id: black\n"
        )
        proc = generate(
            repo, keys_file, facts_path, stubs, "gitleaks", scripts=skill_copy / "scripts"
        )
        assert proc.returncode != 0
        assert "exactly one repo entry" in proc.stderr


# -- decided by the script, not by the agent -----------------------------------


def test_the_mermaid_prerequisite_is_reported_rather_than_probed(repo, stubs):
    """SKILL.md used to hand the agent `command -v npm && command -v node` and
    ask it to interpret the output. shutil.which answers that exactly."""
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["prerequisites"]["mermaid"] in ("binaries present",) or got["prerequisites"][
        "mermaid"
    ].startswith("missing: ")


def test_the_prerequisite_sentinel_is_the_string_SKILL_md_branches_on(repo, stubs):
    """The value is a contract with the procedure, not a label.

    SKILL.md tests it literally, and a reworded one fails in the direction that
    looks like nothing: every healthy run takes the missing-prerequisite branch
    and warns that picking mermaid aborts the whole write, with nothing wrong.
    Renaming it here has to be red until the procedure is renamed with it.
    """
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    value = got["prerequisites"]["mermaid"]
    assert value == "binaries present"
    assert f"`{value}`" in (SKILL / "SKILL.md").read_text(encoding="utf-8")


def test_a_missing_prerequisite_is_named(repo, stubs, tmp_path):
    empty = tmp_path / "no-node"
    empty.mkdir()
    (empty / "git").symlink_to(stubs / "git")
    got = out_json(
        run("precommit.py", "--dir", str(repo), "--recommend", stubs=empty, only_path=True)
    )
    assert got["prerequisites"]["mermaid"] == "missing: node, npm"


def test_autofixed_is_split_by_whose_file_it_is(repo, keys_file, facts_path, stubs, tmp_path):
    """Two halves, opposite sentences: this run's own files DO get committed
    (--verify re-hashes them so an autofixed one still passes the commit gate),
    the rest are the user's. The agent used to do this set arithmetic itself."""
    generate(repo, keys_file, facts_path, stubs, "hygiene")
    ours = json.loads(facts_path.read_text())["internal"]["managed_files"][0]["path"]
    # Committed first: `autofixed` is the DELTA in dirty paths across the run,
    # so a file that was already dirty going in can never appear in it. The
    # scenario this splits is the update-an-existing-config one.
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "base"], check=True)
    theirs = repo / "theirs.md"
    theirs.write_text("# theirs\n")
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "commit", "-qm", "theirs"], check=True)

    fake = tmp_path / "toucher"
    fake.mkdir()
    pcs = fake / "pre-commit"
    pcs.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "install" ]; then exit 0; fi\n'
        f'echo "fixed" >> "{repo}/{ours}"\n'
        f'echo "fixed" >> "{theirs}"\n'
        'echo "end-of-file-fixer.....Failed"\n'
        "exit 1\n"
    )
    pcs.chmod(0o755)
    got = out_json(
        run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake)
    )
    assert ours in got["autofixed_ours"], got
    assert "theirs.md" in got["autofixed_elsewhere"], got
    assert ours not in got["autofixed_elsewhere"]


def test_the_rerun_list_is_worked_out_for_the_agent(repo, keys_file, facts_path, stubs, tmp_path):
    """The vacuous recovery path used to tell the agent to union files.written
    with scan.detected_paths by hand -- on the one path that is already running
    because verification went wrong once."""
    (repo / "notes.md").write_text("# notes\n")
    run(
        "precommit.py",
        "--dir",
        str(repo),
        "--recommend",
        "--facts-out",
        str(facts_path),
        stubs=stubs,
    )
    generate(repo, keys_file, facts_path, stubs, "hygiene", force=True)
    # A stub pre-commit, like every other verify test here: the real binary is
    # on a developer's PATH and not on a CI runner's, and this test is about the
    # list the tool computes, not about running hooks.
    fake = _pre_commit_stub(tmp_path, "rerun", ["trailing-whitespace......Passed"])
    got = out_json(
        run("precommit.py", "--dir", str(repo), "--verify", "--facts", str(facts_path), stubs=fake)
    )
    facts = json.loads(facts_path.read_text())
    expected = sorted({*facts["files"]["written"], *facts["scan"]["detected_paths"]})
    assert got["rerun_files"] == expected
    # Both halves really are in it: what this run wrote, and the file that
    # caused a hook to be recommended -- which is the whole point, since the
    # trigger file is what exercises the hook.
    assert set(facts["files"]["written"]) <= set(got["rerun_files"])
    assert set(facts["scan"]["detected_paths"]) <= set(got["rerun_files"])
    assert facts["scan"]["detected_paths"], "no trigger detected; the test proves half of itself"
