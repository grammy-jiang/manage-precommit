"""Read and extend a .pre-commit-config.yaml without a YAML library.

There is no YAML parser in the standard library, and the skill installs as a
bare symlink under the user's system python3, so there is nowhere for a
third-party one to come from. That constraint turned out to be an improvement.

**Reading** is a strict line scanner. It understands exactly the shape a
pre-commit config has -- top-level keys, a block sequence under ``repos:``, and
``repo``/``rev``/``hooks``/``id`` inside it -- and *refuses* anything it cannot
prove it understands (anchors, aliases, merge keys, flow sequences where a block
is expected, more than one document, tabs). A refusal is an exit code; a guess
would be a wrong answer that looks right.

**Writing** never re-emits the file. Blocks of text are *inserted*; every other
byte is carried across untouched, and :func:`verify_additive` proves it by
reconstructing the original from the result. "Never clobber" is therefore a
property of the write strategy, not a behaviour we hope a library preserves.
A round-trip dump could reflow a quoted scalar or drop a blank line and still
call itself faithful. This cannot.

The one exception is documented at :data:`EMPTY_REPOS_LINE`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# `repos: []` is the empty list our own base skeleton writes. It is the single
# line this module may rewrite (to `repos:`) rather than merely insert around,
# because a block sequence cannot be appended to a flow-style empty list. The
# line's full content is known exactly, it carries no user data, and the rewrite
# happens only when the list is genuinely empty.
EMPTY_REPOS_LINE = re.compile(r"^(?P<indent> *)repos:\s*\[\s*\]\s*$")


class ConfigRefused(Exception):
    """The config uses something the scanner will not guess at.

    Carries the line number so the caller can point at it. The remedy is always
    the same: add the hook by hand, or simplify the construct.
    """

    def __init__(self, reason: str, line_no: int | None = None) -> None:
        self.reason = reason
        self.line_no = line_no
        where = f" (line {line_no})" if line_no else ""
        super().__init__(f"{reason}{where}")


# Constructs that make a line's meaning depend on something elsewhere in the
# file. An alias could pull in a repo the scanner never sees, so a config
# carrying one is refused outright rather than half-understood.
_ANCHOR = re.compile(r"(?:^|[\s:\[{,])[&*][A-Za-z0-9_][^\s\[\]{},]*")
_MERGE_KEY = re.compile(r"^\s*<<\s*:")


@dataclass
class Hook:
    """One ``- id: x`` item inside a repo's ``hooks:`` list."""

    id: str
    start: int  # index of the "- id:" line
    end: int  # last line index belonging to this hook, inclusive


@dataclass
class RepoEntry:
    """One ``- repo: URL`` item in the top-level ``repos:`` sequence."""

    url: str
    rev: str | None
    hooks: list[Hook] = field(default_factory=list)
    start: int = 0
    end: int = 0  # last line index of this entry, inclusive
    item_indent: int = 0  # column of the "-" marker
    hooks_key_line: int | None = None
    hook_item_indent: int | None = None  # column of the "-" of a hook item


@dataclass
class Config:
    """A scanned config, plus everything needed to insert text into it."""

    text: str
    lines: list[str]
    top_keys: dict[str, int]  # top-level key -> line index
    repos: list[RepoEntry]
    repos_key_line: int | None
    repos_seq_indent: int | None  # column of the "-" markers under repos:
    repos_end: int | None  # last line index of the repos block, inclusive
    empty_repos: bool  # `repos: []` -- see EMPTY_REPOS_LINE

    def repo(self, url: str) -> RepoEntry | None:
        for entry in self.repos:
            if entry.url == url:
                return entry
        return None

    def hook_ids(self, url: str) -> set[str]:
        entry = self.repo(url)
        return {h.id for h in entry.hooks} if entry else set()

    def local_hook_ids(self) -> set[str]:
        """Every hook id under a ``repo: local`` entry.

        There can legitimately be more than one local block, so these are
        pooled: a local hook is "already present" if any of them defines it.
        """
        ids: set[str] = set()
        for entry in self.repos:
            if entry.url == "local":
                ids |= {h.id for h in entry.hooks}
        return ids


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _split_key(text: str) -> tuple[str, str] | None:
    """Split ``key: value`` at the first colon outside quotes.

    Returns None when the text is not a mapping entry. Quote tracking matters
    because ``files: '\\.ya?ml:$'`` contains a colon that is data.
    """
    quote: str | None = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "#":  # a comment starts here; no key was found before it
            return None
        if ch == ":":
            rest = text[i + 1 :]
            if rest and not rest[0].isspace():
                continue  # "a:b" is a plain scalar, not a mapping entry
            return text[:i].strip(), rest.strip()
    return None


