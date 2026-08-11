"""Helpers shared by the manage-precommit scripts.

Four concerns live here because more than one script needs them and separate
copies would drift: neutralising repo-derived text, opening a file without
following a symlink, writing a file atomically while keeping its permissions,
and the shape of the JSON the tools hand to each other.

Imported by path: Python puts the running script's directory on sys.path, and
all the scripts sit together in the skill's ``scripts/`` directory. Nothing here
imports ``manage_precommit`` or any third-party module, because the skill is
installed as a bare symlink and runs under the user's system python3 -- an
import outside the standard library is a runtime failure on someone else's
machine, not a packaging inconvenience.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Sequence
from typing import NoReturn, TypedDict

# Character ranges are declared as integers and the pattern is built at import,
# rather than written as escapes inside a string literal. A literal here would
# put the very bytes these patterns exist to catch into this file, where a
# colorising shell, an editor or a careless merge can mangle them invisibly --
# and where nothing would notice, because a mangled class still compiles.
# Every range is named, so what is covered is reviewable without decoding.

# Invisible or text-moving: nothing here is ever legitimate in a name, a hook
# id, a URL or a path that a human is about to act on.
_INVISIBLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x061C, 0x061C),  # ARABIC LETTER MARK
    (0x200B, 0x200F),  # zero-width space .. right-to-left mark
    (0x202A, 0x202E),  # bidi embedding and override
    (0x2060, 0x2064),  # word joiner .. invisible plus
    (0x2066, 0x2069),  # bidi isolates
    (0x2028, 0x2029),  # line/paragraph separator: str.splitlines() breaks here
    (0xFEFF, 0xFEFF),  # BOM / zero-width no-break space
    (0xFE00, 0xFE0F),  # variation selectors
    (0xE0000, 0xE007F),  # Unicode Tag block
    (0xE0100, 0xE01EF),  # variation selectors supplement
)

# C0 + DEL + the C1 block. C1 carries a single-codepoint CSI at U+009B, so a
# summary that neutralised only ESC would still be forgeable.
_C0_C1_RANGES: tuple[tuple[int, int], ...] = ((0x00, 0x1F), (0x7F, 0x9F))

# The same, minus tab (0x09), newline (0x0A) and carriage return (0x0D).
_C0_C1_RANGES_KEEPING_WHITESPACE: tuple[tuple[int, int], ...] = (
    (0x00, 0x08),
    (0x0B, 0x0C),
    (0x0E, 0x1F),
    (0x7F, 0x9F),
)


def _char_class(*groups: Iterable[tuple[int, int]]) -> str:
    """Build a regex character class from integer codepoint ranges.

    re.escape on each endpoint, so a range bound that happens to be a regex
    metacharacter cannot change the class's meaning.
    """
    parts = []
    for group in groups:
        for low, high in group:
            if low == high:
                parts.append(re.escape(chr(low)))
            else:
                parts.append(f"{re.escape(chr(low))}-{re.escape(chr(high))}")
    return "[" + "".join(parts) + "]"


CONTROL_CHARS = re.compile(_char_class(_C0_C1_RANGES, _INVISIBLE_RANGES))


def clean(value: object) -> str:
    """Stringify a value and neutralise anything that could forge output.

    Replaced with a space rather than deleted, so a two-line value reads as two
    words instead of silently becoming one.
    """
    text = value if isinstance(value, str) else str(value)
    return CONTROL_CHARS.sub(" ", text).strip()


# `clean` neutralises tab/newline/CR because a newline inside a summary *field*
# forges a row; a newline inside a *file* is just a line ending, so scanning
# file content must not flag it.
SUSPICIOUS_CHARS = re.compile(_char_class(_C0_C1_RANGES_KEEPING_WHITESPACE, _INVISIBLE_RANGES))


def refuse_option_like(value: str, what: str, die: Callable[[str], NoReturn]) -> str:
    """Reject a value a command would read as an option rather than data.

    Catalog keys, refs, remotes and branches all reach argv, and all can come
    from somewhere the user does not control. One guard, one wording.
    """
    if value.startswith("-"):
        die(f"refusing {what} that looks like an option: {value!r}")
    return value


def has_suspicious_chars(text: str) -> bool:
    """True if text carries a control or text-reordering character.

    Ordinary whitespace does not count. Used where stripping would be wrong (a
    config file is written verbatim) but the reader still needs to be told the
    bytes are there.
    """
    return SUSPICIOUS_CHARS.search(text) is not None


class SymlinkRefused(OSError):
    """Raised instead of following a symlink at the final path component."""


class NotARegularFile(OSError):
    """Raised for a FIFO, device or directory where a plain file is required."""


class TooLarge(OSError):
    """Raised when a file this skill reads exceeds its size bound."""


MAX_READ_BYTES = 4_000_000  # a config, an asset or a facts file is a few KB


def read_bytes_nofollow(path: str, max_bytes: int = MAX_READ_BYTES) -> bytes:
    """Read a file, refusing to follow a symlink at the final component.

    A ``.pre-commit-config.yaml`` that is a symlink would otherwise let these
    tools read an arbitrary file (say ~/.ssh/id_rsa) and carry its contents into
    a commit.

    Bounded: nothing this skill reads is legitimately large, and an unbounded
    read is an unbounded allocation.
    """
    try:
        # O_NONBLOCK as well as O_NOFOLLOW: opening a FIFO with no writer BLOCKS,
        # so without it the refusal below is never reached -- the process simply
        # hangs, which reads as a slow run rather than a failure. On a regular
        # file O_NONBLOCK has no effect.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # what O_NOFOLLOW raises on a symlink
            raise SymlinkRefused(f"{path} is a symlink; refusing to follow it") from exc
        raise
    try:
        # Checked on the raw descriptor, BEFORE fdopen: a directory makes fdopen
        # itself raise, and O_NOFOLLOW stops a symlink but not a FIFO (reading
        # one blocks forever with no writer). Only a regular file is a config.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotARegularFile(f"{path} is not a regular file; refusing to read it")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise TooLarge(f"{path} is larger than {max_bytes} bytes; refusing to read it")
        return data


def read_bytes_or_die(path: str, die: Callable[[str], NoReturn]) -> bytes:
    """read_bytes_nofollow with every refusal turned into a caller's die().

    Every script needs the same three-way translation; one copy keeps their
    error wording identical.
    """
    try:
        return read_bytes_nofollow(path)
    except (SymlinkRefused, NotARegularFile, TooLarge) as exc:
        die(str(exc))
    except OSError as exc:
        die(f"cannot read {path}: {exc}")


def atomic_write_bytes(target: str, data: bytes, *, mode: int | None = None) -> None:
    """Replace target in one step; a crash mid-write can never truncate it.

    mkstemp creates the temp file 0600, so `mode` is applied before the rename
    when the caller wants the destination to keep different permissions.
    """
    # os.replace() puts a regular file where the link was, rather than writing
    # through it -- so the link's target is never touched. Stated here because
    # callers rely on it for paths (facts files) with no symlink gate of their own.
    directory = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def default_file_mode() -> int:
    """0666 & ~umask -- what an ordinary tool would create a file as."""
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def preserved_mode(path: str) -> int:
    """The mode `path` already has, or the umask default when it does not exist.

    follow_symlinks=False: read the link's own mode rather than leaking the
    permission bits of whatever it points at into the file we write.
    """
    try:
        return stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    except OSError:
        return default_file_mode()


def write_json(path: str, payload: dict) -> None:
    """Write a facts file atomically, keeping its permissions.

    Several commands update this file in turn; a half-written one would fail the
    next step with a JSON error rather than the real cause. mkstemp creates 0600,
    so without restoring a mode every update would narrow the file.
    """
    text = json.dumps(payload, indent=2) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"), mode=preserved_mode(path))


def read_json_or_die(path: str, die: Callable[[str], NoReturn]) -> dict:
    """Read the facts JSON, or stop with the caller's die().

    The tools' cross-boundary handshake, and it was implemented three times --
    twice in one file, three lines apart -- with identical wording. Anything
    that ever needs to change about how this file is read (BOM tolerance,
    naming the offending key) has to be changeable in one place.
    """
    raw = read_bytes_or_die(path, die)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        die(f"cannot read facts file {path}: {exc}")
    if not isinstance(parsed, dict):
        die("facts file must contain a JSON object")
    return parsed


def write_json_or_die(path: str, payload: dict, die: Callable[[str], NoReturn]) -> None:
    """write_json with the failure turned into a caller's die().

    Mirrors read_bytes_or_die so every script reports an unwritable facts file
    the same way.
    """
    try:
        write_json(path, payload)
    except OSError as exc:
        die(f"cannot write facts file {path}: {exc}")


def refuse_facts_inside_repo(
    repo: str, facts_path: str | None, die: Callable[[str], NoReturn]
) -> None:
    """The facts file must live outside the repository being worked on.

    Shared, and checked before the file is ever read: the set of files a run may
    touch is read *from* this file, so a guard needing its contents first could
    not protect those contents. Outside-the-repo is the stronger and simpler
    invariant anyway -- a facts file inside the tree turns up in `git status`,
    lands in the diff the user is about to approve, and, if it aliased a managed
    path, would be overwritten by the very step meant to commit it.
    """
    if facts_path is None:
        return
    root = os.path.realpath(repo)
    target = os.path.realpath(facts_path)
    if target == root or target.startswith(root + os.sep):
        die(
            f"the facts file must be outside the repository ({facts_path} is inside "
            f"{repo}). Put it in a mktemp path; the run deletes it at the end."
        )


def porcelain_path(line: str) -> str:
    """The path from one `git status --porcelain --no-renames` line.

    git C-quotes any path carrying a non-ASCII byte, a literal quote, a
    backslash or a control character (core.quotePath defaults on): an accented
    filename comes back as a quoted string full of octal escapes. Three parsers
    already stripped the quotes and a fourth did not, and that fourth builds the
    autofixed list the user is shown -- a name they then cannot find. One
    helper, so a future fix cannot miss a sibling again.
    """
    path = line[3:].strip()
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        body = path[1:-1]
        try:
            # The same escaping C uses, which is what git emits.
            return (
                body.encode("latin-1", "backslashreplace")
                .decode("unicode_escape")
                .encode("latin-1")
                .decode("utf-8", "replace")
            )
        except (UnicodeDecodeError, UnicodeEncodeError):
            return body
    return path


MAX_ERR_LEN = 400  # git stderr can carry arbitrary remote-server text


def is_work_tree(git: Callable[..., tuple[int, str, str]], repo: str) -> bool:
    """True only inside an actual work tree.

    `rev-parse --is-inside-work-tree` exits 0 and prints "false" when run inside
    a .git directory, so testing the exit status alone calls that a repository.
    Shared because two scripts need it and the rc-only variant had already
    drifted into both of them.
    """
    rc, out, _ = git(repo, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"


def safe_porcelain(
    git: Callable[..., tuple[int, str, str]],
    repo: str,
    paths: Sequence[str],
    die: Callable[[str], NoReturn],
    *,
    what: str,
) -> str:
    """`git status --porcelain --no-renames [-- paths]`, or a hard stop.

    One policy, stated once: **a failed check is not a clean result.** Empty
    output means "nothing changed", so swallowing a non-zero exit (a locked
    index, an I/O error) reports the tree as clean -- which variously discards
    work this run just wrote, merges into a user's in-progress edit, or drops
    the autofix disclosure Step 5 promises before the commit question.

    Shared because three call sites across two scripts had hand-written the
    same command and the same guard with three different wordings, and had
    already begun to drift. `what` supplies the only part that legitimately
    differs. A caller needing its own exit code passes a `die` that closes over
    it -- the failure is one thing; how loudly each caller exits is theirs.

    Output is UNSTRIPPED: the two leading status columns are meaningful.
    """
    args = ["status", "--porcelain", "--no-renames"]
    if paths:
        args += ["--", *paths]
    rc, out, err = git(repo, *args, strip=False)
    if rc != 0:
        die(
            f"git status failed (exit {rc}): {err or 'no stderr'}. Refusing to report "
            f"{what}, because a failed check is not a clean result."
        )
    return out


def make_git(
    die: Callable[[str], NoReturn], *, timeout: int = 120
) -> Callable[..., tuple[int, str, str]]:
    """Build a hardened `git` runner bound to a script's own die().

    Shared rather than copied because the hardening below is the security
    boundary: two copies would drift, and the copy that lost a flag would be the
    one still running. Both precommit.py (which reads state) and gitwork.py
    (which mutates it) go through this.
    """

    def git(
        repo: str,
        *args: str,
        check: bool = False,
        strip: bool = True,
        stdin: str | None = None,
        isolated: bool = False,
    ) -> tuple[int, str, str]:
        """Run a git command in repo. Returns (rc, stdout, stderr).

        check=True turns a non-zero exit into a hard stop -- used for the
        commands whose failure must never be walked past (commit, push, fetch).

        strip=False keeps stdout byte-exact. Required for --porcelain, whose
        first two columns are positional: stripping turns " M path" (modified,
        unstaged) into "M path" (staged), which would then show an empty
        --cached diff for a file that really did change.
        """
        # Fail closed on transport. GIT_TERMINAL_PROMPT=0: a credential prompt
        # would hang a headless run. protocol.ext.allow=never: `ext::` remotes
        # execute a command named in repo-local config, which is code execution
        # from a checked-out repo. protocol.file.allow=user keeps ordinary local
        # remotes working when this tool invokes git directly, while still
        # refusing them when git itself would be following a submodule.
        # Hooks are deliberately NOT disabled -- a repo's hooks are part of how
        # its owner wants commits made, and a rejecting hook is a handled
        # outcome. That matters more here than in most tools: this skill's whole
        # subject is the repo's pre-commit hooks.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        hardening = [
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.file.allow=user",
            # core.fsmonitor runs a program on the FIRST `git status` this tool
            # makes, before any question has been asked. Cleared rather than
            # reported: git falls back to its own implementation, so nothing
            # legitimate is lost.
            "-c",
            "core.fsmonitor=",
            # git's own default, forced because it is repo-local and therefore
            # attacker-settable. With core.quotePath=false git prints raw bytes
            # in a filename -- control characters, bidi overrides -- and every
            # path this tool reads from git flows into a summary the user acts
            # on. clean() is the belt; this is the braces, and it is the layer
            # that stops the byte reaching the decoder at all.
            "-c",
            "core.quotePath=true",
        ]
        if isolated:
            # For a lookup that is purely about a hardcoded upstream URL and has
            # nothing to do with the repository being configured. Local config is
            # attacker-reachable -- a checkout can ship a .git/config carrying
            # url.<base>.insteadOf, http.proxy, core.sshCommand or a credential
            # helper, and the ext:: guard above does nothing about any of them. A
            # redirected catalog URL would hand back an attacker's tags, which
            # only get shape-checked, and Step 4 then clones the same URL as a
            # hook. So: no system config, no global config, and a cwd that is not
            # inside any repository.
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            env["GIT_CONFIG_GLOBAL"] = os.devnull
        # diff.external gets a flag, not a `-c`. Setting it empty does NOT
        # disable it: git then tries to EXEC the empty string and every diff
        # dies with "cannot run :". `--no-ext-diff` is the mechanism that works,
        # and being per-subcommand it is injected here so no call site has to
        # remember it. It matters because an external differ produces the very
        # diff the user approves in Step 5 -- it can fabricate one outright,
        # which inspecting the output afterwards could never detect.
        argv = list(args)
        if argv and argv[0] in ("diff", "show", "log", "format-patch"):
            # --no-textconv as well: textconv is a DIFFERENT mechanism from
            # diff.external, driven by a .gitattributes `diff=<name>` mapping
            # plus a local `diff.<name>.textconv` command. git runs that command
            # and substitutes its output as the file's content -- so it forges
            # the very diff Step 5 asks the operator to approve, and executes
            # code doing it. --no-ext-diff does nothing against it.
            argv[1:1] = ["--no-ext-diff", "--no-textconv"]
        try:
            proc = subprocess.run(
                ["git", "-C", repo, *hardening, *argv],
                env=env,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",  # a non-UTF-8 locale must not crash a git read
                timeout=timeout,
            )
        except FileNotFoundError:
            die("git not found")
        except subprocess.TimeoutExpired:
            die(f"git {' '.join(args)} timed out after {timeout}s")
        # git's stderr can carry text straight from a remote server, so it is
        # neutralised before it is printed or stored, like any other display string.
        err = clean(proc.stderr) if proc.stderr.strip() else ""
        if len(err) > MAX_ERR_LEN:
            err = err[:MAX_ERR_LEN] + " ...(truncated)"
        if check and proc.returncode != 0:
            die(f"git {' '.join(args)} failed (exit {proc.returncode}): {err}")
        out = proc.stdout.strip() if strip else proc.stdout.rstrip("\n")
        return proc.returncode, out, err

    return git


# -- the facts contract ------------------------------------------------------
# Three files touch this JSON: precommit.py writes the config-side sections,
# gitwork.py adds the git-side ones, summary.py reads all of them. These
# TypedDicts are the single description of that shape, so `make verify`'s mypy
# run turns a renamed or mistyped key into an error instead of a silently
# missing row in the summary. Types only -- no behaviour lives here.


class ScanFacts(TypedDict, total=False):
    git_repo: bool
    config: str  # existing | fresh | none
    prev_repos: int
    detected: list[str]  # prose for a human: "markdown (README.md)"
    detected_paths: list[str]  # the bare paths, for anything passed to a command


class Recommendation(TypedDict):
    name: str
    reason: str  # the file that triggered it -- a marker, never a guess


class HooksFacts(TypedDict, total=False):
    added: list[str]
    selected: list[str]  # the catalog keys the user actually chose
    disabled: list[str]  # present catalog entries that look like they never fire
    added_ids: list[str]  # the hook ids this run put in the config
    scoped_ids: list[str]  # of those, the ones with a `files:` filter
    left_as_is: list[str]
    # An entry whose `hooks:` list is a shape the writer cannot extend. The
    # user has to add those hooks by hand, so the summary has to say so --
    # this used to print to stderr and never reach the facts at all.
    needs_manual: list[str]
    recommended: list[Recommendation]
    versions: dict[str, str]


class FilesFacts(TypedDict, total=False):
    written: list[str]
    kept: list[str]


class VerifyFacts(TypedDict, total=False):
    """What `precommit.py verify` observed. `run_ok` is its verdict, not a guess.

    `vacuous` is the trap this exists to catch: `pre-commit run --all-files`
    covers only git-*tracked* files, so in a repo where the setup files are
    still untracked every hook reports "(no files to check) Skipped" and the
    command exits 0 -- a pass that tested nothing.
    """

    scope: str  # all-files | these-files -- what "passed" actually covers
    install: str
    run: str
    run_ok: bool
    vacuous: bool
    autofixed: list[str]
    # Hooks this run ADDED that reported no files to check. A run can be green
    # overall while these were never exercised -- see precommit.skipped_hooks.
    unchecked: list[str]


class PushFacts(TypedDict, total=False):
    """Where a push landed, in pieces. summary.py composes the display."""

    sha: str
    remote: str
    branch: str
    # Set only on a force-push. The summary is the durable record of the run,
    # and without this it read identically whether the push fast-forwarded or
    # rewrote history over commits that no longer exist anywhere.
    forced: bool
    dropped: int


class CommitFacts(TypedDict, total=False):
    choice: str  # commit + push | commit only | not committed
    hash: str
    subject: str
    scope: str
    untouched: str  # "2 other files"
    untouched_files: list[str]  # and which ones -- a count alone cannot be checked
    push: PushFacts


class NetFacts(TypedDict, total=False):
    prev_repos: int
    new_repos: int
    delta: str
    diffstat: str


class PushPlan(TypedDict, total=False):
    """What `push-plan` emits. A superset: `action` says which keys are set."""

    action: str
    branch: str
    remote: str | None  # null on `no-upstream` when the caller must choose
    remotes: list[str]
    remote_url: str  # upstream actions: where a push would land
    remote_urls: dict[str, str]  # no-upstream: one per candidate remote
    merge_ref: str
    upstream_sha: str
    ahead: int
    behind: int
    would_drop: list[str]
    would_add: list[str]
    suspicious_characters: bool
    # Local git config that can run a program or receive push credentials.
    # Reported with the destination so it is weighed before a push, not found
    # out afterwards; see gitwork.risky_local_config.
    local_overrides: list[str]
    # Executable git hooks this skill did not install, which run during its own
    # commit and push. See gitwork.native_hooks.
    native_hooks: list[str]
    error: str
    destination: str  # where a push would land, name and URL, with no verdict
    guidance: str  # one sentence to tell the user; see gitwork.ACTION_GUIDANCE
    permits_push: bool  # whether `push` will attempt anything at all


class RecommendReport(TypedDict, total=False):
    """What `precommit.py --recommend` emits.

    This is the decision table that used to live in SKILL.md as prose. The rule
    "*.md present -> markdownlint" is a scan, not a judgement, so it belongs
    where it can be tested.

    And it is now actually applied: cmd_recommend annotates its payload with
    this type. Declared but never used, it was a type-safety claim wired to
    nothing -- it had already drifted two fields behind the real payload while
    reading as though mypy were watching. A shape nobody checks is worse than
    no shape, because the next reader believes it.
    """

    always_on: list[str]
    recommended: list[Recommendation]
    previous: list[str]  # catalog keys the existing config already carries
    disabled: dict[str, list[str]]  # present, but configured never to fire
    proposed: list[str]  # always_on + recommended, minus previous
    detected: list[str]  # the markers actually seen, for the summary's SCAN row
    detected_paths: list[str]  # the same markers as bare paths, for --files-file
    prev_repos: int
    config: str  # existing | none


class ManagedFile(TypedDict):
    """One file this run wrote, bound to the bytes it was verified as.

    The commit gate compares against `sha256` before staging, so anything that
    rewrote the file between the write and the commit is a refusal rather than
    a silent commit of someone else's content.
    """

    path: str  # repo-relative
    sha256: str


class InternalFacts(TypedDict, total=False):
    """Tool-to-tool handshake values. Never rendered; not a display contract."""

    managed_files: list[ManagedFile]


class Facts(TypedDict, total=False):
    scan: ScanFacts
    hooks: HooksFacts
    files: FilesFacts
    verify: VerifyFacts
    commit: CommitFacts
    net: NetFacts
    notes: list[str]
    internal: InternalFacts
