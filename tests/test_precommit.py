"""The engine, driven as a subprocess the way SKILL.md drives it."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys

import pytest

from conftest import NPM_VERSION, REAL_GIT, SKILL, out_json, run, stub_calls

sys.path.insert(0, str(SKILL / "scripts"))


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
    assert by_name["mermaid-parse"] == "docs/arch.md"
    assert "mermaid" not in by_name, "the renderer is asked for by name, never recommended"
    assert "gitleaks" in by_name
    assert got["config"] == "none"


def test_recommend_skips_mermaid_without_a_fence(repo, stubs):
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert not {"mermaid", "mermaid-parse"} & {r["name"] for r in got["recommended"]}


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
    assert proc.returncode == 6
    assert "no version tags" in proc.stderr
    got = out_json(proc)
    assert got["reason"] == "version_pin_failed"
    assert got["cause"] == "no-version-tags"
    assert got["source"] == "git"
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
    assert keys == {"hygiene", "yamllint", "markdownlint", "mermaid-parse", "mermaid", "gitleaks"}


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
    assert not {"mermaid", "mermaid-parse"} & {r["name"] for r in got["recommended"]}


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
    assert proc.returncode == 6
    assert "unexpected version" in proc.stderr
    assert out_json(proc)["cause"] == "invalid-version"
    assert not (repo / ".pre-commit-config.yaml").exists()


# npm's `code` line, verbatim from the failures each one names. `npm error` is
# npm 9 and later; `npm ERR!` is npm 8 and earlier, and both are still in the
# wild, so the parser is asked about both here rather than about whichever one
# happens to be installed.
NPM_FAILURES = [
    pytest.param(
        "npm error code ENOENT\nnpm error syscall mkdir\nnpm error path /root/.npm\n",
        "filesystem",
        "/root/.npm",
        id="unwritable cache (the report this exit exists for)",
    ),
    pytest.param("npm error code E404\n", "not-found", "", id="no such package"),
    pytest.param("npm ERR! code E401\n", "auth", "", id="registry wants credentials"),
    # Not auth: npm labels any HTTP failure E<status> and special-cases only
    # 401, so a 403 is as likely a company registry blocking a package by
    # policy for an account that authenticated perfectly well.
    pytest.param("npm error code E403\n", "forbidden", "", id="refused, not unauthenticated"),
    pytest.param("npm error code ENOTFOUND\n", "network", "", id="dns"),
    # getaddrinfo's family, matched as a family: EAI_AGAIN was in the set and
    # EAI_FAIL was not, which is the difference between a rule and a memory.
    pytest.param("npm error code EAI_AGAIN\n", "network", "", id="resolver: try again"),
    pytest.param("npm error code EAI_FAIL\n", "network", "", id="resolver: gave up"),
    pytest.param("npm ERR! code EAI_NONAME\n", "network", "", id="resolver: no such name"),
    pytest.param("npm error code EAI_NODATA\n", "network", "", id="resolver: no address"),
    # The same prefix, and not the same meaning: bad arguments, no memory and a
    # full buffer are not reachability, and "retry the network" is the wrong
    # sentence for all three. A shared prefix is not a shared cause.
    pytest.param("npm error code EAI_BADFLAGS\n", "unknown", "", id="resolver: bad arguments"),
    pytest.param("npm error code EAI_MEMORY\n", "unknown", "", id="resolver: out of memory"),
    pytest.param("npm error code EAI_OVERFLOW\n", "unknown", "", id="resolver: buffer too small"),
    pytest.param("npm error code EAUTHIP\n", "auth", "", id="the registry refused this IP"),
    pytest.param("npm error code ESOCKETTIMEDOUT\n", "timeout", "", id="socket timed out"),
    pytest.param("npm ERR! code ECONNREFUSED\n", "network", "", id="connection refused"),
    # The kernel's answer when there is no route at all, which is what a laptop
    # off the VPN reports rather than any of the friendlier ones above.
    pytest.param("npm error code ENETUNREACH\n", "network", "", id="no route to the network"),
    pytest.param("npm error code EHOSTUNREACH\n", "network", "", id="no route to the host"),
    pytest.param("npm ERR! code EPIPE\n", "network", "", id="connection went away mid-write"),
    # npm gives up on the socket itself and exits normally, so this never
    # reaches the TimeoutExpired handler. Reported as `network` it left the
    # `timeout` bucket with almost nothing that could land in it.
    pytest.param("npm error code ETIMEDOUT\n", "timeout", "", id="npm gave up on the socket"),
    pytest.param("npm ERR! code ERR_SOCKET_TIMEOUT\n", "timeout", "", id="socket timeout"),
    # @npmcli/agent's own four, for connect, idle, response and transfer. Each
    # exits normally, so none of them reaches the TimeoutExpired handler either.
    pytest.param("npm error code ECONNECTIONTIMEOUT\n", "timeout", "", id="agent: connect"),
    pytest.param("npm error code EIDLETIMEOUT\n", "timeout", "", id="agent: idle"),
    pytest.param("npm error code ERESPONSETIMEOUT\n", "timeout", "", id="agent: response"),
    pytest.param("npm error code ETRANSFERTIMEOUT\n", "timeout", "", id="agent: transfer"),
    pytest.param(
        "npm error code ENOSPC\nnpm error path /tmp/x/npm-cache\n",
        "filesystem",
        "/tmp/x/npm-cache",
        id="the scratch cache filled the disk",
    ),
    # The disk has room and the user does not: a quota is the same advice as a
    # full filesystem and arrives under a different name.
    pytest.param(
        "npm error code EDQUOT\nnpm error path /tmp/x\n",
        "filesystem",
        "/tmp/x",
        id="the quota ran out",
    ),
    pytest.param("npm error code EIO\n", "filesystem", "", id="the device errored"),
    pytest.param("npm ERR! code ENAMETOOLONG\n", "filesystem", "", id="path too long"),
    pytest.param(
        "npm error code EACCES\nnpm error path /opt/x\n", "filesystem", "/opt/x", id="perm"
    ),
    pytest.param("npm error code EWEIRDNESS\n", "unknown", "", id="a code with no bucket"),
    pytest.param("something went wrong\n", "unknown", "", id="npm said nothing machine-readable"),
    # SKILL.md promises that `network` covers TLS, and openssl's verify codes
    # are far too many to enumerate -- so the family is matched as a family, and
    # these are the ones a corporate middlebox actually produces.
    pytest.param("npm error code CERT_HAS_EXPIRED\n", "network", "", id="expired certificate"),
    pytest.param(
        "npm error code UNABLE_TO_GET_ISSUER_CERT_LOCALLY\n",
        "network",
        "",
        id="an intercepting proxy's root is not trusted",
    ),
    pytest.param(
        "npm error code ERR_TLS_CERT_ALTNAME_INVALID\n", "network", "", id="wrong hostname"
    ),
    pytest.param(
        "npm error code SELF_SIGNED_CERT_IN_CHAIN\n", "network", "", id="self-signed chain"
    ),
    pytest.param(
        "npm error code ERR_SSL_WRONG_VERSION_NUMBER\n", "network", "", id="not actually TLS"
    ),
    # The three that carry no CERT/TLS/SSL token at all, which is why matching
    # on those words was the wrong shape: the first of these is what an
    # intercepting proxy usually produces, and it had been classified correctly
    # until a "family, not a list" rewrite dropped it into `unknown`.
    pytest.param(
        "npm error code UNABLE_TO_VERIFY_LEAF_SIGNATURE\n",
        "network",
        "",
        id="the proxy's chain cannot be verified",
    ),
    pytest.param("npm error code CRL_HAS_EXPIRED\n", "network", "", id="stale revocation list"),
    pytest.param(
        "npm error code SUBJECT_ISSUER_MISMATCH\n", "network", "", id="chain does not join up"
    ),
]


@pytest.mark.parametrize("stderr,cause,path", NPM_FAILURES)
def test_a_failed_pin_names_its_cause(
    repo, keys_file, facts_path, tmp_path, stubs, stderr, cause, path
):
    """Five different remedies, and the agent should not be guessing which.

    An unwritable cache is the user's to fix, a 404 is a bug in this catalog, a
    dropped connection is worth retrying -- and npm says which in a `code` line
    that means the same thing in every locale, unlike the sentence beside it.
    `unknown` is a bucket rather than a default, because the failure that
    matches no rule is exactly the one that must not disappear.
    """
    quoted = stderr.replace("\n", "\\n")
    fake = _fake_bin(tmp_path, "npmcoded", f'#!/bin/sh\nprintf "{quoted}" >&2\nexit 1\n')
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["reason"] == "version_pin_failed"
    assert got["source"] == "npm"
    assert got["target"] == "@mermaid-js/mermaid-cli"
    assert got["cause"] == cause
    assert got["npm_path"] == path
    # Whatever the classification, npm's own words survive it.
    assert got["detail"], "the raw complaint must not be swallowed by the label"
    assert not (repo / ".pre-commit-config.yaml").exists()


# Written out here rather than read from the source, deliberately: a test that
# imports the same set it is checking proves only that a set equals itself. This
# is the list from node's own documentation of what TLS verification can fail
# with, and it is the outside opinion the classifier is measured against.
TLS_CODES_FROM_NODE_DOCS = [
    "UNABLE_TO_GET_ISSUER_CERT",
    "UNABLE_TO_GET_CRL",
    "UNABLE_TO_DECRYPT_CERT_SIGNATURE",
    "UNABLE_TO_DECRYPT_CRL_SIGNATURE",
    "UNABLE_TO_DECODE_ISSUER_PUBLIC_KEY",
    "CERT_SIGNATURE_FAILURE",
    "CRL_SIGNATURE_FAILURE",
    "CERT_NOT_YET_VALID",
    "CERT_HAS_EXPIRED",
    "CRL_NOT_YET_VALID",
    "CRL_HAS_EXPIRED",
    "ERROR_IN_CERT_NOT_BEFORE_FIELD",
    "ERROR_IN_CERT_NOT_AFTER_FIELD",
    "ERROR_IN_CRL_LAST_UPDATE_FIELD",
    "ERROR_IN_CRL_NEXT_UPDATE_FIELD",
    "DEPTH_ZERO_SELF_SIGNED_CERT",
    "SELF_SIGNED_CERT_IN_CHAIN",
    "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
    "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
    "CERT_CHAIN_TOO_LONG",
    "CERT_REVOKED",
    "INVALID_CA",
    "PATH_LENGTH_EXCEEDED",
    "INVALID_PURPOSE",
    "CERT_UNTRUSTED",
    "CERT_REJECTED",
    "HOSTNAME_MISMATCH",
]


def test_the_whole_tls_verify_family_is_reachability_advice(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """SKILL.md promises `network` for TLS, and half these codes are not named
    for it.

    The previous attempt matched any code containing CERT, TLS or SSL, which
    reads as thorough and silently loses UNABLE_TO_VERIFY_LEAF_SIGNATURE,
    CRL_HAS_EXPIRED and SUBJECT_ISSUER_MISMATCH -- the first being what a
    corporate middlebox actually reports. One case per code, so a rewrite of the
    classifier has to keep the whole family and not the recognisable half.
    """
    fake = _fake_bin(
        tmp_path,
        "npmtls",
        '#!/bin/sh\nprintf "npm error code %s\\n" "$(cat "$MP_TLS_CODE")" >&2\nexit 1\n',
    )
    (fake / "git").symlink_to(stubs / "git")
    code_file = tmp_path / "tls-code.txt"
    monkeypatch.setenv("MP_TLS_CODE", str(code_file))
    unclassified = []
    for code in TLS_CODES_FROM_NODE_DOCS:
        code_file.write_text(code)
        proc = generate(repo, keys_file, facts_path, fake, "mermaid")
        assert proc.returncode == 6, proc.stderr
        if out_json(proc)["cause"] != "network":
            unclassified.append(code)
    assert not unclassified, f"TLS codes that got the unclassified answer: {unclassified}"


def test_a_coloured_npm_is_still_classified(repo, keys_file, facts_path, tmp_path, stubs):
    """`color=always` in a user .npmrc is configuration this skill honours.

    npm then writes escapes into a pipe, between `npm` and `error` -- straight
    through the middle of the line the cause is read from. Every classified
    failure would arrive `unknown`, which is the answer that fits none of
    SKILL.md's advice, on a machine whose only fault is a colour preference.

    The stub also proves `--no-color` was passed, since prevention and the
    strip are meant to be belt and braces rather than one dressed as two.
    """
    fake = _fake_bin(
        tmp_path,
        "npmcolour",
        "#!/bin/sh\n"
        'case " $* " in *" --no-color "*) ;; *) echo "no --no-color" >&2; exit 7;; esac\n'
        'printf "npm \\033[31merror\\033[0m code E403\\n" >&2\n'
        'printf "npm \\033[31merror\\033[0m 403 Forbidden\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    assert out_json(proc)["cause"] == "forbidden"
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_a_scoped_registry_is_the_one_reported(repo, keys_file, facts_path, tmp_path, stubs):
    """Every npm package this catalog pins is scoped, so this is the usual case.

    `@scope:registry` routes a scoped package on its own while `registry` still
    reads as npmjs -- so asking only the default reports npmjs confidently, and
    SKILL.md then blames this catalog for a package the company mirror simply
    does not carry. Which is precisely the misdiagnosis the `registry` field was
    added to prevent.
    """
    fake = _fake_bin(
        tmp_path,
        "npmscoped",
        "#!/bin/sh\n"
        'if [ "$1" = "config" ]; then\n'
        '  case "$3" in\n'
        '    @*:registry) echo "https://npm.corp.invalid/" ;;\n'
        '    *) echo "https://registry.npmjs.org/" ;;\n'
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf "npm error code E404\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["cause"] == "not-found"
    assert got["registry"] == "https://npm.corp.invalid/"


@pytest.mark.parametrize(
    "answered,public",
    [
        pytest.param("https://registry.npmjs.org/", True, id="npmjs, as npm writes it"),
        pytest.param("https://registry.npmjs.org", True, id="npmjs, no trailing slash"),
        pytest.param("HTTPS://Registry.NPMJS.org/", True, id="npmjs, shouted"),
        pytest.param("https://npm.corp.invalid/", False, id="a company mirror"),
        pytest.param("https://registry.npmjs.org.evil.invalid/", False, id="a lookalike host"),
        # The name alone is not the endpoint: a port means something local is
        # answering for it, and this field exists to tell "npmjs said no" apart
        # from "your mirror said no".
        pytest.param("https://registry.npmjs.org:4873/", False, id="a proxy on npmjs's name"),
        pytest.param("https://registry.npmjs.org:443/", True, id="the default port, spelled out"),
        # Not over plain http: nothing authenticates the far end, so a proxy
        # can answer for that name -- including with a 404 -- and this field
        # exists to tell "npmjs said no" from "something else said no".
        pytest.param("http://registry.npmjs.org/", False, id="npmjs over plain http"),
        pytest.param("ftp://registry.npmjs.org/", False, id="not a scheme npm speaks"),
        pytest.param("https://registry.npmjs.org:nope/", False, id="a port that is not a number"),
        # The DNS root label. Same host, npm keeps the spelling, and the
        # certificate is accepted for it -- so refusing it told people an
        # npmjs 404 probably came from their own mirror.
        pytest.param("https://registry.npmjs.org./", True, id="written with the DNS root label"),
        pytest.param("https://registry.npmjs.org../", False, id="two dots is not a hostname"),
        pytest.param(
            "https://registry.npmjs.org.evil.invalid./",
            False,
            id="a lookalike keeps failing on the name",
        ),
        # Spellings of the root that npm preserves and node resolves before
        # asking. Normalised rather than matched one at a time -- this was the
        # seventh spelling of the same endpoint to come through review.
        pytest.param("https://registry.npmjs.org/./", True, id="a dot segment"),
        pytest.param("https://registry.npmjs.org/%2e/", True, id="an encoded dot segment"),
        pytest.param("https://registry.npmjs.org/a/../", True, id="up out of a segment"),
        pytest.param(
            "https://registry.npmjs.org/custom/./", False, id="a real path with a dot segment"
        ),
        # npm keeps the base path and appends the package to it, so a path is a
        # different endpoint wearing the same name, exactly as a port is.
        pytest.param("https://registry.npmjs.org/custom/", False, id="a base path on npmjs's name"),
        pytest.param("https://registry.npmjs.org/", True, id="the root, which is the real one"),
        # npm appends the package to the configured string whole, so a query or
        # a fragment leaves the request inside it rather than at the root.
        pytest.param("https://registry.npmjs.org/?mirror=corp", False, id="a query string"),
        pytest.param("https://registry.npmjs.org/#frag", False, id="a fragment"),
        # Credentials change who is asking, not who answers.
        pytest.param("https://token@registry.npmjs.org/", True, id="npmjs with a token"),
    ],
)
def test_whether_the_registry_was_npms_own_is_decided_here(
    repo, keys_file, facts_path, tmp_path, stubs, answered, public
):
    """One registry, several spellings, and the agent must not be comparing them.

    `npm config get registry` returns whatever the user wrote, so npmjs itself
    arrives with or without a trailing slash -- and a string test in SKILL.md
    then reads it as a company mirror and tells someone that a bug in this
    catalog is theirs to go and fix. The lookalike host is here because a
    substring test would call it npmjs, which is the other way to get it wrong.
    """
    fake = _fake_bin(
        tmp_path,
        "npmspelling",
        "#!/bin/sh\n"
        'if [ "$1" = "config" ]; then\n'
        '  case "$3" in\n'
        "    @*:registry) echo undefined ;;\n"
        # Quoted, because `--json` is passed and this is what npm answers with.
        # Passed as an ARGUMENT, not as printf's format string: a `%2e` in
        # the URL is otherwise read as a conversion spec and the stub
        # answers something this test never wrote.
        f"    *) printf '\"%s\"\\n' '{answered}' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf "npm error code E404\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    assert out_json(proc)["registry_is_public"] is public


def test_a_token_in_the_registry_url_is_not_relayed(repo, keys_file, facts_path, tmp_path, stubs):
    """npm returns the registry exactly as configured, credentials and all.

    SKILL.md hands `registry` to the agent, so an unredacted one puts a token
    into the model's context and into whatever log the session writes -- and
    `clean()` only removes control characters. The classification still runs on
    the whole URL, so allowing credentials there (they change who is asking, not
    who answers) does not have to mean printing them.

    npm's own error text is redacted too: it quotes the URL it requested.
    """
    fake = _fake_bin(
        tmp_path,
        "npmtoken",
        "#!/bin/sh\n"
        'if [ "$1" = "config" ]; then\n'
        '  case "$3" in\n'
        "    @*:registry) echo undefined ;;\n"
        "    *) printf '\"https://s3cr3t-token@registry.npmjs.org/\"\\n' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf "npm error code E404\\n" >&2\n'
        'printf "npm error 404 GET https://s3cr3t-token@registry.npmjs.org/x\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert "s3cr3t-token" not in proc.stdout
    assert "s3cr3t-token" not in proc.stderr
    assert got["registry"] == "https://***@registry.npmjs.org/"
    assert "?" not in got["registry"]
    assert "s3cr3t-token" not in got["detail"]
    # And redacting the copy that leaves did not blind the copy that decides.
    assert got["registry_is_public"] is True


def test_a_path_credential_is_gone_from_npms_own_words_too(
    repo, keys_file, facts_path, tmp_path, stubs
):
    """The same secret appears twice, and fixing the field fixed one of them.

    npm quotes the URL it requested, so a registry authenticating by path puts
    its key in front of the package name in the failure text -- which reaches
    the agent as `detail` and as the printed sentence.
    """
    fake = _fake_bin(
        tmp_path,
        "npmpathdetail",
        "#!/bin/sh\n"
        'if [ "$1" = "config" ]; then\n'
        '  case "$3" in\n'
        "    @*:registry) echo undefined ;;\n"
        "    *) printf '\"https://registry.corp.invalid/npm/sekrit/\"\\n' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf "npm error code E403\\n" >&2\n'
        'printf "npm error 403 Forbidden - GET '
        'https://registry.corp.invalid/npm/sekrit/@mermaid-js%%2fmermaid-cli\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    assert "sekrit" not in proc.stdout, "the payload still carries it"
    assert "sekrit" not in proc.stderr, "the printed sentence still carries it"
    got = out_json(proc)
    assert got["cause"] == "forbidden"
    assert "registry.corp.invalid" in got["detail"], "the server is still named"


def test_a_token_in_the_registry_path_is_not_relayed(repo, keys_file, facts_path, tmp_path, stubs):
    """End to end: the payload must not carry it, and classification must not
    change because the payload stopped carrying it."""
    fake = _fake_bin(
        tmp_path,
        "npmpathtoken",
        "#!/bin/sh\n"
        'if [ "$1" = "config" ]; then\n'
        '  case "$3" in\n'
        "    @*:registry) echo undefined ;;\n"
        "    *) printf '\"https://registry.corp.invalid/npm/sekrit/\"\\n' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf "npm error code E404\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    assert "sekrit" not in proc.stdout
    got = out_json(proc)
    assert got["registry"] == "https://registry.corp.invalid/***"
    assert got["registry_is_public"] is False


def test_a_token_in_the_registry_query_is_not_relayed(repo, keys_file, facts_path, tmp_path, stubs):
    """A query carries a secret as readily as userinfo does.

    `https://registry.example/?token=...` is a shape real registries use, and
    the earlier redaction only looked before the authority's at-sign. What the
    agent needs from this field is which server answered, which the query never
    tells it.
    """
    fake = _fake_bin(
        tmp_path,
        "npmquerytoken",
        "#!/bin/sh\n"
        'if [ "$1" = "config" ]; then\n'
        '  case "$3" in\n'
        "    @*:registry) echo undefined ;;\n"
        "    *) printf '\"https://registry.corp.invalid/?token=sekrit\"\\n' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf "npm error code E404\\n" >&2\n'
        'printf "npm error 404 GET https://registry.corp.invalid/?token=sekrit\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    assert "sekrit" not in proc.stdout
    assert "sekrit" not in proc.stderr
    got = out_json(proc)
    assert got["registry"] == "https://registry.corp.invalid/?***"
    assert "sekrit" not in got["detail"]
    assert got["registry_is_public"] is False


def test_an_empty_scoped_answer_means_unset_and_does_fall_back(
    repo, keys_file, facts_path, tmp_path, stubs
):
    """The other half of the tri-state: answered-with-nothing is not refused.

    npm printing nothing for a key is npm saying the key is unset, and the
    default registry is then the right one to report. Reading that as "could not
    ask" would throw away an answer the run actually has -- the opposite error
    to the one its sibling test guards, and equally a wrong report.
    """
    fake = _fake_bin(
        tmp_path,
        "npmscopedempty",
        "#!/bin/sh\n"
        'if [ "$1" = "config" ]; then\n'
        '  case "$3" in\n'
        "    @*:registry) echo ;;\n"
        "    *) printf '\"https://npm.corp.invalid/\"\\n' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf "npm error code E404\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["registry"] == "https://npm.corp.invalid/"
    assert got["registry_is_public"] is False


def test_a_pin_refuses_when_the_global_config_cannot_be_pinned(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """Two earlier fixes meeting, and the meeting point had to be closed.

    `--prefix` stops a scratch inside a workspace being read as a member, and
    npm defines `globalconfig` as `{prefix}/etc/npmrc` -- so `--prefix` without
    the pin can move which global file is read. A selector in a user or global
    `.npmrc` refuses every `npm config` command, the pin's own probe included,
    and continuing then meant `--prefix` with no pin: the empty scratch config,
    and a version taken from npmjs while the user's registry is a mirror they
    are required to use. A wrong pin that looks like any other.

    So it refuses instead, loudly and with the usual cause named. The cost is
    real -- that configuration cannot pin at all now -- and it is the cheaper
    side of the trade.
    """
    # Inside a workspace, because that is the only place `--prefix` is passed
    # and so the only place the globalconfig question is asked at all.
    root = tmp_path / "wsroota"
    (root / "packages").mkdir(parents=True)
    (root / "package.json").write_text('{"name":"r","workspaces":["packages/*"]}\n')
    monkeypatch.setenv("TMPDIR", str(root / "packages"))
    fake = _fake_bin(
        tmp_path,
        "npmnoglobalconfig",
        "#!/bin/sh\n"
        'if [ "$3" = "globalconfig" ]; then\n'
        '  echo "npm error code ENOWORKSPACES" >&2\n'
        '  echo "This command does not support workspaces." >&2\n'
        "  exit 1\n"
        "fi\n"
        f"printf '\"{NPM_VERSION}\"\\n'\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6, proc.stderr
    got = out_json(proc)
    assert got["cause"] == "not-isolated"
    assert "workspace=" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_a_symlinked_root_manifest_still_counts_as_a_workspace(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """ "Could not read it" is not "there is nothing there".

    npm follows a symlinked `package.json` quite happily; this reader refuses
    one on a rule of its own. Counting that refusal as proof of no workspace let
    an enclosing project redirect the pin it was supposed to be isolated from --
    the same conflation of "no" with "don't know" that has bitten this branch
    elsewhere, here between a reader's policy and a fact about the filesystem.

    The stub demands `--prefix`, so this passes only if the unreadable manifest
    was treated as a root.
    """
    root = tmp_path / "symlinkws"
    (root / "packages").mkdir(parents=True)
    real = tmp_path / "elsewhere-manifest.json"
    real.write_text('{"name":"r","workspaces":["packages/*"]}\n')
    (root / "package.json").symlink_to(real)
    monkeypatch.setenv("TMPDIR", str(root / "packages"))

    fake = _fake_bin(
        tmp_path,
        "npmsymlinkws",
        "#!/bin/sh\n"
        'if [ "$3" = "globalconfig" ]; then printf \'"/etc/npmrc"\\n\'; exit 0; fi\n'
        'case " $* " in *" --prefix "*) ;; *)\n'
        '  echo "a symlinked root was read as no workspace: $*" >&2; exit 9;; esac\n'
        f"printf '\"{NPM_VERSION}\"\\n'\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 0, proc.stderr
    assert (
        f"@mermaid-js/mermaid-cli@{NPM_VERSION}" in (repo / ".pre-commit-config.yaml").read_text()
    )


def test_an_unparseable_manifest_declares_nothing(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """The other half, and it goes the other way.

    A `package.json` that will not parse is not one npm would honour either, so
    it declares no workspace and must not drag in `--prefix` and the globalconfig
    question behind it. Unreadable and unparseable are different answers.
    """
    root = tmp_path / "brokenws"
    (root / "packages").mkdir(parents=True)
    (root / "package.json").write_text("{ this is not json\n")
    monkeypatch.setenv("TMPDIR", str(root / "packages"))
    fake = _fake_bin(
        tmp_path,
        "npmbrokenws",
        "#!/bin/sh\n"
        'if [ "$3" = "globalconfig" ]; then echo "refused" >&2; exit 1; fi\n'
        f"printf '\"{NPM_VERSION}\"\\n'\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 0, proc.stderr


def test_a_workspaces_key_with_no_members_is_not_a_workspace(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """`workspaces: []` declares nobody, so nothing can be read as a member.

    Treating the key's presence as enough would pass `--prefix` there, and with
    it the globalconfig question -- which is exactly the dependency that has to
    stay out of runs that were never at risk. The stub refuses that question, so
    this passes only if it is never asked.
    """
    root = tmp_path / "emptyws"
    (root / "packages").mkdir(parents=True)
    (root / "package.json").write_text('{"name":"r","workspaces":[]}\n')
    monkeypatch.setenv("TMPDIR", str(root / "packages"))
    fake = _fake_bin(
        tmp_path,
        "npmemptyws",
        "#!/bin/sh\n"
        'if [ "$3" = "globalconfig" ]; then echo "refused" >&2; exit 1; fi\n'
        f"printf '\"{NPM_VERSION}\"\\n'\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 0, proc.stderr
    assert (
        f"@mermaid-js/mermaid-cli@{NPM_VERSION}" in (repo / ".pre-commit-config.yaml").read_text()
    )


def test_a_probe_that_cannot_be_rooted_answers_nothing(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """The same failure on the registry probe degrades rather than refuses.

    A probe naming a registry read from the wrong global file is worse than a
    probe naming none: the payload is what SKILL.md attributes blame from. The
    pin refuses, this answers nothing, and both come from the same unanswerable
    question.
    """
    # Inside a workspace, because that is the only place `--prefix` is passed
    # and so the only place the globalconfig question is asked at all.
    root = tmp_path / "wsrootb"
    (root / "packages").mkdir(parents=True)
    (root / "package.json").write_text('{"name":"r","workspaces":["packages/*"]}\n')
    monkeypatch.setenv("TMPDIR", str(root / "packages"))
    fake = _fake_bin(
        tmp_path,
        "npmnoglobalcfg2",
        "#!/bin/sh\n"
        'if [ "$3" = "globalconfig" ]; then echo "nope" >&2; exit 1; fi\n'
        'if [ "$1" = "config" ]; then printf \'"https://npm.corp.invalid/"\\n\'; exit 0; fi\n'
        'printf "npm error code E404\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    # The pin refuses first, on the same question -- which is the point: one
    # unanswerable probe, two callers, neither of them proceeding regardless.
    assert proc.returncode == 6
    assert out_json(proc)["cause"] == "not-isolated"


def test_an_inherited_workspace_selector_is_kept_out_of_npms_environment(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """`npm config get` refuses to run at all when a workspace is selected.

    ENOWORKSPACES, "This command does not support workspaces" -- so an inherited
    `workspace=` costs the registry lookup entirely, and with it the field that
    tells a 404 from a mirror apart from a bug in this catalog. `npm view`
    survives it; the probes do not, which is why fixing the pin last round left
    this behind.

    The environment is the only layer that can be cleared, and it is cleared.
    A selector in a user or global `.npmrc` stays, and the registry stays
    unknown -- reported as unknown rather than guessed.
    """
    monkeypatch.setenv("npm_config_workspace", "foo")
    monkeypatch.setenv("NPM_CONFIG_WORKSPACES", "true")
    fake = _fake_bin(
        tmp_path,
        "npmwsenv",
        "#!/bin/sh\n"
        '[ -z "${npm_config_workspace:-}" ] || { echo "selector survived" >&2; exit 9; }\n'
        '[ -z "${NPM_CONFIG_WORKSPACES:-}" ] || { echo "selector survived" >&2; exit 9; }\n'
        'if [ "$1" = "config" ]; then\n'
        '  case "$3" in\n'
        "    @*:registry) echo undefined ;;\n"
        "    *) printf '\"https://npm.corp.invalid/\"\\n' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf "npm error code E404\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6, proc.stderr
    got = out_json(proc)
    assert got["cause"] == "not-found"
    assert got["registry"] == "https://npm.corp.invalid/"


def test_a_scoped_lookup_that_fails_does_not_fall_back_to_the_default(
    repo, keys_file, facts_path, tmp_path, stubs
):
    """A scoped key that could not be read is not a scoped key that is absent.

    npm answering "unset" and npm refusing to answer both arrived as `None`, and
    the fallback treated them alike -- so a wrapper that rejects scoped config
    queries made the default registry look like the one that served a scoped
    package it never saw. With the default being npmjs, that reports
    `registry_is_public=true` for a private mirror's 404 and sends the user to
    blame this catalog.

    Only the scoped query fails here; the default one answers perfectly well,
    which is what makes the fallback look reasonable and be wrong.
    """
    fake = _fake_bin(
        tmp_path,
        "npmscopedfail",
        "#!/bin/sh\n"
        'if [ "$1" = "config" ]; then\n'
        '  case "$3" in\n'
        '    @*:registry) echo "scoped config is not permitted here" >&2; exit 1 ;;\n'
        "    *) printf '\"https://registry.npmjs.org/\"\\n' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf "npm error code E404\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["cause"] == "not-found"
    assert got["registry"] == "", "a registry that could not be determined must not be named"
    assert "registry_is_public" not in got, "and must not be called npmjs"


def test_a_registry_npm_will_not_name_is_reported_as_unknown_not_as_a_mirror(
    repo, keys_file, facts_path, tmp_path, stubs
):
    """Not knowing is not the same as knowing it was a mirror.

    npm withholds a registry that carries credentials, and the empty string it
    left behind went through `is_public_registry` and came back False -- which
    SKILL.md reads as "their mirror said no", so someone is sent to fix a
    registry that may be perfectly fine while a real catalog bug goes
    unreported. The field is now absent rather than false, and `registry` is
    empty, which the procedure treats as "attribute nothing".
    """
    fake = _fake_bin(
        tmp_path,
        "npmcoy",
        "#!/bin/sh\n"
        'if [ "$1" = "config" ]; then\n'
        '  echo "The registry option is protected, and can not be retrieved in this way." >&2\n'
        "  exit 1\n"
        "fi\n"
        'printf "npm error code E404\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["cause"] == "not-found"
    assert got["registry"] == ""
    assert "registry_is_public" not in got, "a False here is a claim the run cannot make"


def test_a_404_says_which_registry_answered(repo, keys_file, facts_path, tmp_path, stubs):
    """Honouring the user's registry makes this the ordinary case, not an edge.

    An enterprise mirror that does not proxy the package answers E404 exactly
    the way a wrong package name does. Told only "no such package", the agent
    reports a bug in this catalog at a user who can fix it in a minute by
    pointing npm somewhere that carries it -- so the run says which registry
    said no rather than leaving that to be inferred.
    """
    fake = _fake_bin(
        tmp_path,
        "npmmirror",
        "#!/bin/sh\n"
        # A scope with no registry of its own: npm answers `undefined`, and the
        # default is what actually served the request.
        'if [ "$1" = "config" ]; then\n'
        '  case "$3" in\n'
        "    @*:registry) echo undefined ;;\n"
        '    *) echo "https://npm.corp.invalid/" ;;\n'
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf "npm error code E404\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["cause"] == "not-found"
    assert got["registry"] == "https://npm.corp.invalid/"
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_the_path_survives_when_json_supplied_the_code(
    repo, keys_file, facts_path, tmp_path, stubs
):
    """npm splits one failure across both streams, so both have to be read.

    Its JSON error object carries `code`, `summary` and `detail`; `path` is only
    ever logged to stderr. Taking the object whole when it had anything at all
    therefore dropped `path` -- and dropped it into a meaning, because SKILL.md
    documents an empty `npm_path` as "the scratch directory could not be made",
    which is a different failure from a write that failed inside one that was.
    """
    fake = _fake_bin(
        tmp_path,
        "npmsplit",
        "#!/bin/sh\n"
        'printf "npm error path /tmp/x/npm-cache\\n" >&2\n'
        'printf \'%s\' \'{"error":{"code":"ENOSPC","summary":"no space"}}\'\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["cause"] == "filesystem"
    assert got["npm_code"] == "ENOSPC"
    assert got["npm_path"] == "/tmp/x/npm-cache", "the stream that has it is the one it came on"


def test_the_path_survives_a_renamed_log_heading(repo, keys_file, facts_path, tmp_path, stubs):
    """The word at the front of every npm log line is the user's to choose.

    Reading the cause from the JSON object fixed `heading=corp` for
    classification, but `path` is only ever on stderr, so a pattern anchored on
    `npm` still lost it -- and an empty `npm_path` is documented as "the scratch
    directory could not be made", which is a different failure from a write
    inside one that was. Two earlier fixes leaving a gap between them.

    The stub answers with a renamed heading *despite* being asked for
    `--heading=npm`, which is the case the flag cannot cover: an npm that does
    not honour it. The flag itself is pinned by the sibling test.
    """
    fake = _fake_bin(
        tmp_path,
        "npmheading",
        "#!/bin/sh\n"
        'printf "corp error path /tmp/pin/npm-cache\\n" >&2\n'
        'printf \'%s\' \'{"error":{"code":"EACCES","summary":"denied"}}\'\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["cause"] == "filesystem"
    assert got["npm_path"] == "/tmp/pin/npm-cache"


def test_both_npm_calls_refuse_to_be_a_workspace_member(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """A sealed scratch inside a workspace stops being sealed.

    npm decides membership from the ROOT's glob, not from the member, so a
    TMPDIR under `packages/` in a repo with `workspaces: ["packages/*"]` makes
    the scratch a member -- and a member's project config is the root's, not the
    two files planted beside it. Observed on npm 10.9.8: `npm view` in that
    position answers nothing at all, and `npm config get` refuses outright;
    npm 11 is reported to answer from the workspace's `.npmrc` instead, which is
    the same seal failing more quietly.

    Both npm calls are checked, because the pin and the registry probe are
    separate commands and only one of them was covered when this was written.

    `--prefix` rather than `--no-workspaces`: the latter isolates just as well
    and then refuses to run at all beside an inherited `workspace=` selector,
    which a user or global .npmrc may set and this cannot rewrite. Naming the
    project root outright conflicts with nothing.

    And `--globalconfig` beside it, because npm documents that file as living
    under the prefix -- so naming a prefix can move which global npmrc is read,
    and the user's registry or proxy may live only there. The stub checks that
    the probe which learns that path is itself unrooted, since a rooted one
    would be asking the question it exists to answer.
    """
    root = tmp_path / "wsroot"
    (root / "packages").mkdir(parents=True)
    (root / "package.json").write_text('{"name":"r","workspaces":["packages/*"]}\n')
    (root / ".npmrc").write_text("registry=https://evil.invalid/\n")
    monkeypatch.setenv("TMPDIR", str(root / "packages"))

    fake = _fake_bin(
        tmp_path,
        "npmworkspace",
        "#!/bin/sh\n"
        # The scratch is what --prefix must name, and the stub is run with the
        # scratch as its cwd -- so this checks the value, not just the flag.
        # The globalconfig probe is the one call that must NOT be rooted --
        # it is how the path --prefix might displace is learned.
        'if [ "$3" = "globalconfig" ]; then\n'
        '  case " $* " in *" --prefix "*)\n'
        '    echo "the globalconfig probe must not be rooted" >&2; exit 8;; esac\n'
        "  printf '\"/etc/npmrc\"\\n'; exit 0\n"
        "fi\n"
        'case " $* " in *" --prefix $PWD "*) ;; *)\n'
        '  echo "not asked with --prefix at the scratch: $*" >&2; exit 9;; esac\n'
        # Everything else must carry the pinned global config beside --prefix,
        # or --prefix may quietly move which global npmrc npm reads.
        'case " $* " in *" --globalconfig /etc/npmrc "*) ;; *)\n'
        '  echo "global config not pinned: $*" >&2; exit 7;; esac\n'
        'if [ "$1" = "config" ]; then\n'
        '  case "$3" in\n'
        "    @*:registry) echo undefined ;;\n"
        "    *) printf '\"https://registry.npmjs.org/\"\\n' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf "npm error code E404\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6, proc.stderr
    got = out_json(proc)
    # Reached the registry probe, which means the pin call carried the flag too:
    # without it the stub exits 9 and the cause would be `unknown`.
    assert got["cause"] == "not-found"
    assert got["registry"] == "https://registry.npmjs.org/"


def test_the_pin_asks_npm_for_a_stable_log_heading(repo, keys_file, facts_path, tmp_path, stubs):
    """Prevention as well as recovery, since the loosened pattern is a fallback
    and a fallback nobody needs is cheaper than one everybody does.

    The decoy line guards the other direction: loosening the prefix must not
    become matching any sentence that happens to contain the word `path`, which
    would take a warning's filename over the one npm reported the error for.
    """
    fake = _fake_bin(
        tmp_path,
        "npmheadflag",
        "#!/bin/sh\n"
        'case " $* " in *" --heading=npm "*) ;; *) echo "no --heading" >&2; exit 8;; esac\n'
        'printf "npm error code EACCES\\nnpm error path /tmp/pin/npm-cache\\n" >&2\n'
        # A decoy after the real line, because these are read last-one-wins: a
        # pattern loose enough to match ordinary prose would take this instead.
        'printf "npm warn deprecated check the path /not/the/cache\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["cause"] == "filesystem"
    assert got["npm_path"] == "/tmp/pin/npm-cache"


def test_the_json_copy_wins_where_the_two_streams_disagree(
    repo, keys_file, facts_path, tmp_path, stubs
):
    """Merging needs a direction, and stderr is the copy the user can reshape.

    `heading`, `loglevel` and `color` all rewrite the stderr lines; nothing
    the user configures touches the JSON error object. So where both name a
    field, the object is the one to believe -- filling gaps from stderr must
    not become overwriting from it.
    """
    fake = _fake_bin(
        tmp_path,
        "npmdisagree",
        "#!/bin/sh\n"
        'printf "npm error code E404\\n" >&2\n'
        'printf \'%s\' \'{"error":{"code":"E401","summary":"Unauthorized"}}\'\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["npm_code"] == "E401"
    assert got["cause"] == "auth"


def test_non_json_on_stdout_does_not_stop_the_classification(
    repo, keys_file, facts_path, tmp_path, stubs
):
    """`--json` is asked for, not guaranteed.

    An npm that answers a failure with something other than a JSON object --
    an older one, or a wrapper script on PATH -- must not turn a parse error
    into a crash. The stderr lines are still there, and they still say E401.
    """
    fake = _fake_bin(
        tmp_path,
        "npmjunkout",
        '#!/bin/sh\necho "not json at all"\nprintf "npm error code E401\\n" >&2\nexit 1\n',
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["cause"] == "auth"
    assert got["npm_code"] == "E401"


@pytest.mark.parametrize(
    "stderr_line,label",
    [
        pytest.param("corp error code E403", "a renamed heading", id="heading=corp"),
        pytest.param("", "nothing on stderr at all", id="loglevel=silent"),
    ],
)
def test_the_cause_survives_npms_logging_configuration(
    repo, keys_file, facts_path, tmp_path, stubs, stderr_line, label
):
    """`^npm error code` assumed two settings that are the user's to change.

    `heading` is the string npm puts in front of every log line -- `npm` by
    default and anything at all if they say so -- and `loglevel=silent` removes
    the lines entirely. Both are honoured here like the rest of their npm
    configuration, so a pattern anchored on `npm` was reading `unknown` off a
    machine whose only fault was a logging preference.

    Under `--json` npm reports the failure as an object on stdout, which no
    logging setting touches. The stub emits that and whatever stderr the setting
    would have left.
    """
    emit = f"printf \"%s\\n\" '{stderr_line}' >&2\n" if stderr_line else ""
    fake = _fake_bin(
        tmp_path,
        "npmlogcfg",
        "#!/bin/sh\n"
        'case " $* " in *" --loglevel=error "*) ;; *) echo "no --loglevel" >&2; exit 7;; esac\n'
        + emit
        + 'printf \'%s\' \'{"error":{"code":"E403","summary":"Forbidden"}}\'\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["cause"] == "forbidden", label
    assert got["npm_code"] == "E403"
    assert got["detail"], "something of npm's own must survive to be quoted"
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_a_huge_npm_complaint_is_bounded_in_both_places_it_is_relayed(
    repo, keys_file, facts_path, tmp_path, stubs
):
    """The sentence and the JSON field are the same channel.

    npm's stderr is a registry's text, and SKILL.md relays the failure message
    as well as the structured fields -- so capping `detail` while the message it
    is printed beside runs free caps nothing. A verbose npm, or a registry that
    answers with a megabyte, reaches the agent's context either way.
    """
    fake = _fake_bin(
        tmp_path,
        "npmshouty",
        "#!/bin/sh\n"
        'i=0; while [ $i -lt 400 ]; do printf "npm error xxxxxxxxxxxxxxxxxxxx\\n" >&2; i=$((i+1)); done\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert len(got["detail"]) < 600, "the JSON field is unbounded"
    assert len(proc.stderr) < 600, "the printed sentence is unbounded"
    assert "(truncated)" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_an_npm_that_will_not_start_is_not_reported_as_missing(
    repo, keys_file, facts_path, tmp_path, stubs
):
    """The exception cannot tell these apart, and the advice differs.

    An npm on PATH whose shebang interpreter is gone raises the same
    FileNotFoundError as no npm at all -- so "install npm" was the answer given
    to someone whose npm is installed and broken. `only_path`, because exec
    walks past a file it cannot start and would otherwise find the real npm
    behind this one, and the test would pass by asking that.
    """
    fake = _fake_bin(tmp_path, "npmbroken", "#!/nonexistent/interpreter\n")
    (fake / "git").symlink_to(stubs / "git")
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--templates-file",
        str(keys_file("mermaid")),
        "--facts-out",
        str(facts_path),
        stubs=fake,
        only_path=True,
    )
    assert proc.returncode == 6, proc.stderr
    got = out_json(proc)
    assert got["cause"] == "unrunnable"
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_a_missing_npm_is_named_as_such(repo, keys_file, facts_path, tmp_path, stubs):
    """Only `mermaid` needs npm; the agent has to be able to say that rather
    than report a generic failure over the whole selection."""
    bare = tmp_path / "nonpm"
    bare.mkdir()
    (bare / "git").symlink_to(stubs / "git")
    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--templates-file",
        str(keys_file("mermaid")),
        "--facts-out",
        str(facts_path),
        stubs=bare,
        only_path=True,
    )
    assert proc.returncode == 6
    assert out_json(proc)["cause"] == "npm-missing"
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_a_git_init_that_seals_nothing_is_not_believed(repo, keys_file, facts_path, tmp_path):
    """Exit zero is not evidence that this directory was sealed.

    `git init` reports success for repositories other than the one asked for --
    an exported GIT_DIR was one way, and clearing those variables closes that
    way rather than the shape of it. The check is what notices a later git
    growing a route nobody here knows about, so it is tested on its own terms:
    a git that says yes and does nothing.
    """
    fake = tmp_path / "gitlyinginit"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "init" ]; then exit 0; fi\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    proc = generate(repo, keys_file, facts_path, fake, "hygiene")
    assert proc.returncode == 6, proc.stderr
    got = out_json(proc)
    assert got["cause"] == "not-isolated"
    assert "left no repository" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_an_exported_git_dir_cannot_hollow_out_the_seal(
    repo, keys_file, facts_path, stubs, tmp_path, monkeypatch
):
    """`git init` reports success for a repository other than the one asked for.

    With GIT_DIR exported, `git -C <scratch> init` reinitialises *that*
    repository, exits 0, and leaves nothing in the scratch directory -- so the
    seal read as applied while the other repository's config was still the one
    in force, and its `url.<other>.insteadOf` could still redirect the lookup.
    A guard that reports success having done nothing is worse than no guard.

    The stub checks the two things that were false: that the lookup runs with no
    repository-selecting variable in its environment, and that the scratch
    really does hold a repository of its own.
    """
    external = tmp_path / "external"
    external.mkdir()
    subprocess.run(["git", "-C", str(external), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(external), "config", "url.https://evil.invalid/.insteadOf", "https://"],
        check=True,
    )
    monkeypatch.setenv("GIT_DIR", str(external / ".git"))

    fake = tmp_path / "gitdircheck"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'here=""; prev=""; want=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "-C" ]; then here="$a"; fi\n'
        '  if [ "$a" = "ls-remote" ]; then want=yes; fi\n'
        '  prev="$a"\n'
        "done\n"
        'if [ "$want" = yes ]; then\n'
        '  [ -z "${GIT_DIR:-}" ] || { echo "GIT_DIR survived into the lookup" >&2; exit 9; }\n'
        '  [ -e "$here/.git" ] || { echo "the scratch $here holds no repository" >&2; exit 9; }\n'
        "  printf '%s\\n' "
        '"1111111111111111111111111111111111111111\trefs/tags/v10.0.1"\n'
        "  exit 0\n"
        "fi\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)

    proc = generate(repo, keys_file, facts_path, fake, "hygiene")
    assert proc.returncode == 0, proc.stderr
    assert "rev: v10.0.1" in (repo / ".pre-commit-config.yaml").read_text()


def test_a_git_template_cannot_smuggle_config_into_the_seal(
    repo, keys_file, facts_path, stubs, tmp_path, monkeypatch
):
    """The command that applies the seal can carry the thing it seals against.

    `git init` copies a template directory into the new repository, and that
    template may hold a `config`. `isolated` turns off the system and global
    files; GIT_TEMPLATE_DIR is a third way in that it does not cover, so a
    template carrying `url.<other>.insteadOf` was copied straight into the
    scratch repository the seal had just made -- leaving the seal in place and
    the redirect inside it.

    The stub asks real git, in the directory git was pointed at, so this fails
    if `--template=` is ever dropped rather than asserting that a flag was
    passed.
    """
    tpl = tmp_path / "tpl"
    tpl.mkdir()
    (tpl / "config").write_text('[url "https://evil.invalid/"]\n\tinsteadOf = https://\n')
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tpl))

    fake = tmp_path / "templatecheck"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'here=""; prev=""; want=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "-C" ]; then here="$a"; fi\n'
        '  if [ "$a" = "ls-remote" ]; then want=yes; fi\n'
        '  prev="$a"\n'
        "done\n"
        'if [ "$want" = yes ]; then\n'
        f'  if {REAL_GIT} -C "$here" config --get url.https://evil.invalid/.insteadOf >/dev/null 2>&1\n'
        "  then\n"
        '    echo "the template config is inside the sealed scratch $here" >&2; exit 9\n'
        "  fi\n"
        "  printf '%s\\n' "
        '"1111111111111111111111111111111111111111\trefs/tags/v10.0.1"\n'
        "  exit 0\n"
        "fi\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)

    proc = generate(repo, keys_file, facts_path, fake, "hygiene")
    assert proc.returncode == 0, proc.stderr
    assert "rev: v10.0.1" in (repo / ".pre-commit-config.yaml").read_text()


def test_a_tmpdir_inside_someones_repository_cannot_reach_the_pin(
    repo, keys_file, facts_path, stubs, tmp_path, monkeypatch
):
    """The scratch cwd is pinning's isolation, and where it lands is not ours.

    git discovers the first `.git` above cwd and npm the first `package.json`,
    so a TMPDIR under any repository -- the target, or a stranger's -- lets that
    repository rewrite a catalog URL with `url.<other>.insteadOf` or name the
    registry, and the pin that comes back looks like any other. `/tmp` is itself
    a git repository on at least one machine this was written on, so refusing
    such a TMPDIR would refuse the machine; the scratch is sealed instead, with
    its own empty repository and empty project for both tools to stop at.

    The stub asks real git, from its own cwd, whether the hostile setting is
    visible -- so this fails if the seal ever stops working, rather than
    asserting that a flag was passed.
    """
    hostile = tmp_path / "someones-repo"
    (hostile / "tmp").mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(hostile)], check=True)
    subprocess.run(
        ["git", "-C", str(hostile), "config", "url.https://evil.invalid/.insteadOf", "https://"],
        check=True,
    )
    (hostile / "package.json").write_text("{}\n")
    (hostile / ".npmrc").write_text("registry=https://evil.invalid/\n")

    fake = tmp_path / "sealcheck"
    fake.mkdir()
    g = fake / "git"
    # `-C <dir>` moves git's idea of where it is, not this script's, so the
    # check has to be made in that directory or it inspects the wrong one --
    # which is how the first version of this test passed while proving nothing.
    g.write_text(
        "#!/bin/sh\n"
        'here=""; prev=""; want=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "-C" ]; then here="$a"; fi\n'
        '  if [ "$a" = "ls-remote" ]; then want=yes; fi\n'
        '  prev="$a"\n'
        "done\n"
        'if [ "$want" = yes ]; then\n'
        f'  if {REAL_GIT} -C "$here" config --get url.https://evil.invalid/.insteadOf >/dev/null 2>&1\n'
        "  then\n"
        '    echo "the enclosing repository is visible from $here" >&2; exit 9\n'
        "  fi\n"
        "  printf '%s\\n' "
        '"1111111111111111111111111111111111111111\trefs/tags/v10.0.1"\n'
        "  exit 0\n"
        "fi\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    monkeypatch.setenv("TMPDIR", str(hostile / "tmp"))

    proc = generate(repo, keys_file, facts_path, fake, "hygiene")
    assert proc.returncode == 0, proc.stderr
    assert "rev: v10.0.1" in (repo / ".pre-commit-config.yaml").read_text()


def test_a_probe_that_cannot_be_sealed_answers_nothing(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """Sealing the probe is only half of it; the other half is what happens when
    the seal fails.

    An unsealed probe that answers anyway reports whichever registry the
    enclosing project names, which is worse than reporting none: SKILL.md reads
    an empty `registry` as "npm would not say" and attributes nothing, and reads
    a filled one as fact. The stub lets the pin's own seal succeed and fails
    every one after it, which is the only way to reach the probe's.
    """
    fake = _fake_bin(
        tmp_path,
        "npmprobeunsealed",
        "#!/bin/sh\n"
        'if [ "$1" = "config" ]; then printf \'"https://npm.corp.invalid/"\\n\'; exit 0; fi\n'
        'printf "npm error code E404\\n" >&2\n'
        "exit 1\n",
    )
    counter = tmp_path / "init-count"
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "init" ]; then\n'
        f'    n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"\n'
        '    [ "$n" -le 1 ] || { echo "fatal: no more repositories for you" >&2; exit 128; }\n'
        "  fi\n"
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)

    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6, proc.stderr
    got = out_json(proc)
    assert got["cause"] == "not-found"
    assert got["registry"] == "", "an unsealed probe must not name a registry"
    assert "registry_is_public" not in got


def test_the_registry_probe_is_sealed_like_the_pin_is(repo, keys_file, facts_path, tmp_path, stubs):
    """`npm config get registry` is asked in a scratch too, and it decides what
    the user is told about who refused their package.

    Unsealed, the project enclosing whatever TMPDIR names answers it, and the
    registry reported is a stranger's rather than the one npm asked -- which is
    the exact wrongness the field exists to prevent, one function away from the
    seal that prevents it.
    """
    fake = _fake_bin(
        tmp_path,
        "npmprobeseal",
        "#!/bin/sh\n"
        'if [ "$1" = "config" ]; then\n'
        '  [ -f "$PWD/package.json" ] || { echo "probe scratch not sealed" >&2; exit 9; }\n'
        '  [ -f "$PWD/.npmrc" ] || { echo "probe scratch not sealed" >&2; exit 9; }\n'
        '  case "$3" in\n'
        "    @*:registry) echo undefined ;;\n"
        "    *) printf '\"https://npm.corp.invalid/\"\\n' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'printf "npm error code E404\\n" >&2\n'
        "exit 1\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 6
    got = out_json(proc)
    assert got["cause"] == "not-found"
    assert got["registry"] == "https://npm.corp.invalid/"


def test_the_scratch_carries_the_marker_npm_stops_at(repo, keys_file, facts_path, stubs, tmp_path):
    """git's half of the seal and npm's are separate, and so are their proofs.

    npm walks up for a `package.json` and reads the `.npmrc` beside it, so the
    scratch needs both of its own or the enclosing project's registry is the one
    it asks. The stub checks its own cwd, which is the scratch, rather than
    trusting that planting them happened.
    """
    fake = _fake_bin(
        tmp_path,
        "npmsealed",
        "#!/bin/sh\n"
        '[ -f "$PWD/package.json" ] || { echo "no package.json in the scratch" >&2; exit 9; }\n'
        '[ -f "$PWD/.npmrc" ] || { echo "no .npmrc in the scratch" >&2; exit 9; }\n'
        f"printf '\"{NPM_VERSION}\"\\n'\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 0, proc.stderr
    assert (
        f"@mermaid-js/mermaid-cli@{NPM_VERSION}" in (repo / ".pre-commit-config.yaml").read_text()
    )


def test_a_scratch_that_cannot_be_sealed_stops_the_run(repo, keys_file, facts_path, tmp_path):
    """An unsealed scratch is not one to pin from.

    If the seal cannot be applied there is no isolation, and pinning anyway
    would produce a version that looks exactly like a trustworthy one. Fails
    closed, before any version is fetched and so before anything is written.
    """
    fake = tmp_path / "gitnoinit"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "init" ]; then echo "fatal: cannot init" >&2; exit 128; fi\n'
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    proc = generate(repo, keys_file, facts_path, fake, "hygiene")
    assert proc.returncode == 6, proc.stderr
    assert out_json(proc)["cause"] == "not-isolated"
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_the_pin_runs_where_the_repos_own_npmrc_cannot_reach_it(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """The repository being configured does not get a vote on the registry.

    npm reads `.npmrc` from the directory it is run in and from that project's
    ancestors, so pinning inside the target repo would let the repo name the
    server that answers for a catalog package. The scratch cwd is the whole of
    the defence, and nothing asserted it -- so `cwd=elsewhere` could have been
    dropped as a tidy-up with the suite still green.

    Note what is NOT claimed: the user's own npm configuration is deliberately
    honoured, no `--registry` is forced, and the proxy variables are passed
    through. See `npm_latest`.

    `cwd=repo` on the run itself is load-bearing, and this test was wrong once
    without it: a child with no cwd of its own inherits the parent's, and the
    parent here is pytest sitting in the checkout. So deleting `cwd=elsewhere`
    still put npm somewhere outside the repository and the test passed while
    proving nothing. Started from inside the repo, the same deletion fails.
    """
    (repo / ".npmrc").write_text("registry=http://not-the-registry.invalid/\n")
    fake = _fake_bin(
        tmp_path,
        "npmcwd",
        f'#!/bin/sh\nprintf "%s" "$PWD" > "$MP_CWD_LOG"\necho {NPM_VERSION}\n',
    )
    (fake / "git").symlink_to(stubs / "git")
    cwd_log = tmp_path / "npm-cwd.txt"
    monkeypatch.setenv("MP_CWD_LOG", str(cwd_log))

    proc = run(
        "precommit.py",
        "--dir",
        str(repo),
        "--templates-file",
        str(keys_file("mermaid")),
        "--facts-out",
        str(facts_path),
        stubs=fake,
        cwd=repo,
    )
    assert proc.returncode == 0, proc.stderr
    where = os.path.realpath(cwd_log.read_text())
    root = os.path.realpath(repo)
    assert where != root
    assert not where.startswith(root + os.sep)
    # And the bait was really there, so a run that stopped calling npm at all
    # could not pass this by doing nothing.
    assert (repo / ".npmrc").is_file()
    assert not os.path.exists(os.path.join(where, ".npmrc"))


def test_the_pin_does_not_need_a_writable_home(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """`--cache` was the point, but HOME is the thing that was really missing.

    npm's cache defaults under `$HOME`, and its log directory hangs off the
    cache -- so with the cache named explicitly, nothing in the pin should need
    a home directory it can write to. That is a deduction until something runs
    it: the environment in the report had `HOME=/root` and no way to create
    anything beneath it.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    nowhere = str(blocker / "home")
    fake = _fake_bin(
        tmp_path,
        "npmnohome",
        "#!/bin/sh\n"
        "set -eu\n"
        f'[ "$HOME" = "{nowhere}" ] || {{ echo "HOME was repaired for us" >&2; exit 5; }}\n'
        f"echo {NPM_VERSION}\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    monkeypatch.setenv("HOME", nowhere)

    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 0, proc.stderr
    assert (
        f"@mermaid-js/mermaid-cli@{NPM_VERSION}" in (repo / ".pre-commit-config.yaml").read_text()
    )


def test_the_pin_names_its_cache_even_when_nothing_was_inherited(
    repo, keys_file, facts_path, tmp_path, stubs, monkeypatch
):
    """The ordinary environment, stated rather than assumed.

    Every other test here runs with `NPM_CONFIG_CACHE` unset, which makes this
    the case that is covered everywhere and asserted nowhere -- and a "fix" that
    only passed `--cache` when it saw a hostile value would sail through all of
    them.
    """
    fake = _fake_bin(
        tmp_path,
        "npmnoenv",
        "#!/bin/sh\n"
        "set -eu\n"
        '[ -z "${NPM_CONFIG_CACHE-}" ] || { echo "expected it unset" >&2; exit 5; }\n'
        'case " $* " in *" --cache "*) ;; *) echo "no --cache" >&2; exit 6;; esac\n'
        f"echo {NPM_VERSION}\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    monkeypatch.delenv("NPM_CONFIG_CACHE", raising=False)
    monkeypatch.delenv("npm_config_cache", raising=False)

    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 0, proc.stderr
    assert (
        f"@mermaid-js/mermaid-cli@{NPM_VERSION}" in (repo / ".pre-commit-config.yaml").read_text()
    )


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


def test_the_pin_asks_for_latest_and_for_a_shape_it_chose(
    repo, keys_file, facts_path, tmp_path, stubs
):
    """npm reads the tag AND the output format out of the user's .npmrc.

    `tag=next` makes `npm view <pkg> version` answer for that tag instead, and
    a prerelease that happens to look like a version is written into their
    config as though it were the latest release -- the quiet half. `json=true`
    is the loud half: every answer comes back quoted, the version check calls a
    perfectly good lookup garbage, and mermaid can never be installed at all.

    Both are asked for explicitly rather than hoped for, so the stub refuses
    anything else and answers in JSON as npm would.
    """
    fake = _fake_bin(
        tmp_path,
        "npmspec",
        "#!/bin/sh\n"
        'case " $* " in *" @mermaid-js/mermaid-cli@latest "*) ;; *)\n'
        '  echo "asked for the configured tag, not latest" >&2; exit 5;; esac\n'
        'case " $* " in *" --json "*) ;; *) echo "no --json" >&2; exit 6;; esac\n'
        f"printf '\"{NPM_VERSION}\"\\n'\n",
    )
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 0, proc.stderr
    assert (
        f"@mermaid-js/mermaid-cli@{NPM_VERSION}" in (repo / ".pre-commit-config.yaml").read_text()
    )


def test_an_npm_that_ignores_the_format_flag_still_works(
    repo, keys_file, facts_path, tmp_path, stubs
):
    """The flag is prevention, and prevention that has no fallback is a bet.

    An npm old or odd enough to print a bare version despite `--json` should
    keep working rather than fail in a new way -- the parse falls back to the
    raw text, which is what every npm printed before this branch existed.
    """
    fake = _fake_bin(tmp_path, "npmbare", f"#!/bin/sh\necho {NPM_VERSION}\n")
    (fake / "git").symlink_to(stubs / "git")
    proc = generate(repo, keys_file, facts_path, fake, "mermaid")
    assert proc.returncode == 0, proc.stderr
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
    assert not {"mermaid", "mermaid-parse"} & set(names)
    assert got["detected"] == []


def test_a_top_level_markdown_file_still_drives_it(repo, stubs):
    """The companion to the exclusion above: it must not exclude everything."""
    (repo / "doc.md").write_text("# hi\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    names = {r["name"] for r in got["recommended"]}
    assert {"markdownlint", "mermaid-parse"} <= names


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


def test_a_files_scope_is_judged_against_the_repository_not_by_its_presence(repo, stubs):
    """A `files:` is a scope, not a switch. This one matches the README the
    fixture repo carries, so the hook runs -- and the mere presence of the key
    used to read as "disabled", which told every repository that selected
    `mermaid` (whose fragment scopes to Markdown) that its check was dead."""
    got = _disabled_for(repo, stubs, "        files: '\\.md$'\n")
    assert got["disabled"] == {}, got["disabled"]


def test_our_own_mermaid_hook_is_not_reported_as_disabled(repo, keys_file, facts_path, stubs):
    """The regression as users met it: select `mermaid`, run the scan again,
    and be told the hook you just installed will not run. Its `files:` scope
    matches the Markdown that got it recommended in the first place."""
    (repo / "doc.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    generate(repo, keys_file, facts_path, stubs, "mermaid")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["previous"] == ["mermaid"]
    assert got["disabled"] == {}, got["disabled"]
    # And, being live, it keeps its alternative from being offered beside it.
    assert "mermaid-parse" not in {r["name"] for r in got["recommended"]}


def test_an_escaped_space_at_the_end_of_the_keys_line_is_content(repo, stubs):
    """`files: "^README\\ ` over `[.]md$"` is the pattern `^README  [.]md$` --
    the escaped space, then the folded break -- which matches no file here.
    Trimmed with the line, the backslash read as escaping the break, the
    pattern became `^README[.]md$`, and a hook that never runs on README.md
    read as covering its fence."""
    (repo / "README.md").write_text("# hi\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-parse\n        name: mermaid-parse\n"
        "        entry: node scripts/parse-mermaid.mjs\n        language: node\n"
        '        files: "^README\\ \n          [.]md$"\n'
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid-parse" in got["disabled"], got["disabled"]


def test_a_non_string_tag_on_a_filter_refuses_the_config(repo, stubs):
    """`files: !!int 123` is a number to YAML, which pre-commit rejects where it
    wants a regex, so the file runs no hook. Read as the text `123` it was a
    live pattern -- one that reaches `123.md`."""
    (repo / "123.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-parse\n        name: mermaid-parse\n"
        "        entry: node scripts/parse-mermaid.mjs\n        language: node\n"
        "        files: !!int 123\n"
    )
    proc = run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs)
    assert proc.returncode == 5, proc.stderr
    got = out_json(proc)
    assert got["reason"] == "config-refused"
    assert got["line"] == 8


def test_a_type_filter_is_judged_only_where_identify_would_agree(repo, stubs):
    """pre-commit applies `types`, `types_or` and `exclude_types` on top of the
    regex filters, by identify's tags -- which come from the extension, from
    well-known names (a `README.md` is `plain-text` too), from the mode and
    from the contents, a database this tool does not carry. So only a certain
    verdict is given. Every Markdown file is `file`, `text` and `markdown`, and
    never `binary`: `exclude_types: [text]` drops them all and `types:
    [binary]` admits none -- dead. `types: [markdown]` admits them all -- live,
    nothing to say. `types: [python]` or `exclude_types: [executable]` may or
    may not admit a given file: not dead, and not shown to reach the fence
    either, which is what the report says. gitleaks is for every file, so only
    `file` is certain for it."""
    (repo / "doc.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    hook = (
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-parse\n        name: mermaid-parse\n"
        "        entry: node scripts/parse-mermaid.mjs\n        language: node\n"
    )

    def verdict(extra: str) -> str:
        (repo / ".pre-commit-config.yaml").write_text(hook + extra)
        got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
        return got["disabled"].get("mermaid-parse", [""])[0]

    assert "every file this entry is for is `text`" in verdict(
        "        exclude_types:\n          - text\n"
    )
    assert "no file this entry is for can be `binary`" in verdict("        types: [binary]\n")
    assert "types_or: [directory, socket]" in verdict("        types_or: [directory, socket]\n")
    assert verdict("        types: [markdown]\n") == ""
    assert verdict("        types_or: [text, python]\n") == ""
    assert "whether it reaches doc.md is not shown" in verdict("        types: [python]\n")
    assert "is not shown" in verdict("        exclude_types: [executable]\n")
    got = _disabled_for(repo, stubs, "        types: [python]\n")
    assert "gitleaks" not in got["disabled"], got["disabled"]
    got = _disabled_for(repo, stubs, "        exclude_types: [file]\n")
    assert "every file this entry is for is `file`" in got["disabled"]["gitleaks"][0]


def test_a_typed_alternative_stands_in_only_where_identify_would_agree(
    repo, keys_file, facts_path, stubs
):
    """A live `mermaid` with `types: [executable]` may or may not run on the
    README the fence is in -- that is identify's call, on the file's mode -- so
    it does not stand in, and `mermaid-parse` stays recommended. Typed
    `[markdown]`, it certainly does, and stands in."""
    (repo / "README.md").write_text("# hi\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    hook = (
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
    )
    (repo / ".pre-commit-config.yaml").write_text(hook + "        types: [executable]\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]
    assert "mermaid-parse" in {r["name"] for r in got["recommended"]}
    (repo / ".pre-commit-config.yaml").write_text(hook + "        types: [markdown]\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid-parse" not in {r["name"] for r in got["recommended"]}


def test_an_implicitly_typed_filter_refuses_the_config(repo, stubs):
    """`files: null` is None to YAML, which pre-commit rejects where it wants a
    regex, so the file runs no hook. Read as the text `null` it was a live
    pattern -- one that reaches `null.md`."""
    (repo / "null.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-parse\n        name: mermaid-parse\n"
        "        entry: node scripts/parse-mermaid.mjs\n        language: node\n"
        "        files: null\n"
    )
    proc = run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs)
    assert proc.returncode == 5, proc.stderr
    got = out_json(proc)
    assert got["reason"] == "config-refused"
    assert got["line"] == 8


def test_a_valueless_tag_is_the_empty_pattern(repo, stubs):
    """`exclude: !!str` is YAML for `exclude: ''`, and the empty pattern matches
    every path: pre-commit hands the hook nothing. Read as the text `!!str` -- a
    pattern matching no file -- the hook was live, and stood as coverage."""
    got = _disabled_for(repo, stubs, "        exclude: !!str\n")
    assert "gitleaks" in got["disabled"], got["disabled"]
    assert "matches every" in got["disabled"]["gitleaks"][0]


def test_an_exclude_that_leaves_files_through_is_not_a_switch(repo, stubs):
    got = _disabled_for(repo, stubs, "        exclude: '^docs/'\n")
    assert got["disabled"] == {}, got["disabled"]


def test_files_and_exclude_are_one_scope(repo, stubs):
    """pre-commit runs a hook on a path that matches `files:` and does not
    match `exclude:`. Judged apart, each half here lets something through and
    the hook reads as live; together they let nothing through, and a Mermaid
    hook in that state would have counted as covering its alternative."""
    # A second tracked file, so that neither half alone is the culprit: `files`
    # matches the README, `exclude` spares the notes, and only the pair leaves
    # nothing.
    (repo / "notes.txt").write_text("x\n")
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "notes.txt"], check=True)
    got = _disabled_for(repo, stubs, "        files: '\\.md$'\n        exclude: '\\.md$'\n")
    assert "gitleaks" in got["disabled"], got["disabled"]
    assert "together they leave no file here" in got["disabled"]["gitleaks"][0]


def test_a_scope_beyond_the_scans_reach_is_not_called_dead(repo, stubs):
    """walk_repo stops below MAX_SCAN_DEPTH and after MAX_SCAN_FILES, but the
    scope verdict rests on git's own listing, which has no depth: a hook scoped
    to `packages/app/src/generated/` in a monorepo is judged against the files
    that are really there."""
    deep = repo / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("x\n")
    got = _disabled_for(repo, stubs, "        files: '^a/b/c/d/'\n")
    assert got["disabled"] == {}, got["disabled"]


def test_the_scope_is_judged_on_the_files_git_would_hand_pre_commit(repo, stubs):
    """Tracked files, which `pre-commit run --all-files` iterates, plus the
    untracked ones git does not ignore, which the next `git add` turns into
    hook targets -- and which the scan itself just walked, so the file that got
    an entry recommended is in the listing its alternative is judged against.
    A tracked `vendor/` reaches a hook scoped to it however walk_repo prunes
    that tree; an ignored file reaches nothing."""
    (repo / "vendor").mkdir()
    (repo / "vendor" / "lib.js").write_text("x\n")
    got = _disabled_for(repo, stubs, "        files: '^vendor/'\n")
    assert got["disabled"] == {}, got["disabled"]  # untracked, not ignored: a target
    (repo / ".gitignore").write_text("*.txt\n")
    (repo / "notes.txt").write_text("x\n")
    got = _disabled_for(repo, stubs, "        files: '\\.txt$'\n")
    assert "gitleaks" in got["disabled"], got["disabled"]  # ignored: never a target
    subprocess.run([REAL_GIT, "-C", str(repo), "add", "-f", "notes.txt"], check=True)
    got = _disabled_for(repo, stubs, "        files: '\\.txt$'\n")
    assert got["disabled"] == {}, got["disabled"]  # tracked despite the ignore rule


def test_outside_a_work_tree_a_pruned_directory_makes_the_walk_incomplete(tmp_path, stubs):
    """walk_repo leaves `vendor/` out on purpose for the recommendation scan,
    and a plain directory has no tracked-file listing to fall back on -- so a
    hook scoped to `^vendor/` there is judged from a listing that cannot see
    its files. An incomplete listing claims nothing."""
    plain = tmp_path / "plain"
    (plain / "vendor").mkdir(parents=True)
    (plain / "vendor" / "a.md").write_text("# a\n")
    (plain / "README.md").write_text("# hi\n")
    (plain / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        files: '^vendor/'\n"
    )
    got = out_json(run("precommit.py", "--dir", str(plain), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]


def test_a_double_quoted_filter_is_read_as_yaml_reads_it(repo, stubs):
    """`files: "\\\\.md$"` is `\\.md$` to YAML and to pre-commit; read as two
    backslashes it was a regex for a literal backslash, matching nothing, and
    a live hook was called dead."""
    got = _disabled_for(repo, stubs, '        files: "\\\\.md$"\n')
    assert got["disabled"] == {}, got["disabled"]


def test_outside_a_work_tree_the_bounded_walk_stands_in(tmp_path, stubs):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "README.md").write_text("# hi\n")
    (plain / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        files: '\\.md$'\n"
    )
    got = out_json(run("precommit.py", "--dir", str(plain), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]
    (plain / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        files: '\\.txt$'\n"
    )
    got = out_json(run("precommit.py", "--dir", str(plain), "--recommend", stubs=stubs))
    assert "gitleaks" in got["disabled"], got["disabled"]


@pytest.mark.parametrize(
    "form", ["default_stages: [manual]\n", "default_stages:\n  - manual\n"], ids=["flow", "block"]
)
def test_default_stages_park_every_hook_that_sets_none_of_its_own(repo, stubs, form):
    """pre-commit applies `default_stages` to a hook that omits `stages:`, so
    `default_stages: [manual]` keeps an otherwise ordinary hook off the commit
    path exactly as `stages: [manual]` on the hook would -- and the verdict
    names the key that did it. A hook with its own `stages:` is unaffected."""
    (repo / ".pre-commit-config.yaml").write_text(
        form + "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "gitleaks" in got["disabled"], got["disabled"]
    assert "default_stages: [manual]" in got["disabled"]["gitleaks"][0]
    (repo / ".pre-commit-config.yaml").write_text(
        form + "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        stages: [pre-commit]\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]


def test_an_invalid_pattern_is_reported_whatever_the_scan_saw(repo, stubs):
    """The pattern's validity does not depend on the listing, so a bounded walk
    does not withhold that answer."""
    deep = repo / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("x\n")
    got = _disabled_for(repo, stubs, "        files: '('\n")
    assert "not a valid pattern" in got["disabled"]["gitleaks"][0]


def test_walk_repo_says_when_it_was_cut_short(repo, monkeypatch):
    import precommit as P

    assert P.walk_repo(str(repo)).complete is True
    (repo / "a" / "b" / "c" / "d").mkdir(parents=True)
    (repo / "a" / "b" / "c" / "d" / "deep.txt").write_text("x\n")
    listing = P.walk_repo(str(repo))
    assert listing.complete is False
    assert "a/b/c/d/deep.txt" not in listing.paths
    (repo / "a" / "b" / "c" / "d").rename(repo / "a" / "b" / "d")  # back within reach
    assert P.walk_repo(str(repo)).complete is True
    monkeypatch.setattr(P, "MAX_SCAN_FILES", 1)
    assert P.walk_repo(str(repo)).complete is False
    monkeypatch.undo()
    assert P.walk_repo(str(repo)).complete is True  # .git is pruned, and is not a target
    (repo / "node_modules").mkdir()
    assert P.walk_repo(str(repo)).complete is False  # this one may hold tracked files


def test_the_configs_own_files_filter_is_part_of_every_hooks_scope(repo, stubs):
    """A config-wide `files: '\\.py$'` keeps every Markdown hook dead while each
    looks live on its own line. The verdict names the filter that did it, by
    where it lives."""
    (repo / ".pre-commit-config.yaml").write_text(
        "files: '\\.py$'\nrepos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "gitleaks" in got["disabled"], got["disabled"]
    assert "the config's files: \\.py$ (matches no file here)" in got["disabled"]["gitleaks"][0]


@pytest.mark.parametrize(
    "top",
    ["files:\n  ^src/\n", "default_stages:\n  [manual]\n"],
    ids=["continued-filter", "continued-flow-default_stages"],
)
def test_a_config_wide_setting_continued_onto_the_next_line_still_counts(repo, stubs, top):
    """The same two settings written with their value on the following line --
    valid YAML the scanner read as empty, so a config-wide filter vanished from
    every scope and a stage default read as unset."""
    (repo / ".pre-commit-config.yaml").write_text(
        top + "repos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "gitleaks" in got["disabled"], got["disabled"]


def test_a_continued_block_scalar_filter_is_a_verdict_not_made(repo, stubs):
    """`files:\n  |\n    ^docs/` reaches the scope check as `|`, the same way
    `files: |` does, and is not judged; folded into `| ^docs/` it compiled to
    an alternation that matched every path."""
    (repo / ".pre-commit-config.yaml").write_text(
        "files:\n  |\n    ^never/\nrepos:\n  - repo: https://github.com/gitleaks/gitleaks\n"
        "    rev: v8.0.0\n    hooks:\n      - id: gitleaks\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]


def test_an_escaped_spelling_of_a_stage_is_that_stage(repo, stubs):
    """`default_stages: ["pre\\u002dcommit"]` is the commit stage to YAML and
    to pre-commit; read with the escape left in, every inheriting hook looked
    parked."""
    (repo / ".pre-commit-config.yaml").write_text(
        'default_stages: ["pre\\u002dcommit"]\nrepos:\n'
        "  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]


def test_a_config_wide_filter_that_kills_mermaid_frees_its_alternative(
    repo, keys_file, facts_path, stubs
):
    (repo / "doc.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    generate(repo, keys_file, facts_path, stubs, "mermaid")
    config = repo / ".pre-commit-config.yaml"
    config.write_text("files: '\\.py$'\n" + config.read_text())
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid" in got["disabled"], got["disabled"]
    assert "mermaid-parse" in {r["name"] for r in got["recommended"]}


@pytest.mark.parametrize("key", ["files", "exclude"])
def test_a_block_scalar_pattern_is_a_verdict_not_made(repo, stubs, key):
    """`files: |` with the regex on the lines below is the usual way to write a
    long `(?x)` pattern. The scanner reads the inline value only, so the filter
    arrives as `|` -- which compiles to an alternation of two empty patterns
    and matches everything. Judged, that called a `files: |` hook live whatever
    it said and an `exclude: |` hook dead whatever it said; a pattern not read
    is a pattern not judged."""
    got = _disabled_for(repo, stubs, f"        {key}: |\n          ^never/\n")
    assert got["disabled"] == {}, got["disabled"]


def test_a_hook_filter_continued_onto_the_next_line_is_judged_as_written(repo, stubs):
    """Inside a hook, `files:` over an indented pattern was stored as "" and
    compiled to a match-everything filter: a hook scoped to a directory that
    does not exist read as live, and one scoped to Markdown could have read as
    dead through an `exclude:` written the same way."""
    got = _disabled_for(repo, stubs, "        files:\n          ^never/\n")
    assert "gitleaks" in got["disabled"], got["disabled"]
    got = _disabled_for(repo, stubs, "        files:\n          \\.md$\n")
    assert got["disabled"] == {}, got["disabled"]


def test_a_pattern_pre_commit_would_refuse_reads_as_disabled(repo, stubs):
    """pre-commit stops loading a config whose `files:` will not compile, so the
    hook never runs -- the same answer, reached earlier and said plainly."""
    got = _disabled_for(repo, stubs, "        files: '('\n")
    assert "gitleaks" in got["disabled"], got["disabled"]
    assert "not a valid pattern" in got["disabled"]["gitleaks"][0]


def test_a_narrow_files_filter_without_always_run_is_flagged(repo, stubs):
    """`\\.txt$` matches nothing in a repository holding only a README, so the
    scope lets no file through and the hook never fires here."""
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
    """`git-ls-remote` is a bucket with nothing in it but git's own words.

    git has no machine-readable code line to classify on, so unreachable host,
    TLS, credentials and a repository that is not there all arrive under this
    one cause -- and SKILL.md's answer is to relay `detail` verbatim rather than
    guess from the wording. That only works if `detail` is actually populated,
    which is what this pins.
    """
    fake = tmp_path / "badremote"
    fake.mkdir()
    g = fake / "git"
    g.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "ls-remote" ]; then\n'
        '    echo "fatal: unreachable: https://github.com/pre-commit/pre-commit-hooks.git" >&2\n'
        "    exit 128\n"
        "  fi\n"
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    g.chmod(0o755)
    proc = generate(repo, keys_file, facts_path, fake, "hygiene")
    assert proc.returncode == 6
    assert "ls-remote failed" in proc.stderr
    got = out_json(proc)
    assert got["source"] == "git"
    assert got["cause"] == "git-ls-remote"
    assert "unreachable" in got["detail"], "git's own message is the whole of what is known"
    # And the repository path survives. npm's error text has its paths stripped
    # because a registry can hide a key in one; a git URL's path is the
    # repository's identity, tokens go in its userinfo instead, and blanking it
    # would leave "could not read from https://github.com/***".
    assert "pre-commit-hooks" in got["detail"]
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


def _declared_tuple(name: str) -> list[str]:
    """A module-level tuple-of-strings literal, read out of the script itself."""
    tree = ast.parse((SKILL / "scripts" / "precommit.py").read_text(encoding="utf-8"))
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Tuple):
            continue
        if any(getattr(t, "id", "") == name for t in stmt.targets):
            return [el.value for el in stmt.value.elts]
    raise AssertionError(f"{name} is not a plain tuple literal any more")


@pytest.mark.parametrize("name,least", [("PIN_CAUSES", 10), ("PIN_FIELDS", 6)])
def test_every_part_of_the_pin_failure_contract_is_named_in_SKILL_md(name, least):
    """The taxonomy and the procedure are one contract in two files.

    A cause with no sentence beside it fails the way a renamed sentinel does:
    the agent falls through to wording that does not fit and nothing about the
    run looks wrong. A *field* with no sentence is quieter still -- it is simply
    never read, so the distinction it was added to draw is not drawn. `registry`
    exists because a 404 from a company mirror is not a 404 from npmjs, and it
    would have been worth nothing unmentioned.

    Read out of the source rather than restated here, so adding one and
    forgetting the other is red instead of a third list to keep in step.
    """
    declared = _declared_tuple(name)
    assert len(declared) >= least
    advice = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    missing = [d for d in declared if f"`{d}`" not in advice]
    assert not missing, f"{name} entries the procedure says nothing about: {missing}"


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


# -- mermaid-parse: the browser-free sibling -----------------------------------


def test_generate_pins_every_package_mermaid_parse_needs(repo, keys_file, facts_path, stubs):
    """Two npm pins for one entry, each asked for by name, both recorded.

    A bare version cannot say which of two packages it belongs to, so an entry
    that pins several records `name@version` pairs; an entry that pins one keeps
    the bare version the summary has always shown.
    """
    proc = generate(repo, keys_file, facts_path, stubs, "mermaid-parse")
    assert proc.returncode == 0, proc.stderr
    text = (repo / ".pre-commit-config.yaml").read_text()
    assert f'"mermaid@{NPM_VERSION}"' in text
    assert f'"linkedom@{NPM_VERSION}"' in text
    assert "__NPM" not in text
    calls = stub_calls(stubs)
    assert "npm view mermaid@latest" in calls
    assert "npm view linkedom@latest" in calls
    assert out_json(proc)["versions"]["mermaid-parse"] == (
        f"mermaid@{NPM_VERSION} linkedom@{NPM_VERSION}"
    )
    assert json.loads(facts_path.read_text())["hooks"]["versions"]["mermaid-parse"] == (
        f"mermaid@{NPM_VERSION} linkedom@{NPM_VERSION}"
    )


def test_mermaid_parse_writes_its_own_asset_and_only_that(repo, keys_file, facts_path, stubs):
    generate(repo, keys_file, facts_path, stubs, "mermaid-parse")
    shipped = (SKILL / "assets" / "parse-mermaid.mjs").read_bytes()
    assert (repo / "scripts" / "parse-mermaid.mjs").read_bytes() == shipped
    assert not (repo / "scripts" / "lint-mermaid.mjs").exists()
    facts = json.loads(facts_path.read_text())
    assert set(facts["files"]["written"]) == {
        ".pre-commit-config.yaml",
        "scripts/parse-mermaid.mjs",
    }


def test_a_foreign_parse_mermaid_script_stops_the_write(repo, keys_file, facts_path, stubs):
    """The same guard lint-mermaid.mjs has: the config would EXECUTE this file."""
    (repo / "scripts").mkdir()
    (repo / "scripts" / "parse-mermaid.mjs").write_text("// not ours\n")
    proc = generate(repo, keys_file, facts_path, stubs, "mermaid-parse")
    assert proc.returncode != 0
    assert "NOT the file this skill ships" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists()
    assert (repo / "scripts" / "parse-mermaid.mjs").read_text() == "// not ours\n"


def test_the_scan_recommends_the_check_that_needs_no_browser(repo, stubs):
    """A pre-commit hook is a syntax check first. The renderer stays in the
    catalog for whoever wants it, asked for by name."""
    (repo / "doc.md").write_text("# hi\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    names = [r["name"] for r in got["recommended"]]
    assert "mermaid-parse" in names
    assert "mermaid" not in names
    assert "mermaid-parse" in got["proposed"]
    assert "mermaid fence (doc.md)" in got["detected"]


@pytest.mark.parametrize(
    "present,absent", [("mermaid", "mermaid-parse"), ("mermaid-parse", "mermaid")]
)
def test_neither_mermaid_entry_is_offered_beside_the_other(
    repo, keys_file, facts_path, stubs, present, absent
):
    """They check the same fences. Offering the second beside a LIVE first
    reads as a gap in coverage that does not exist -- in either direction."""
    (repo / "doc.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    generate(repo, keys_file, facts_path, stubs, present)
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert present in got["previous"]
    names = {r["name"] for r in got["recommended"]}
    assert absent not in names
    assert present not in names
    # The fence itself is still reported: the scan saw it, whatever is installed.
    assert "mermaid fence (doc.md)" in got["detected"]


def test_a_disabled_alternative_does_not_hide_the_recommendation(repo, stubs):
    """A `mermaid` kept on `stages: [manual]` -- the ordinary way to park a
    check whose browser is too slow for every commit -- is exactly the config
    that wants the browser-free one. The alternative has its own hook id, so it
    is the one repair this run can make; suppressing it would leave the user
    with no working Mermaid check and a report saying one is present."""
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
        "        stages: [manual]\n"
    )
    (repo / "doc.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid" in got["previous"]
    assert "mermaid" in got["disabled"]
    assert "mermaid-parse" in {r["name"] for r in got["recommended"]}
    assert "mermaid-parse" in got["proposed"]


def test_a_dead_mermaid_parse_gets_the_renderer_offered_in_its_place(
    repo, keys_file, facts_path, stubs
):
    """The mirror of the case above: the scan names `mermaid-parse`, it is
    present but parked on `stages: [manual]`, and selecting it again would write
    nothing. Its live alternative is offered instead, with the same reason."""
    (repo / "doc.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    generate(repo, keys_file, facts_path, stubs, "mermaid-parse")
    config = repo / ".pre-commit-config.yaml"
    config.write_text(
        config.read_text().replace(
            "      - id: mermaid-parse\n", "      - id: mermaid-parse\n        stages: [manual]\n"
        )
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid-parse" in got["disabled"], got["disabled"]
    offered = {r["name"]: r["reason"] for r in got["recommended"]}
    assert "mermaid-parse" not in offered
    assert offered["mermaid"] == "doc.md"
    assert "mermaid" in got["proposed"]


def test_a_scope_is_judged_among_the_files_the_entry_is_for(repo, stubs):
    """A mermaid hook behind `files: '\\.py$'` admits every Python file in a
    mixed repository and read as live, while no Markdown file could reach it --
    the diagram the scan found went unchecked, and the alternative that would
    check it was withheld. Judged among the Markdown files, which is what the
    entry is for, it is dead; a gitleaks hook behind the same filter is for
    every file, and stays live."""
    (repo / "main.py").write_text("x = 1\n")
    (repo / "doc.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
        "        files: '\\.py$'\n"
        "  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        files: '\\.py$'\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert set(got["disabled"]) == {"mermaid"}, got["disabled"]
    assert "matches none of the files this entry is for" in got["disabled"]["mermaid"][0]
    assert "mermaid-parse" in {r["name"] for r in got["recommended"]}


def test_always_run_is_not_coverage_for_a_hook_that_consumes_filenames(repo, stubs):
    """pre-commit runs an `always_run` hook whatever its scope admits -- with
    the files it admits, which may be none. Both mermaid scripts exit 0 on an
    empty argv, so that is a run over nothing. gitleaks ignores its file list
    (upstream sets `pass_filenames: false`) and is genuinely live."""
    (repo / "doc.md").write_text("# hi\n")
    hook = (
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
        "        files: '^never/'\n        always_run: true\n"
    )
    (repo / ".pre-commit-config.yaml").write_text(hook)
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid" in got["disabled"], got["disabled"]


def test_pass_filenames_false_on_a_hook_that_reads_its_file_list_is_a_run_over_nothing(repo, stubs):
    """What the program does is the catalog's knowledge: both mermaid scripts
    read argv, so a hook that hands them none -- whatever its scope, and
    whether or not it is `always_run` -- checks nothing. gitleaks never reads
    the list, so the same setting on it changes nothing."""
    (repo / "doc.md").write_text("# hi\n")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-parse\n        name: mermaid-parse\n"
        "        entry: node scripts/parse-mermaid.mjs\n        language: node\n"
        "        files: '(?i)\\.(md|markdown)$'\n        pass_filenames: false\n"
        "  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n        pass_filenames: false\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert set(got["disabled"]) == {"mermaid-parse"}, got["disabled"]
    assert "pass_filenames: false" in got["disabled"]["mermaid-parse"][0]


def test_an_explicitly_empty_exclude_is_a_pattern_that_matches_everything(repo, stubs):
    """`exclude: ''` compiles to the empty regex, which matches every path, so
    pre-commit hands the hook no files. Read by truthiness it was "unset", and a
    hook it had emptied read as live. Both the hook's own and the config's."""
    got = _disabled_for(repo, stubs, "        exclude: ''\n")
    assert "gitleaks" in got["disabled"], got["disabled"]
    assert "exclude: '' (matches every file here)" in got["disabled"]["gitleaks"][0]
    (repo / ".pre-commit-config.yaml").write_text(
        "exclude: ''\nrepos:\n  - repo: https://github.com/gitleaks/gitleaks\n    rev: v8.0.0\n"
        "    hooks:\n      - id: gitleaks\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "the config's exclude: ''" in got["disabled"]["gitleaks"][0]


def test_a_hook_id_declared_twice_is_covered_while_one_declaration_is_live(repo, stubs):
    """Two `mermaid-lint` declarations, one parked on `stages: [manual]` and one
    ordinary: the live one checks the diagrams, so the key is covered and the
    alternative is not offered. A hygiene entry with only `check-json` parked
    still reports that one, since no declaration of *that* id is live."""
    (repo / "doc.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    hook = (
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
    )
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n"
        + hook
        + "        stages: [manual]\n"
        + hook
        + "  - repo: https://github.com/pre-commit/pre-commit-hooks\n    rev: v6.0.0\n"
        "    hooks:\n      - id: trailing-whitespace\n"
        "      - id: check-json\n        stages: [manual]\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid" not in got["disabled"], got["disabled"]
    assert "mermaid-parse" not in {r["name"] for r in got["recommended"]}
    assert got["disabled"]["hygiene"] == ["check-json (stages: [manual])"]


def test_a_live_alternative_stands_in_only_where_it_reaches_the_fence(repo, stubs):
    """A `mermaid` scoped to `^docs/` is live for `docs/a.md` and never sees the
    `README.md` whose fence got `mermaid-parse` recommended. Live is not the
    same as checking that file: the recommendation stands, with that file as
    its reason. Widen the scope to every Markdown file and it is stood in for."""
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("# a\n")
    (repo / "README.md").write_text("# hi\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    hook = (
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
    )
    (repo / ".pre-commit-config.yaml").write_text(hook + "        files: '^docs/'\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]  # live: docs/a.md reaches it
    offered = {r["name"]: r["reason"] for r in got["recommended"]}
    assert offered.get("mermaid-parse") == "README.md"
    (repo / ".pre-commit-config.yaml").write_text(hook + "        files: '\\.md$'\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid-parse" not in {r["name"] for r in got["recommended"]}


def test_an_unread_filter_on_the_alternative_is_no_evidence_it_covers_the_fence(repo, stubs):
    """`files: |-` with the pattern below is a filter the scanner captured but
    did not read. It is not held against the hook (not dead) and not counted for
    it either (not covering), so the recommendation stands -- being told the
    check is already there, by a hook whose scope nobody read, is the report
    this feature exists to prevent."""
    (repo / "README.md").write_text("# hi\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
        "        files: |-\n          ^docs/\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]
    assert "mermaid-parse" in {r["name"] for r in got["recommended"]}


def test_coverage_is_judged_on_the_path_as_git_names_it(repo, stubs):
    """`reason` is the trigger path cleaned for display, and `clean` strips
    whitespace. A file named ` README.md` (leading space) holds the fence; a
    `mermaid` scoped to `^README` reaches `README.md` and not that file, and
    judged on the cleaned name it would have seemed to."""
    (repo / " README.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
        "        files: '^README'\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]  # live: README.md reaches it
    assert "mermaid-parse" in {r["name"] for r in got["recommended"]}


def test_a_live_alternative_must_reach_every_fence_file_to_stand_in(repo, stubs):
    """The probe records every Markdown file with a fence, not only the first
    it meets. A renderer scoped to `^a/` covers `a/covered.md` and never sees
    `z/uncovered.md`, so the recommendation stands; scoped to every Markdown
    file, it is stood in for."""
    (repo / "a").mkdir()
    (repo / "z").mkdir()
    (repo / "a" / "covered.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    (repo / "z" / "uncovered.md").write_text("```mermaid\ngraph TD;\nC-->D;\n```\n")
    hook = (
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
    )
    (repo / ".pre-commit-config.yaml").write_text(hook + "        files: '^a/'\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]
    offered = {r["name"]: r["reason"] for r in got["recommended"]}
    assert offered.get("mermaid-parse") == "a/covered.md"  # the first file, as always
    assert got["detected_paths"] == ["README.md", "a/covered.md"]  # unchanged: one trigger
    (repo / ".pre-commit-config.yaml").write_text(hook + "        files: '\\.md$'\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid-parse" not in {r["name"] for r in got["recommended"]}


def test_a_capped_fence_probe_lets_no_alternative_stand_in(repo, stubs):
    """The probe reads at most MAX_MERMAID_PROBES Markdown files. With more
    than that, fences inside the sample all under `a/` and one past the cap
    under `z/`, a renderer scoped to `^a/` covers everything the probe saw and
    nothing it did not -- so a capped look proves nothing, and the
    recommendation stands."""
    import precommit as P

    (repo / "a").mkdir()
    (repo / "z").mkdir()
    for n in range(P.MAX_MERMAID_PROBES):
        (repo / "a" / f"f{n:03d}.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    (repo / "z" / "uncovered.md").write_text("```mermaid\ngraph TD;\nC-->D;\n```\n")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
        "        files: '^a/'\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]
    assert "mermaid-parse" in {r["name"] for r in got["recommended"]}


def test_a_present_entry_that_never_reaches_the_fence_file_is_reported(repo, stubs):
    """`mermaid-parse` scoped to `^docs/` runs on `docs/a.md` and never sees
    the `README.md` the fence is in. It is live, so it was "already there" and
    nothing more; now it is reported beside the dead entries, and the live
    alternative is offered in its place. Widened to every Markdown file, it is
    simply present."""
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("# a\n")
    (repo / "README.md").write_text("# hi\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    hook = (
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-parse\n        name: mermaid-parse\n"
        "        entry: node scripts/parse-mermaid.mjs\n        language: node\n"
    )
    (repo / ".pre-commit-config.yaml").write_text(hook + "        files: '^docs/'\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid-parse" in got["disabled"], got["disabled"]
    assert "does not reach README.md" in got["disabled"]["mermaid-parse"][0]
    offered = {r["name"]: r["reason"] for r in got["recommended"]}
    assert offered.get("mermaid") == "README.md"
    (repo / ".pre-commit-config.yaml").write_text(hook + "        files: '\\.md$'\n")
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert got["disabled"] == {}, got["disabled"]
    assert "mermaid" not in {r["name"] for r in got["recommended"]}


def test_a_markdown_file_the_probe_could_not_read_makes_its_look_incomplete(repo, stubs):
    """A fence under `a/` was seen; a Markdown file too large for the probe was
    not, and may hold the fence a renderer scoped to `^a/` does not reach. An
    incomplete look lets no alternative stand in."""
    import precommit as P

    (repo / "a").mkdir()
    (repo / "a" / "covered.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    (repo / "z-large.md").write_text("x" * (P.MAX_PROBE_BYTES + 1) + "\n")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
        "        files: '^a/'\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid-parse" in {r["name"] for r in got["recommended"]}


def test_a_walk_cut_short_by_its_bounds_taints_the_fence_probe(repo, stubs):
    """A fence under `a/` was seen; a fence four directories deep was beyond
    MAX_SCAN_DEPTH and not. The walk says it was cut short, so the probe's look
    is not complete and a renderer scoped to `^a/` does not stand in. A pruned
    `node_modules/` is policy, not a size cap, and does not taint it."""
    import precommit as P

    (repo / "a").mkdir()
    (repo / "a" / "covered.md").write_text("```mermaid\ngraph TD;\nA-->B;\n```\n")
    deep = repo / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "uncovered.md").write_text("```mermaid\ngraph TD;\nC-->D;\n```\n")
    hook = (
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-lint\n        name: mermaid-lint\n"
        "        entry: node scripts/lint-mermaid.mjs\n        language: node\n"
        "        files: '^a/'\n"
    )
    (repo / ".pre-commit-config.yaml").write_text(hook)
    assert P.walk_repo(str(repo)).bounded is True
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid-parse" in {r["name"] for r in got["recommended"]}
    # Policy, not a bound: with the deep tree gone and a node_modules/ present,
    # the walk is incomplete but not bounded, and the alternative stands in.
    shutil.rmtree(repo / "b")
    (repo / "node_modules").mkdir()
    listing = P.walk_repo(str(repo))
    assert listing.complete is False and listing.bounded is False
    (repo / ".pre-commit-config.yaml").write_text(hook.replace("'^a/'", "'\\.md$'"))
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid-parse" not in {r["name"] for r in got["recommended"]}


def test_an_observed_gap_is_reported_even_when_the_probe_was_capped(repo, stubs):
    """A fence in `README.md` that a present `mermaid-parse` scoped to `^docs/`
    never reaches is a gap the probe saw. More Markdown behind the cap does not
    unsee it: completeness decides whether coverage may be CLAIMED, not whether
    an observed gap is reported."""
    import precommit as P

    (repo / "docs").mkdir()
    for n in range(P.MAX_MERMAID_PROBES):
        (repo / "docs" / f"d{n:03d}.md").write_text("# d\n")
    (repo / "README.md").write_text("# hi\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-parse\n        name: mermaid-parse\n"
        "        entry: node scripts/parse-mermaid.mjs\n        language: node\n"
        "        files: '^docs/'\n"
    )
    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    assert "mermaid-parse" in got["disabled"], got["disabled"]
    assert "does not reach README.md" in got["disabled"]["mermaid-parse"][0]
    assert "mermaid" in {r["name"] for r in got["recommended"]}


def test_a_config_yaml_would_not_load_is_refused_not_judged(repo, stubs):
    """`files: "\\.md$"` is a regex written as if the quotes were single; YAML
    has no `\\.` escape and pre-commit stops at "found unknown escape
    character", so the file runs no hook. The scan read it as the regex meant,
    called the entry live, and let it stand in for the working alternative. It
    is a refusal now, with the line, like every other shape YAML would not
    load -- including a never-closed quote inside a flow item, which used to
    pass the scan whole and escape as a traceback when the stages were read."""
    (repo / "README.md").write_text("# hi\n\n```mermaid\ngraph TD;\nA-->B;\n```\n")
    head = (
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: mermaid-parse\n        name: mermaid-parse\n"
        "        entry: node scripts/parse-mermaid.mjs\n        language: node\n"
    )
    for tail in ('        files: "\\.md$"\n', '        stages: ["pre-commit]\n'):
        (repo / ".pre-commit-config.yaml").write_text(head + tail)
        proc = run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs)
        assert proc.returncode == 5, proc.stderr
        assert "Traceback" not in proc.stderr
        got = out_json(proc)
        assert got["reason"] == "config-refused"
        assert got["line"] == 8


def test_the_alternatives_point_at_each_other(stubs):
    import precommit as P

    for key, meta in P.CATALOG.items():
        for other in meta.get("alternatives", ()):
            assert key in P.CATALOG[other].get("alternatives", ()), (
                f"{key} names {other} as an alternative, and {other} does not name it back"
            )
    assert P.CATALOG["mermaid-parse"]["alternatives"] == ("mermaid",)


def test_both_mermaid_entries_can_share_one_config(repo, keys_file, facts_path, stubs):
    """Alternatives, not exclusives: asked for by name, the second is inserted
    as its own local block, and both are then present and both assets are on
    disk."""
    generate(repo, keys_file, facts_path, stubs, "mermaid")
    proc = generate(repo, keys_file, facts_path, stubs, "mermaid-parse", force=True)
    assert proc.returncode == 0, proc.stderr
    got = out_json(run("precommit.py", "--dir", str(repo), "--detect", stubs=stubs))
    assert {"mermaid", "mermaid-parse"} <= set(got["present"])
    local_ids = {h for r in got["repos"] if r["repo"] == "local" for h in r["hooks"]}
    assert {"mermaid-lint", "mermaid-parse"} <= local_ids
    assert (repo / "scripts" / "lint-mermaid.mjs").exists()
    assert (repo / "scripts" / "parse-mermaid.mjs").exists()


def test_prerequisites_are_reported_per_npm_backed_entry(repo, stubs):
    """SKILL.md reads `prerequisites.<key>` for whichever entry is offered, so
    every entry that pins an npm package has a row -- and they share one answer,
    because they share the two binaries."""
    import precommit as P

    got = out_json(run("precommit.py", "--dir", str(repo), "--recommend", stubs=stubs))
    npm_backed = {k for k, m in P.CATALOG.items() if m.get("npm")}
    assert set(got["prerequisites"]) == npm_backed == {"mermaid", "mermaid-parse"}
    assert len(set(got["prerequisites"].values())) == 1


def test_an_unfilled_placeholder_in_any_spelling_stops_the_run(
    repo, keys_file, facts_path, stubs, skill_copy
):
    """`__NPM__` was the only npm spelling the old check knew, and a fragment
    pinning two packages carries two others. A token the catalog does not name
    has to be caught by shape, before anything is written."""
    fragment = skill_copy / "templates" / "mermaid-parse.yaml"
    fragment.write_text(fragment.read_text().replace("__NPM_LINKEDOM__", "__NPM_LINKEDOM_2__"))
    proc = generate(
        repo, keys_file, facts_path, stubs, "mermaid-parse", scripts=skill_copy / "scripts"
    )
    assert proc.returncode != 0
    assert "unfilled placeholder" in proc.stderr
    assert not (repo / ".pre-commit-config.yaml").exists()


@pytest.mark.parametrize("key", ["mermaid-parse", "mermaid"])
def test_the_mermaid_hooks_take_uppercase_markdown_extensions_like_the_scan_does(key):
    """detect_markers lowercases the name before it looks for `.md`, so a
    `README.MD` gets the entry recommended. The hook's own `files:` has to
    reach that same file, or the recommendation installs a check that never
    sees the file that triggered it."""
    import config as C
    import precommit as P

    text = (SKILL / "templates" / P.CATALOG[key]["fragment"]).read_text(encoding="utf-8")
    for placeholder in P.CATALOG[key]["npm"]:
        text = text.replace(placeholder, "0.0.0")
    (hook,) = C.scan("repos:\n" + text).repos[0].hooks
    pattern = re.compile(hook.settings["files"])
    assert pattern.search("docs/README.MD")
    assert pattern.search("notes.Markdown")
    assert not pattern.search("script.mdx")


def test_this_repository_runs_every_local_hook_it_ships(stubs):
    """The config in this checkout is managed by this tool, and CI runs it.

    Every local hook the catalog can write is in it, wired to a symlink into
    the very asset it ships -- so the copy that runs here and the payload that
    goes into other repositories cannot drift apart. A hook that is only ever
    run in other people's repositories is one nobody here would see break.
    """
    import precommit as P

    root = SKILL.parents[2]
    config = (root / ".pre-commit-config.yaml").read_text()
    for key, meta in P.CATALOG.items():
        if not meta.get("local_hook_id"):
            continue
        assert f"- id: {meta['local_hook_id']}" in config, f"{key} is not dogfooded"
        for src, rel in meta["assets"]:
            link = root / rel
            assert link.is_symlink(), f"{rel} is not a symlink"
            assert link.resolve() == (SKILL / "assets" / src).resolve(), rel