def _scalar(raw: str) -> str:
    """The value of a scalar: quotes removed, trailing comment dropped."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in "\"'":
        quote = raw[0]
        end = raw.find(quote, 1)
        if end == -1:
            return raw[1:]
        return raw[1:end]
    # Unquoted: a " #" starts a comment, a bare "#" inside a word does not.
    cut = raw.find(" #")
    if cut != -1:
        raw = raw[:cut]
    return raw.strip()


def _code_only(line: str) -> str:
    """The line with any trailing unquoted #-comment removed.

    The anchor and merge-key guards run over the whole file before any
    structural parsing, so they must not fire on prose. English comment style
    puts `*emphasis*` in comments, and `# skip *generated* files` matched the
    anchor pattern -- refusing an ordinary config the tool exists to extend.
    """
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def scan(text: str) -> Config:
    """Read a config's structure, or raise ConfigRefused.

    Only the ``repos:`` block is understood in depth. Other top-level keys are
    recorded by name and their bodies skipped wholesale -- ``ci:`` and
    ``default_language_version:`` are ordinary and must not be refused just
    because this tool has no interest in them.
    """
    if "\x00" in text:
        raise ConfigRefused("config contains a NUL byte")
    lines = text.splitlines()

    seen_content = False
    for i, line in enumerate(lines, start=1):
        if re.match(r"^\s*\t", line):
            raise ConfigRefused("config indents with a tab, which YAML forbids", i)
        stripped = line.strip()
        if stripped == "---":
            # A leading `---` opens the only document and is fine. One that
            # follows real content opens a SECOND document, and a second
            # document could carry another `repos:` this tool would never see.
            if seen_content:
                raise ConfigRefused("config holds more than one YAML document", i)
            continue
        if stripped == "..." or stripped.startswith("... "):
            raise ConfigRefused("config holds more than one YAML document", i)
        code = _code_only(line)
        if _MERGE_KEY.match(code):
            raise ConfigRefused(
                "config uses a merge key (<<:), which this tool will not guess at", i
            )
        if _ANCHOR.search(code) and not _is_blank_or_comment(line):
            raise ConfigRefused(
                "config uses a YAML anchor or alias, which this tool will not guess at", i
            )
        if not _is_blank_or_comment(line):
            seen_content = True

    top_keys: dict[str, int] = {}
    repos: list[RepoEntry] = []
    repos_key_line: int | None = None
    repos_seq_indent: int | None = None
    repos_end: int | None = None
    empty_repos = False

    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_blank_or_comment(line) or line.strip() == "---":
            i += 1
            continue
        if _indent_of(line) != 0:
            raise ConfigRefused("unexpected indentation at the top level of the config", i + 1)
        parsed = _split_key(line)
        if parsed is None:
            raise ConfigRefused("top-level line is not a `key: value` mapping entry", i + 1)
        key, value = parsed
        if key in top_keys:
            raise ConfigRefused(f"config defines the top-level key {key!r} twice", i + 1)
        top_keys[key] = i
        if key != "repos":
            i = _skip_block(lines, i + 1)
            continue

        repos_key_line = i
        if EMPTY_REPOS_LINE.match(line):
            empty_repos = True
            repos_end = i
            i += 1
            continue
        if value:
            raise ConfigRefused(
                "`repos:` is not a block sequence (flow style or a scalar); "
                "this tool only extends the block form",
                i + 1,
            )
        i, repos, repos_seq_indent, repos_end = _scan_repos(lines, i + 1)

    if repos_key_line is None:
        raise ConfigRefused("config has no top-level `repos:` key")

    return Config(
        text=text,
        lines=lines,
        top_keys=top_keys,
        repos=repos,
        repos_key_line=repos_key_line,
        repos_seq_indent=repos_seq_indent,
        repos_end=repos_end,
        empty_repos=empty_repos,
    )


def _skip_block(lines: list[str], start: int) -> int:
    """Index of the next line at indent 0, skipping a key's indented body."""
    i = start
    while i < len(lines):
        line = lines[i]
        if not _is_blank_or_comment(line) and _indent_of(line) == 0:
            return i
        i += 1
    return i


