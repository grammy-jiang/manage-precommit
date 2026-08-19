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
# Derived from the directory, not written out again: a new sibling script would
# otherwise have to be remembered here, and forgetting looks exactly like the
# third-party import this test exists to catch.
OWN = {path.stem for path in SCRIPTS.glob("*.py")}


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


# -- the failure paths ---------------------------------------------------------
#
# Every one of these is a die() nobody reaches on a good day, which is exactly
# why they are worth pinning: they run for the first time on somebody's broken
# machine, and a traceback there is worse than a sentence.


class Stop(Exception):
    """Stands in for die(), which is a NoReturn the caller supplies."""


def stopper(message):
    raise Stop(message)


def test_an_unreadable_file_stops_with_the_reason(tmp_path):
    missing = tmp_path / "nope" / "deeper.json"
    with pytest.raises(Stop, match="cannot read"):
        shared.read_bytes_or_die(str(missing), stopper)


def test_a_facts_file_that_is_not_an_object_is_refused(tmp_path):
    """json.loads happily returns a list or a string; every reader here indexes
    it by key."""
    listy = tmp_path / "facts.json"
    listy.write_text("[1, 2, 3]")
    with pytest.raises(Stop, match="must contain a JSON object"):
        shared.read_json_or_die(str(listy), stopper)


def test_a_facts_file_that_cannot_be_written_stops(tmp_path):
    unwritable = tmp_path / "no-such-dir" / "facts.json"
    with pytest.raises(Stop, match="cannot write facts file"):
        shared.write_json_or_die(str(unwritable), {"a": 1}, stopper)


def test_a_porcelain_path_that_will_not_decode_comes_back_as_it_arrived():
    """git C-quotes a non-ASCII filename as escapes, and this undoes that --
    but an escape can name a codepoint that is not a single byte, which the
    latin-1 round-trip cannot represent. The quoted body is then the only
    honest answer; guessing would invent a name the user cannot find."""
    escaped = chr(92) + "u1234"  # a literal backslash-u escape, as git would emit
    assert shared.porcelain_path(' M "' + escaped + '"') == escaped


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param(
            "https://token@registry.npmjs.org/",
            "https://***@registry.npmjs.org/",
            id="a bare token",
        ),
        pytest.param(
            "fatal: https://user:pw@github.com/x.git denied",
            "fatal: https://***@github.com/x.git denied",
            id="user and password, mid-sentence",
        ),
        pytest.param(
            "https://registry.npmjs.org/",
            "https://registry.npmjs.org/",
            id="nothing to redact",
        ),
        pytest.param("mail me at a@b.com", "mail me at a@b.com", id="an at-sign that is not a URL"),
        # An unescaped @ is legal in userinfo and parsers take the LAST one as
        # the authority delimiter, so stopping at the first leaves the tail of
        # the secret behind.
        pytest.param(
            "https://user@example.com@host/",
            "https://***@host/",
            id="an email-shaped username",
        ),
        pytest.param(
            "https://user:p@ss@host/x",
            "https://***@host/x",
            id="an at-sign inside the password",
        ),
        pytest.param(
            "https://host/path@x",
            "https://host/path@x",
            id="an at-sign in the path is not userinfo",
        ),
        # A query carries a token as easily as userinfo does, and taking it out
        # by parameter name means keeping a list of the names I have met.
        pytest.param(
            "https://registry.example/?token=sekrit",
            "https://registry.example/?***",
            id="a token in the query",
        ),
        pytest.param(
            "https://host?token=x", "https://host?***", id="a query with no path before it"
        ),
        pytest.param(
            "https://user:p@ss@host/x?k=v#f",
            "https://***@host/x?***",
            id="userinfo and query together",
        ),
        pytest.param("https://host/x#frag", "https://host/x#***", id="a fragment"),
        # A question mark is legal fragment data, so looking for `?` first cut
        # after the secret and left it in front of a `***` claiming otherwise.
        pytest.param(
            "https://host/#token=sekrit?next",
            "https://host/#***",
            id="a question mark inside the fragment",
        ),
        pytest.param("https://host?a#b", "https://host?***", id="query first, no path between"),
        # An apostrophe is a valid sub-delimiter in userinfo and in a query, so
        # treating it as a delimiter truncated the match inside the credential:
        # the first of these went out untouched and the second went out looking
        # redacted with the tail of the secret still on it.
        pytest.param(
            "https://sec'ret@host/", "https://***@host/", id="an apostrophe in the credential"
        ),
        pytest.param(
            "https://host/?token=sec'ret",
            "https://host/?***",
            id="an apostrophe in the query token",
        ),
        pytest.param(
            "said 'https://a@h/x' ok",
            "said 'https://***@h/x' ok",
            id="a quoted URL still keeps its quotes",
        ),
        pytest.param(
            'json "https://a@h/x" end',
            'json "https://***@h/x" end',
            id="a double-quoted URL is not swallowed",
        ),
        # With a query, and that is the case that tells the two delimiters
        # apart: `"` still ends the match so the closing quote survives the
        # truncation, where the apostrophe deliberately does not.
        pytest.param(
            'npm said "https://h/?k=v" once',
            'npm said "https://h/?***" once',
            id="a double-quoted URL with a query keeps its quote",
        ),
        pytest.param(
            "a https://u@h1/?k=1 and https://v@h2/?k=2 b",
            "a https://***@h1/?*** and https://***@h2/?*** b",
            id="two of them in one line",
        ),
    ],
)
def test_url_credentials_are_removed_before_anything_is_relayed(text, expected):
    """git and npm both take credentials in a URL and both print the URL back."""
    assert shared.redact_urls(text) == expected


