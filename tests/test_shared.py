"""The helpers every script depends on, plus the rules that keep them portable."""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

import shared

SCRIPTS = Path(shared.__file__).resolve().parent
STDLIB = set(sys.stdlib_module_names)
OWN = {"shared", "config", "precommit", "gitwork", "summary"}


# -- the portability rules ---------------------------------------------------


def test_the_scripts_import_nothing_but_the_standard_library_and_each_other():
    """The skill installs as a bare symlink and runs under the user's system
    python3, so nothing would ever install a dependency on its behalf. An
    import outside this set is a runtime failure on someone else's machine.

    This is what replacing ruamel.yaml bought, and this test is what stops it
    coming back.
    """
    offenders = {}
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name and name not in STDLIB and name not in OWN:
                    offenders.setdefault(path.name, set()).add(name)
    assert not offenders, f"non-stdlib imports: {offenders}"


def test_no_script_imports_the_installer_package():
    """The scripts must work from the symlink alone, with nothing importable.

    Checked against the parsed imports rather than the raw text: the word
    appears in a docstring explaining exactly this rule, and a test that cannot
    tell an explanation from an import is a test that will be worked around.
    """
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not a.name.startswith("manage_precommit") for a in node.names), path.name
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("manage_precommit"), path.name


def test_the_source_holds_no_invisible_characters():
    """The character classes are built from integer ranges precisely so the
    bytes they catch never appear as literals in this file, where a colorising
    shell or a careless merge could mangle them unnoticed."""
    for path in sorted(SCRIPTS.glob("*.py")):
        raw = path.read_bytes()
        bad = [(i, b) for i, b in enumerate(raw) if b > 126 or (b < 32 and b not in (9, 10, 13))]
        assert not bad, f"{path.name} holds non-printable bytes at {bad[:5]}"


# -- neutralising repo-derived text ------------------------------------------


# Built from codepoints rather than written as literals, for the same reason
# shared.py builds its character classes that way: a literal here would put the
# very bytes under test into this file, where they are invisible to review and
# survivable by a careless merge.
FORGERY_CODEPOINTS = [
    (0x001B, "ESC"),
    (0x009B, "C1-CSI-one-codepoint-so-stripping-ESC-is-not-enough"),
    (0x0000, "NUL"),
    (0x202E, "right-to-left-override"),
    (0x200B, "zero-width-space"),
    (0xFEFF, "BOM"),
    (0x2028, "line-separator-splitlines-breaks-on-it"),
    (0xE0041, "unicode-tag-block"),
    (0xFE0F, "variation-selector"),
]


@pytest.mark.parametrize(
    "codepoint", [c for c, _ in FORGERY_CODEPOINTS], ids=[n for _, n in FORGERY_CODEPOINTS]
)
def test_clean_neutralises_everything_that_could_forge_a_row(codepoint):
    text = "a" + chr(codepoint) + "b"
    assert shared.clean(text) == "a b"
    assert shared.has_suspicious_chars(text) is True


@pytest.mark.parametrize("text", ["a\nb", "a\tb", "a\rb"])
def test_ordinary_whitespace_is_not_suspicious_in_file_content(text):
    """A newline inside a summary *field* forges a row, so `clean` removes it;
    a newline inside a *file* is a line ending, so the scanner must not flag it."""
    assert shared.has_suspicious_chars(text) is False
    assert shared.clean(text) == "a b"


def test_clean_stringifies_non_strings():
    assert shared.clean(7) == "7"
    assert shared.clean(None) == "None"


def test_refuse_option_like_rejects_a_leading_dash():
    calls = []

    def die(msg):
        calls.append(msg)
        raise SystemExit(1)

    assert shared.refuse_option_like("origin", "remote", die) == "origin"
    with pytest.raises(SystemExit):
        shared.refuse_option_like("--upload-pack=evil", "remote", die)
    assert "looks like an option" in calls[0]