def _scan_repos(
    lines: list[str], start: int
) -> tuple[int, list[RepoEntry], int | None, int | None]:
    """Parse the block sequence under ``repos:``.

    Returns (next top-level index, entries, sequence indent, last line index).
    """
    entries: list[RepoEntry] = []
    seq_indent: int | None = None
    last = start - 1
    i = start
    while i < len(lines):
        line = lines[i]
        if _is_blank_or_comment(line):
            i += 1
            continue
        indent = _indent_of(line)
        body = line.strip()
        if seq_indent is None:
            # A block sequence may sit at the SAME column as its key, which is
            # how a great many configs are written:
            #     repos:
            #     - repo: ...
            # so the first item decides the column rather than "must be > 0".
            # Anything that is not a sequence item means `repos:` is empty and
            # this block is already over.
            if not body.startswith("- ") and body != "-":
                break
            seq_indent = indent
        else:
            if indent < seq_indent:
                break
            if indent > seq_indent or not body.startswith(("- ", "-")):
                raise ConfigRefused("unexpected line inside the `repos:` block", i + 1)
        if body == "-":
            raise ConfigRefused("a `-` on its own line is not a shape this tool reads", i + 1)
        i, entry = _scan_repo_entry(lines, i, indent)
        entries.append(entry)
        last = entry.end
    return i, entries, seq_indent, (last if entries else None)


# `|`, `>` and their chomping/indent modifiers open a multi-line scalar.
_BLOCK_SCALAR = re.compile(r"^[|>][+-]?\d*$|^[|>]\d*[+-]?$")


def _refuse_multiline_scalar(
    lines: list[str], at: int, key_indent: int, raw_value: str, what: str
) -> None:
    """Refuse a value that continues onto another line.

    `repo:`, `rev:` and `id:` are read from one physical line. YAML lets a plain
    or quoted scalar fold across several, and lets `|`/`>` open a block scalar --
    and the entry/hook loops skip a continuation line without matching anything,
    so the value would be silently TRUNCATED to its first line. A truncated url
    is worse than a refusal: it makes `already present` compare against the
    wrong thing and can insert a duplicate entry.
    """
    if _BLOCK_SCALAR.match(raw_value.strip()):
        raise ConfigRefused(
            f"`{what}:` opens a multi-line block scalar; this tool reads single-line values",
            at + 1,
        )
    nxt = _next_content(lines, at + 1)
    if nxt is None or _indent_of(lines[nxt]) <= key_indent:
        return
    follower = lines[nxt].strip()
    # A deeper line that is itself a mapping entry or a sequence item is
    # ordinary nesting. Anything else at that depth is a folded continuation.
    if follower.startswith(("- ", "-")) or _split_key(follower) is not None:
        return
    raise ConfigRefused(
        f"`{what}:` continues onto the next line; this tool reads single-line values",
        nxt + 1,
    )


