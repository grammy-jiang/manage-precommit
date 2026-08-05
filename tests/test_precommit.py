"""The engine, driven as a subprocess the way SKILL.md drives it."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess

import pytest

from conftest import NPM_VERSION, REAL_GIT, out_json, run, stub_calls


def generate(repo, keys_file, facts_path, stubs, *names, force=False):
    args = ["--dir", str(repo), "--templates-file", str(keys_file(*names))]
    if force:
        args.append("--force")
    args += ["--facts-out", str(facts_path)]
    return run("precommit.py", *args, stubs=stubs)


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
    assert "npm view @mermaid-js/mermaid-cli version" in calls
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


def test_a_forged_character_in_the_merged_config_stops_the_write(
    repo, keys_file, facts_path, stubs
):
    """The one guard between a hostile existing config -- carried through
    byte-for-byte by design -- and a human approving a diff that does not say
    what the bytes say."""
    (repo / ".pre-commit-config.yaml").write_text(
        f"# note{BIDI} reversed\nrepos:\n- repo: local\n  hooks:\n  - id: x\n"
    )
    before = (repo / ".pre-commit-config.yaml").read_bytes()
    proc = generate(repo, keys_file, facts_path, stubs, "gitleaks", force=True)
    assert proc.returncode != 0
    assert "text-reordering" in proc.stderr
    assert (repo / ".pre-commit-config.yaml").read_bytes() == before, "the file was rewritten"


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
