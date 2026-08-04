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