def _scan_repo_entry(lines: list[str], start: int, item_indent: int) -> tuple[int, RepoEntry]:
    """Parse one ``- repo: ...`` item and everything indented under it."""
    first = lines[start]
    inline = first.strip()[2:]  # past the "- "
    if inline.lstrip().startswith(("[", "{")):
        raise ConfigRefused(
            "a repo entry is in flow style; this tool only reads block form", start + 1
        )
    parsed = _split_key(inline)
    if parsed is None:
        raise ConfigRefused("a repo entry does not start with `repo:`", start + 1)
    # The key column of the item's mapping: "- " is two columns, so the keys of
    # the mapping it introduces start two past the marker.
    key_indent = item_indent + 2

    entry = RepoEntry(url="", rev=None, start=start, end=start, item_indent=item_indent)
    key, value = parsed
    if key == "repo":
        _refuse_multiline_scalar(lines, start, key_indent, value, "repo")
        entry.url = _scalar(value)
    elif key in ("rev", "hooks"):
        raise ConfigRefused("a repo entry lists `repo:` after another key; unsupported", start + 1)
    else:
        raise ConfigRefused(f"unexpected key {key!r} at the start of a repo entry", start + 1)

    i = start + 1
    in_hooks = False
    while i < len(lines):
        line = lines[i]
        if _is_blank_or_comment(line):
            i += 1
            continue
        indent = _indent_of(line)
        if indent <= item_indent:
            break
        entry.end = i
        if in_hooks and entry.hook_item_indent is not None and indent == entry.hook_item_indent:
            body = line.strip()
            if body.startswith("- "):
                i, hook = _scan_hook(lines, i, indent)
                entry.hooks.append(hook)
                entry.end = hook.end
                continue
        if indent == key_indent:
            parsed = _split_key(line.strip())
            if parsed is None:
                i += 1
                continue
            key, value = parsed
            in_hooks = key == "hooks"
            if key == "rev":
                _refuse_multiline_scalar(lines, i, key_indent, value, "rev")
                entry.rev = _scalar(value)
            elif key == "repo":
                raise ConfigRefused("a repo entry defines `repo:` twice", i + 1)
            elif in_hooks:
                if value.lstrip().startswith("["):
                    raise ConfigRefused(
                        "`hooks:` is in flow style; this tool only reads block form", i + 1
                    )
                if value:
                    raise ConfigRefused("`hooks:` is not a block sequence", i + 1)
                entry.hooks_key_line = i
                nxt = _next_content(lines, i + 1)
                if nxt is not None and _indent_of(lines[nxt]) > key_indent:
                    entry.hook_item_indent = _indent_of(lines[nxt])
                elif (
                    nxt is not None
                    and _indent_of(lines[nxt]) == key_indent
                    # `hooks:` followed by items at the SAME column as the key:
                    # valid YAML, and how many hand-written configs look.
                    and lines[nxt].strip().startswith("- ")
                ):
                    entry.hook_item_indent = key_indent
            i += 1
            continue
        i += 1
    return i, entry


def _scan_hook(lines: list[str], start: int, item_indent: int) -> tuple[int, Hook]:
    """Parse one ``- id: x`` hook item and its indented body."""
    body = lines[start].strip()[2:]
    if body.lstrip().startswith(("[", "{")):
        raise ConfigRefused("a hook is in flow style; this tool only reads block form", start + 1)
    parsed = _split_key(body)
    if parsed is None or parsed[0] != "id":
        raise ConfigRefused("a hook item does not start with `id:`", start + 1)
    _refuse_multiline_scalar(lines, start, item_indent + 2, parsed[1], "id")
    hook = Hook(id=_scalar(parsed[1]), start=start, end=start)
    i = start + 1
    while i < len(lines):
        line = lines[i]
        if _is_blank_or_comment(line):
            i += 1
            continue
        if _indent_of(line) <= item_indent:
            break
        hook.end = i
        i += 1
    return i, hook


def _next_content(lines: list[str], start: int) -> int | None:
    for i in range(start, len(lines)):
        if not _is_blank_or_comment(lines[i]):
            return i
    return None


# -- writing -----------------------------------------------------------------