@pytest.mark.parametrize(
    "configured,relayed",
    [
        pytest.param(
            "https://registry.example/npm/sekrit/",
            "https://registry.example/***",
            id="a key in the path",
        ),
        pytest.param(
            "https://registry.npmjs.org/", "https://registry.npmjs.org/", id="a bare root stays"
        ),
        pytest.param(
            "https://registry.npmjs.org", "https://registry.npmjs.org", id="no path at all stays"
        ),
        pytest.param("https://h:8443/a/b", "https://h:8443/***", id="the port is not a secret"),
        pytest.param(
            "https://u:p@h/npm/key/?t=1#f",
            "https://***@h/***?***",
            id="userinfo, path and query together",
        ),
        pytest.param("not a url", "not a url", id="not a URL at all"),
    ],
)
def test_a_registry_path_can_be_a_credential_too(configured, relayed):
    """Registries authenticate by path segment, and a registry is not any URL.

    `redact_urls` removes what can be a secret in *any* URL -- userinfo, query,
    fragment -- and a path is not that: in npm's own error text the path is the
    package, and blanking it there would destroy the one part worth reading. A
    configured registry is the exception, because a path-based key lives exactly
    where npm's own package path would otherwise be, so the trim belongs to this
    narrower helper rather than to redaction in general.

    Which server answered is what the field is for, and scheme, host and port
    already say it.
    """
    assert shared.redact_registry(configured) == relayed


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param(
            "npm error 403 Forbidden - GET https://registry.example/npm/sekrit/@sc%2fname",
            "npm error 403 Forbidden - GET https://registry.example/***",
            id="a registry that authenticates by path",
        ),
        pytest.param(
            "GET https://registry.npmjs.org/@sc%2fname",
            "GET https://registry.npmjs.org/***",
            id="the ordinary case loses the package path too",
        ),
        pytest.param("nothing to see here", "nothing to see here", id="no URL at all"),
    ],
)
def test_npm_error_text_is_cut_down_to_the_server(text, expected):
    """npm quotes the URL it asked for, so a path-based key is in the prose too.

    Redacting only the `registry` field left the same secret in `detail` and in
    the sentence beside it. Paths go wholesale rather than by matching the
    configured registry: that needs another query which can itself fail, and a
    redaction that works only when a lookup succeeds fails exactly when things
    are already going wrong. Nothing is lost -- `target` names the package and
    `registry` names the server, both structured and both already redacted.
    """
    assert shared.strip_url_paths(text) == expected


def test_credentials_are_removed_before_control_characters_are(monkeypatch):
    """Order, and it is not cosmetic.

    `clean` turns a control character into a space, and a space ends the run
    this pattern matches -- so cleaning first leaves `tok ***@` and half the
    token behind. bounded_err redacts first for that reason.
    """
    assert "en@" not in shared.bounded_err("fatal: https://tok\x00en@host/repo denied")
    assert shared.bounded_err("https://tok\x00en@host/x") == "https://***@host/x"


def test_a_missing_git_binary_stops_with_a_sentence(monkeypatch):
    def absent(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(shared.subprocess, "run", absent)
    git = shared.make_git(stopper)
    with pytest.raises(Stop, match="git not found"):
        git("/tmp", "status")


def test_a_git_call_that_hangs_is_stopped_and_named(monkeypatch):
    """A timeout is the failure mode that matters most here: this tool shells
    out to git inside somebody's repository, where a credential prompt or a
    wedged filesystem can block forever."""

    def hang(argv, **kwargs):
        raise shared.subprocess.TimeoutExpired(argv, kwargs.get("timeout", 120))

    monkeypatch.setattr(shared.subprocess, "run", hang)
    git = shared.make_git(stopper)
    with pytest.raises(Stop, match="timed out after"):
        git("/tmp", "status")


def test_a_hanging_git_can_be_routed_somewhere_other_than_die(monkeypatch):
    """A stalled remote is a different answer from a git that refused.

    Version pinning reports the two as different causes, and it must not tell
    them apart by reading the wording of this message -- that is classifying by
    prose, one file away from the code that produces it. Without the hook, a
    remote that hangs leaves through the plain die() and the caller loses the
    machine-readable failure it was promised.
    """

    def hang(argv, **kwargs):
        raise shared.subprocess.TimeoutExpired(argv, kwargs.get("timeout", 120))

    monkeypatch.setattr(shared.subprocess, "run", hang)
    routed = []

    def elsewhere(message):
        routed.append(message)
        raise Stop(message)

    git = shared.make_git(stopper, on_timeout=elsewhere)
    with pytest.raises(Stop, match="timed out after"):
        git("/tmp", "status")
    assert routed, "on_timeout was declared and then not consulted"
