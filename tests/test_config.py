"""The scanner and the additive writer.

These are the two halves of the ruamel replacement, and the reason it is an
improvement rather than a compromise: the scanner refuses what it cannot prove
it understands, and the writer cannot touch a byte outside the blocks it adds.
"""

from __future__ import annotations

import pytest

import config as C

# -- shapes that must be READ correctly --------------------------------------

INDENTED = """\
minimum_pre_commit_version: "4.0.0"
repos:
  - repo: https://github.com/psf/black
    rev: 24.1.0
    hooks:
      - id: black
        args: [--line-length, "100"]
      - id: black-jupyter
"""

SAME_COLUMN = """\
repos:
- repo: https://github.com/psf/black
  rev: 24.1.0
  hooks:
  - id: black
- repo: local
  hooks:
  - id: mine
    name: mine
    entry: ./x.sh
    language: script
"""

OTHER_TOP_KEYS = """\
ci:
  autofix_prs: true
  skip: [mermaid-lint]
default_language_version:
  python: python3.11
exclude: '^vendor/'
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0
    hooks:
      - id: trailing-whitespace
"""


def test_reads_the_indented_style():
    cfg = C.scan(INDENTED)
    assert [e.url for e in cfg.repos] == ["https://github.com/psf/black"]
    entry = cfg.repos[0]
    assert entry.rev == "24.1.0"
    assert [h.id for h in entry.hooks] == ["black", "black-jupyter"]
    assert entry.item_indent == 2
    assert entry.hook_item_indent == 6


def test_reads_a_sequence_at_the_same_column_as_its_key():
    """`repos:` with items at column 0 is valid YAML and very common.

    Pins the defect where the scanner required indent > 0 and refused this
    whole style as "unexpected indentation at the top level".
    """
    cfg = C.scan(SAME_COLUMN)
    assert [e.url for e in cfg.repos] == ["https://github.com/psf/black", "local"]
    assert cfg.repos_seq_indent == 0
    assert cfg.repos[0].hook_item_indent == 2
    assert cfg.local_hook_ids() == {"mine"}


def test_skips_top_level_keys_it_has_no_interest_in():
    """`ci:` and `default_language_version:` are ordinary; refusing them would
    reject a large share of real configs."""
    cfg = C.scan(OTHER_TOP_KEYS)
    assert set(cfg.top_keys) == {"ci", "default_language_version", "exclude", "repos"}
    assert [e.url for e in cfg.repos] == ["https://github.com/pre-commit/pre-commit-hooks"]


def test_a_colon_inside_a_quoted_scalar_is_data():
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: x\n        files: '\\.ya?ml:$'\n"
    )
    assert [h.id for h in cfg.repos[0].hooks] == ["x"]


def test_a_trailing_comment_is_not_part_of_a_rev():
    cfg = C.scan(
        "repos:\n  - repo: https://x/y\n    rev: v1.2.3  # pinned deliberately\n"
        "    hooks:\n      - id: a\n"
    )
    assert cfg.repos[0].rev == "v1.2.3"


# -- shapes that must be REFUSED ---------------------------------------------


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("anchor", "repos:\n  - &base\n    repo: https://x/y\n"),
        ("alias", "repos:\n  - repo: https://x/y\n    hooks: *common\n"),
        ("merge key", "d: &d\n  rev: v1\nrepos:\n  - <<: *d\n    repo: https://x/y\n"),
        ("second document", "repos: []\n---\nrepos: []\n"),
        ("document end", "repos: []\n...\n"),
        ("flow repos", "repos: [{repo: local}]\n"),
        ("flow hooks", "repos:\n  - repo: https://x/y\n    hooks: [{id: a}]\n"),
        ("flow entry", "repos:\n  - {repo: local}\n"),
        ("tab indent", "repos:\n\t- repo: https://x/y\n"),
        ("no repos key", "exclude: 'x'\n"),
        ("duplicate top key", "exclude: 'a'\nexclude: 'b'\nrepos: []\n"),
        ("NUL byte", "repos: []\n\x00\n"),
    ],
)
def test_refuses_what_it_cannot_prove_it_understands(name, text):
    with pytest.raises(C.ConfigRefused):
        C.scan(text)


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("stray line in repos block", "repos:\n  - repo: https://x/y\n  oops\n"),
        ("bare dash", "repos:\n  -\n"),
        ("entry not starting with repo", "repos:\n  - rev: v1\n    repo: https://x/y\n"),
        ("unexpected first key", "repos:\n  - name: mine\n    repo: https://x/y\n"),
        ("repo twice", "repos:\n  - repo: https://x/y\n    repo: https://a/b\n"),
        ("hooks as a scalar", "repos:\n  - repo: https://x/y\n    hooks: nope\n"),
        ("hook in flow style", "repos:\n  - repo: local\n    hooks:\n      - {id: a}\n"),
        ("hook without id", "repos:\n  - repo: local\n    hooks:\n      - name: a\n"),
        ("folded repo url", "repos:\n  - repo: https://github.com/psf/\n      black\n"),
        ("block scalar rev", "repos:\n  - repo: https://x/y\n    rev: |\n      v1\n"),
        ("folded hook id", "repos:\n  - repo: local\n    hooks:\n      - id: my\n          hook\n"),
    ],
)
def test_refuses_inside_an_opened_entry_too(name, text):
    """An untested refusal branch is an untested piece of the spec -- and a
    regression that downgrades one to a guess corrupts the user's config."""
    with pytest.raises(C.ConfigRefused):
        C.scan(text)