def reindent(block: str, spaces: int) -> str:
    """Shift a whole block by `spaces` columns, leaving blank lines empty.

    The catalog fragments are authored at a canonical indent; a target config
    may use another. Only block-style YAML with no multi-line scalars is
    shifted, which is all the fragments contain.
    """
    if spaces == 0:
        return block
    out = []
    for line in block.split("\n"):
        if not line.strip():
            out.append("")
        elif spaces > 0:
            out.append(" " * spaces + line)
        else:
            strip = min(-spaces, _indent_of(line))
            out.append(line[strip:])
    return "\n".join(out)


def hook_delta(entry: RepoEntry) -> int | None:
    """How far this entry indents its hook items past the mapping's key column.

    YAML allows a block sequence either indented under its key or at the same
    column, and yamllint's `indent-sequences: consistent` requires a file to
    pick one. So an inserted block has to adopt whatever the target file already
    does, rather than whatever the catalog fragment happens to look like --
    otherwise this tool writes a config its own yamllint hook then rejects.
    """
    if entry.hook_item_indent is None:
        return None
    return entry.hook_item_indent - (entry.item_indent + 2)


def observed_hook_delta(cfg: Config) -> int | None:
    """The hook-indent convention this config already uses, if it has one."""
    for entry in cfg.repos:
        delta = hook_delta(entry)
        if delta is not None:
            return delta
    return None


def render_entry(
    text: str, entry: RepoEntry, seq_indent: int, want_hook_delta: int | None
) -> list[str]:
    """A fragment's lines, re-indented to a target file's conventions.

    Two shifts, not one: the entry moves to the target's sequence column, and
    its hook items move again to the target's sequence-indentation convention.
    """
    lines = ("repos:\n" + text.rstrip("\n")).splitlines()
    base = seq_indent - entry.item_indent
    mine = hook_delta(entry)
    extra = 0 if want_hook_delta is None or mine is None else want_hook_delta - mine
    hook_lines: set[int] = set()
    for hook in entry.hooks:
        hook_lines.update(range(hook.start, hook.end + 1))
    out = []
    for i in range(1, len(lines)):  # index 0 is the synthetic "repos:" line
        out.append(reindent(lines[i], base + (extra if i in hook_lines else 0)))
    return out


@dataclass
class Insertion:
    """One block of text to add, at a line index, before the existing line."""

    at: int  # insert before lines[at]
    block: list[str]
    what: str  # for the report


def apply_insertions(lines: list[str], insertions: list[Insertion]) -> list[str]:
    """Splice blocks in, working from the bottom so earlier indices stay valid."""
    out = list(lines)
    for ins in sorted(insertions, key=lambda x: x.at, reverse=True):
        out[ins.at : ins.at] = ins.block
    return out


def verify_additive(original: list[str], result: list[str], insertions: list[Insertion]) -> None:
    """Prove the result is the original plus exactly the inserted blocks.

    Reconstructs the original by deleting the inserted line ranges from the
    result. Anything else that moved -- a reflowed scalar, a dropped blank line,
    a stray edit -- shows up here as a mismatch rather than in the user's diff.
    """
    positions = [ins.at for ins in insertions]
    if len(set(positions)) != len(positions):
        raise ConfigRefused(
            "internal check failed: two blocks were planned for the same line, whose "
            "order the splice cannot preserve. Nothing was written."
        )
    # Ascending, with NO running offset: removing an earlier block shifts every
    # later one back to exactly the index it was inserted at, so each `at` is
    # already correct by the time it is reached. Adding an offset here was a real
    # defect -- it mis-sliced every insertion after the first and reported a
    # clean additive merge as a clobber.
    rebuilt = list(result)
    for ins in sorted(insertions, key=lambda x: x.at):
        del rebuilt[ins.at : ins.at + len(ins.block)]
    if rebuilt != original:
        raise ConfigRefused(
            "internal check failed: the write would have changed lines outside the "
            "blocks it added. Nothing was written."
        )