# -- reading files safely ----------------------------------------------------


def test_read_refuses_to_follow_a_symlink(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("id_rsa\n")
    link = tmp_path / "link"
    link.symlink_to(secret)
    with pytest.raises(shared.SymlinkRefused):
        shared.read_bytes_nofollow(str(link))


def test_read_refuses_a_directory(tmp_path):
    with pytest.raises(shared.NotARegularFile):
        shared.read_bytes_nofollow(str(tmp_path))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_read_refuses_a_fifo_instead_of_hanging(tmp_path):
    """Without O_NONBLOCK this blocks forever with no writer, which reads as a
    slow run rather than a failure."""
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(shared.NotARegularFile):
        shared.read_bytes_nofollow(str(fifo))


def test_read_is_bounded(tmp_path):
    big = tmp_path / "big"
    big.write_bytes(b"x" * 100)
    with pytest.raises(shared.TooLarge):
        shared.read_bytes_nofollow(str(big), max_bytes=10)


# -- writing files safely ----------------------------------------------------


def test_atomic_write_replaces_a_symlink_rather_than_writing_through_it(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("untouched\n")
    link = tmp_path / "link"
    link.symlink_to(victim)
    shared.atomic_write_bytes(str(link), b"new\n")
    assert victim.read_text() == "untouched\n"
    assert link.read_text() == "new\n"
    assert not link.is_symlink()


def test_atomic_write_leaves_no_temp_file_behind_on_failure(tmp_path):
    target = tmp_path / "t"
    with pytest.raises(TypeError):
        shared.atomic_write_bytes(str(target), "not bytes")  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


def test_write_json_keeps_the_files_permissions(tmp_path):
    p = tmp_path / "facts.json"
    p.write_text("{}")
    p.chmod(0o640)
    shared.write_json(str(p), {"a": 1})
    assert (p.stat().st_mode & 0o777) == 0o640


def test_refuse_facts_inside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def die(msg):
        raise SystemExit(msg)

    shared.refuse_facts_inside_repo(str(repo), str(tmp_path / "outside.json"), die)
    with pytest.raises(SystemExit, match="outside the repository"):
        shared.refuse_facts_inside_repo(str(repo), str(repo / "facts.json"), die)
    with pytest.raises(SystemExit):
        shared.refuse_facts_inside_repo(str(repo), str(repo / "a" / "b" / "facts.json"), die)


# -- the git hardening is the security boundary, so it is tested as one -------


def _git_for_test(tmp_path):
    """A make_git bound to a die() that raises instead of exiting."""

    def die(msg):
        raise AssertionError(f"die: {msg}")

    return shared.make_git(die)


def test_ext_remotes_cannot_execute_a_command(tmp_path):
    """`ext::` remotes run a command named in repository config -- code
    execution from a checked-out repo.

    Current git refuses ext:: by default, which makes the obvious version of
    this test unfalsifiable: it passes whether or not our flag is present. So
    the repo here does what a hostile checkout would do and re-enables the
    transport in its OWN config. The control below proves that is a live
    attack; this proves our command-line override beats it, which is the only
    thing worth asserting.
    """
    import subprocess as sp

    repo = tmp_path / "hostile"
    repo.mkdir()
    sp.run(["git", "init", "-q", str(repo)], check=True)
    sp.run(["git", "-C", str(repo), "config", "protocol.ext.allow", "always"], check=True)

    marker = tmp_path / "pwned"
    control = tmp_path / "pwned-control"

    # Control: without the hardening the command really does run.
    sp.run(
        ["git", "-C", str(repo), "fetch", f"ext::sh -c touch% {control}"],
        capture_output=True,
    )
    assert control.exists(), (
        "the control did not fire -- this git refuses ext:: even with repo config "
        "enabling it, so the assertion below would prove nothing"
    )

    rc, _, _ = _git_for_test(tmp_path)(str(repo), "fetch", f"ext::sh -c touch% {marker}")
    assert rc != 0, "the ext:: remote was accepted"
    assert not marker.exists(), "an ext:: remote executed a command"


def test_remote_stderr_is_truncated_before_it_is_stored(tmp_path, monkeypatch):
    """git's stderr can carry arbitrary text straight from a remote server."""
    fake = tmp_path / "bin"
    fake.mkdir()
    g = fake / "git"
    g.write_text("#!/bin/sh\npython3 -c \"import sys; sys.stderr.write('x'*900)\"\nexit 1\n")
    g.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")

    git = _git_for_test(tmp_path)
    _, _, err = git(str(tmp_path), "status")
    assert err.endswith("...(truncated)")
    assert len(err) <= shared.MAX_ERR_LEN + len(" ...(truncated)")


def test_a_credential_prompt_is_never_waited_on(tmp_path):
    """GIT_TERMINAL_PROMPT=0: a prompt would hang a headless run forever."""
    import subprocess as sp

    repo = tmp_path / "r2"
    repo.mkdir()
    sp.run(["git", "init", "-q", str(repo)], check=True)
    git = _git_for_test(tmp_path)
    # An https remote with no credentials available: must fail, not block.
    rc, _, _ = git(str(repo), "ls-remote", "https://localhost:1/nonexistent.git")
    assert rc != 0


def test_isolated_lookups_ignore_repository_and_global_config(tmp_path):
    """A checked-out repo can ship a .git/config that redirects any URL via
    url.<base>.insteadOf -- and the catalog lookup is about a hardcoded
    upstream, so it must not run under config that repository controls.

    Tested against real git, because the rewrite happens *inside* git: a stub
    only ever sees the argv it was handed, which is the URL before rewriting,
    so a stub-based version of this test could not fail.
    """
    import subprocess as sp

    upstream = tmp_path / "upstream.git"
    sp.run(["git", "init", "-q", "--bare", str(upstream)], check=True)

    hostile = tmp_path / "hostile"
    hostile.mkdir()
    sp.run(["git", "init", "-q", str(hostile)], check=True)
    sp.run(
        ["git", "-C", str(hostile), "config", f"url.{tmp_path}/nowhere-.insteadOf", f"{tmp_path}/"],
        check=True,
    )

    git = _git_for_test(tmp_path)

    # Control: run under the hostile repo's config and the URL is rewritten to
    # somewhere that does not exist, so the lookup fails.
    rc_plain, _, _ = git(str(hostile), "ls-remote", "--tags", str(upstream))
    assert rc_plain != 0, (
        "the control did not fire -- this git ignored url.insteadOf, so the "
        "assertion below would prove nothing"
    )

    # Isolated: no system config, no global config, cwd outside any repository.
    rc_iso, _, _ = git(str(tmp_path), "ls-remote", "--tags", str(upstream), isolated=True)
    assert rc_iso == 0, "the isolated lookup was still redirected"


def test_isolated_lookups_ignore_a_hostile_global_config(tmp_path, monkeypatch):
    """Running outside the repository defeats a redirect in *its* config; it
    does nothing about one in the user's global config, which a compromised
    dotfile or a shared machine can supply. That is what GIT_CONFIG_NOSYSTEM
    and GIT_CONFIG_GLOBAL are for, and this is the half that exercises them.
    """
    import subprocess as sp

    upstream = tmp_path / "upstream2.git"
    sp.run(["git", "init", "-q", "--bare", str(upstream)], check=True)

    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f'[url "{tmp_path}/nowhere-"]\n\tinsteadOf = {tmp_path}/\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))

    git = _git_for_test(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    # Control: outside any repo, but the global config still redirects.
    rc_plain, _, _ = git(str(outside), "ls-remote", "--tags", str(upstream))
    assert rc_plain != 0, (
        "the control did not fire -- this git ignored the global insteadOf, so "
        "the assertion below would prove nothing"
    )

    rc_iso, _, _ = git(str(outside), "ls-remote", "--tags", str(upstream), isolated=True)
    assert rc_iso == 0, "the isolated lookup still read the global config"


def test_is_work_tree_rejects_a_git_directory(tmp_path):
    """`rev-parse --is-inside-work-tree` exits 0 and prints "false" inside a
    .git directory, so an rc-only check calls that a repository -- and two
    scripts had independently drifted into exactly that variant."""
    import subprocess as sp

    repo = tmp_path / "wt"
    repo.mkdir()
    sp.run(["git", "init", "-q", str(repo)], check=True)
    git = _git_for_test(tmp_path)

    assert shared.is_work_tree(git, str(repo)) is True
    assert shared.is_work_tree(git, str(repo / ".git")) is False
    assert shared.is_work_tree(git, str(tmp_path / "not-a-repo-at-all")) is False


def test_a_quoted_porcelain_path_is_decoded():
    """git C-quotes any path with a non-ASCII byte, so the raw field is a
    quoted string of octal escapes -- a name the user cannot find on disk."""
    accented = "caf" + chr(0xE9) + ".md"
    quoted = '"caf\\303\\251.md"'
    assert shared.porcelain_path(" M " + quoted) == accented


def test_an_ordinary_porcelain_path_is_untouched():
    assert shared.porcelain_path(" M README.md") == "README.md"
    assert shared.porcelain_path("?? a/b/c.txt") == "a/b/c.txt"


# -- round 15 ----------------------------------------------------------------


def test_core_quotePath_is_forced(monkeypatch):
    """git's own default, but repo-local and therefore attacker-settable. With
    it off git prints the raw bytes of a filename -- control characters, bidi
    overrides -- into output this tool turns into a summary the user acts on."""
    seen = {}

    class Result:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        return Result()

    monkeypatch.setattr(shared.subprocess, "run", fake_run)
    git = shared.make_git(lambda m: pytest.fail(m))
    git("/tmp", "status")
    assert "core.quotePath=true" in seen["argv"]
    # The rest of the hardening is still there -- this test must not become the
    # reason a later edit drops one of the others.
    for flag in ("protocol.ext.allow=never", "protocol.file.allow=user", "core.fsmonitor="):
        assert flag in seen["argv"]


def test_safe_porcelain_refuses_rather_than_reporting_clean():
    """Empty output means "nothing changed", so a swallowed failure reads as a
    clean tree -- which variously discards this run's work, merges into a user's
    edit, or drops the autofix disclosure."""
    calls = []

    def failing_git(repo, *args, **kwargs):
        calls.append(args)
        return 128, "", "fatal: index file corrupt"

    class Stop(Exception):
        pass

    def die(msg):
        raise Stop(msg)

    with pytest.raises(Stop) as caught:
        shared.safe_porcelain(failing_git, "/tmp", ["a.txt"], die, what="the state of things")
    message = str(caught.value)
    assert "a failed check is not a clean result" in message
    assert "the state of things" in message
    assert "index file corrupt" in message


def test_safe_porcelain_passes_paths_after_a_double_dash():
    """A path beginning with a dash would otherwise be read as an option."""
    seen = {}

    def ok_git(repo, *args, **kwargs):
        seen["args"] = args
        return 0, " M a.txt\n", ""

    out = shared.safe_porcelain(ok_git, "/tmp", ["a.txt"], lambda m: None, what="x")
    assert out == " M a.txt\n"
    assert seen["args"] == ("status", "--porcelain", "--no-renames", "--", "a.txt")


def test_safe_porcelain_omits_the_double_dash_when_scanning_everything():
    seen = {}

    def ok_git(repo, *args, **kwargs):
        seen["args"] = args
        return 0, "", ""

    shared.safe_porcelain(ok_git, "/tmp", (), lambda m: None, what="x")
    assert seen["args"] == ("status", "--porcelain", "--no-renames")