def test_a_wrapped_url_is_refused_rather_than_truncated():
    """Truncation is worse than refusal: a shortened url compares against the
    wrong thing, so `already present` misses and a duplicate entry is added."""
    with pytest.raises(C.ConfigRefused, match="continues onto the next line"):
        C.scan("repos:\n  - repo: https://github.com/psf/\n      black\n    rev: v1\n")


def test_a_leading_document_marker_is_fine():
    """One `---` opens the only document; only a second one is ambiguous."""
    cfg = C.scan("---\nrepos:\n  - repo: local\n    hooks:\n      - id: a\n")
    assert [e.url for e in cfg.repos] == ["local"]


@pytest.mark.parametrize(
    "line",
    [
        "    entry: bash -c 'a && b'",
        "    args: [--fix, --glob, '*.md']",
        "    exclude: '^(a|b)*$'",
        "    entry: node scripts/lint-mermaid.mjs",
        "    files: '\\.(md|markdown)$'",
    ],
)
def test_the_anchor_guard_does_not_fire_on_ordinary_lines(line):
    """`&&` in a shell entry and `*` in a glob are not YAML anchors.

    A false positive here refuses a perfectly ordinary config, which is a worse
    failure than the one the guard exists to prevent.
    """
    assert C._ANCHOR.search(line) is None


# -- the additive guarantee --------------------------------------------------


def test_verify_additive_accepts_a_real_multi_block_insert():
    """Pins the offset defect: verify_additive added a running offset while
    reconstructing, which mis-sliced every insertion after the first and
    reported a clean additive merge as a clobber."""
    original = [f"line{i}" for i in range(12)]
    insertions = [
        C.Insertion(at=3, block=["added-a"], what="a"),
        C.Insertion(at=12, block=["added-b1", "added-b2"], what="b"),
    ]
    result = C.apply_insertions(original, insertions)
    assert len(result) == len(original) + 3
    C.verify_additive(original, result, insertions)  # must not raise


def test_verify_additive_catches_a_changed_line():
    original = ["a", "b", "c"]
    insertions = [C.Insertion(at=1, block=["new"], what="x")]
    result = C.apply_insertions(original, insertions)
    result[2] = "b-TAMPERED"
    with pytest.raises(C.ConfigRefused, match="outside the blocks"):
        C.verify_additive(original, result, insertions)


def test_verify_additive_rejects_two_blocks_at_one_position():
    """The splice cannot preserve their order, so it must not be attempted."""
    original = ["a", "b"]
    insertions = [
        C.Insertion(at=1, block=["x"], what="x"),
        C.Insertion(at=1, block=["y"], what="y"),
    ]
    with pytest.raises(C.ConfigRefused, match="same line"):
        C.verify_additive(original, C.apply_insertions(original, insertions), insertions)


def test_apply_insertions_keeps_untouched_lines_byte_identical():
    original = ["# comment", "exclude: '^x/'", "", "repos:", "- repo: local"]
    insertions = [C.Insertion(at=3, block=['minimum_pre_commit_version: "4.0.0"'], what="m")]
    result = C.apply_insertions(original, insertions)
    assert result[:3] == original[:3]
    assert result[4:] == original[3:]


# -- indentation -------------------------------------------------------------


def test_hook_delta_reads_the_files_convention():
    indented = C.scan(INDENTED)
    same = C.scan(SAME_COLUMN)
    assert C.observed_hook_delta(indented) == 2  # hooks indented under their key
    assert C.observed_hook_delta(same) == 0  # hooks at the key's own column


def test_render_entry_adopts_the_targets_convention():
    """Writing indented hooks into a file that does not indent them produces a
    config the skill's own yamllint hook rejects under
    `indent-sequences: consistent`."""
    fragment = "- repo: https://x/y\n  rev: v1\n  hooks:\n    - id: a\n      args: [--x]\n"
    entry = C.scan("repos:\n" + fragment).repos[0]

    flat = C.render_entry(fragment, entry, seq_indent=0, want_hook_delta=0)
    assert flat == ["- repo: https://x/y", "  rev: v1", "  hooks:", "  - id: a", "    args: [--x]"]

    nested = C.render_entry(fragment, entry, seq_indent=2, want_hook_delta=2)
    assert nested[0] == "  - repo: https://x/y"
    assert nested[3] == "      - id: a"


def test_reindent_leaves_blank_lines_empty():
    assert C.reindent("a\n\nb", 2) == "  a\n\n  b"
