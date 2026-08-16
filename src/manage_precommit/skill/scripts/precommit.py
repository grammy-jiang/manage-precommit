#!/usr/bin/env python3
"""Build or extend a .pre-commit-config.yaml from the manage-precommit catalog.

Everything here is a decision a program can make correctly every time: which
hooks a repository's contents call for, which catalog entries it already has,
what the latest pinned version is, where a missing hook belongs in the file, and
whether the run that followed actually tested anything. None of it is inferred
from prose, and none of it is re-derived by hand.

Modes (all take --dir REPO, default "."):
  --catalog                          list catalog keys and what each is for
  --detect                           report the existing config as JSON
  --recommend                        what this repo calls for, and why
  --templates-file F --facts-out J   generate/merge, copy assets, record facts
  --verify --facts J                 install the git hook, run it, judge the run

Exit codes:
  0  fine
  1  error
  3  unknown catalog key -- recoverable; re-ask with the near matches printed
  4  a file this run would write already carries an uncommitted change
  5  the existing config uses YAML this tool will not guess at
  6  a version could not be pinned -- nothing was written; `cause` says why
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, NoReturn
from urllib.parse import urlsplit

import config as cfgmod
from hookoutput import is_vacuous, skipped_hooks
from shared import (
    PRECOMMIT_CONFIG_NAME,
    Facts,
    ManagedFile,
    Recommendation,
    RecommendReport,
    atomic_write_bytes,
    bounded_err,
    clean,
    default_file_mode,
    emit,
    has_suspicious_chars,
    is_work_tree,
    make_git,
    porcelain_path,
    preserved_mode,
    read_bytes_nofollow,
    read_bytes_or_die,
    read_json_or_die,
    redact_urls,
    refuse_facts_inside_repo,
    refuse_option_like,
    safe_porcelain,
    write_json_or_die,
)

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(SKILL_DIR, "templates")
ASSETS = os.path.join(SKILL_DIR, "assets")
TARGET_NAME = PRECOMMIT_CONFIG_NAME

EXIT_ERROR = 1
EXIT_UNKNOWN_KEY = 3
EXIT_DIRTY = 4
EXIT_REFUSED = 5
EXIT_PIN_FAILED = 6

# A hook can print a whole file -- gitleaks echoes match context, markdownlint
# lists every violation. The agent reads this to judge the run, so it is bounded
# the way git's stderr is bounded in shared.make_git.
MAX_HOOK_OUTPUT = 20000

# `Any`, deliberately: this is JSON read off disk, so the value types really
# are unknown until a reader checks them -- and every reader here does, with
# .get() and a default. Pretending otherwise with `object` only moves the
# cast to each call site.
CATALOG: dict[str, dict[str, Any]] = {
    "hygiene": {
        "fragment": "hygiene.yaml",
        "rev_repo": "https://github.com/pre-commit/pre-commit-hooks",
        "assets": [],
        "desc": "base hygiene: whitespace, EOF, check-yaml/json, large files, merge conflicts",
    },
    "yamllint": {
        "fragment": "yamllint.yaml",
        "rev_repo": "https://github.com/adrienverge/yamllint",
        "assets": [("yamllint.yaml", ".yamllint.yaml")],
        "desc": "YAML linter (config: .yamllint.yaml)",
    },
    "markdownlint": {
        "fragment": "markdownlint.yaml",
        "rev_repo": "https://github.com/DavidAnson/markdownlint-cli2",
        "assets": [("markdownlint.yaml", ".markdownlint.yaml")],
        # Scoped by a `files:` filter, so "no files to check" means the run was
        # not given a file it matches -- unlike hygiene's hooks, which match
        # anything and are legitimately quiet in a repo with none of that type.
        "file_scoped": True,
        "desc": "Markdown linter (config: .markdownlint.yaml)",
    },
    "mermaid": {
        "fragment": "mermaid.yaml",
        "rev_repo": None,
        "npm": "@mermaid-js/mermaid-cli",
        # The one catalog entry with no rev_repo, so presence is decided by hook
        # id. Named here rather than spelled out at each use: three separate
        # literals meant renaming it in the template would leave present_keys
        # re-offering mermaid forever and verify_written failing every
        # successful write.
        "local_hook_id": "mermaid-lint",
        "assets": [("lint-mermaid.mjs", "scripts/lint-mermaid.mjs")],
        # The fragment hardcodes `entry: node scripts/lint-mermaid.mjs`, so this
        # asset is not data the hook reads -- it is the program the hook runs.
        "executes_assets": True,
        "file_scoped": True,
        "desc": "Mermaid diagram validator (local hook; needs node + a browser)",
    },
    "gitleaks": {
        "fragment": "gitleaks.yaml",
        "rev_repo": "https://github.com/gitleaks/gitleaks",
        "assets": [],
        "desc": "secret scanner",
    },
}

# Repo-independent hygiene. Not up for a vote, and the reason is stated rather
# than assumed: these turn up whatever the project is written in, and the config
# file this skill writes is itself YAML.
ALWAYS_ON = ("hygiene", "yamllint")


def die(msg: str, code: int = EXIT_ERROR, **payload: object) -> NoReturn:
    """Stop, and for a CLASSIFIED exit say why in JSON as well as in English.

    gitwork.py's non-1 exits always emit a machine-checkable object; this file's
    exits 3/4/5 handed the caller a stderr sentence and nothing else -- and
    EXIT_DIRTY covers two different causes (the check itself failed, versus a
    file genuinely being dirty) that SKILL.md could only tell apart by reading
    the prose. A `reason` discriminant is something a program can branch on;
    an English sentence is something an agent has to interpret.
    """
    if payload:
        emit({"ok": False, "exit": code, **payload})
    print(f"precommit: {msg}", file=sys.stderr)
    sys.exit(code)


git = make_git(die)


# -- versions ----------------------------------------------------------------
VER_RE = re.compile(r"^v?\d+(?:\.\d+)*$")


def version_key(tag: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", tag)]


# Long enough for a registry that is slow rather than absent.
NPM_TIMEOUT = 90


# Every value `cause` can take, in one place. SKILL.md turns each into different
# advice and a test reads this tuple against that list, because a cause added
# here without a sentence there fails the way a renamed sentinel does: the agent
# falls through to wording that does not fit, and nothing looks wrong.
PIN_CAUSES = (
    "filesystem",
    "auth",
    "not-found",
    "network",
    "timeout",
    "npm-missing",
    "unrunnable",
    "invalid-version",
    "git-ls-remote",
    "no-version-tags",
    "not-isolated",
    "forbidden",
    "unknown",
)

# And every key the payload can carry, for the same reason. A field the agent is
# never told about is a field it will not read: `registry` was added because a
# 404 from a mirror is not a 404 from npmjs, and that distinction is worth
# nothing if SKILL.md does not name the key that draws it.
PIN_FIELDS = (
    "source",
    "target",
    "cause",
    "npm_code",
    "npm_path",
    "detail",
    "registry",
    "registry_is_public",
)


def scratch_root() -> str:
    """Where a scratch directory would have gone, for when one cannot be made.

    Best effort on purpose: `gettempdir()` raises for the same reason
    `TemporaryDirectory()` just did, and a failure to name the place is not a
    reason to withhold the rest of the report.
    """
    try:
        return tempfile.gettempdir()
    except OSError:  # pragma: no cover - unreachable for the reason below
        return ""


def scratch_or_pin_failed(source: str, target: str) -> tempfile.TemporaryDirectory[str]:
    """A scratch directory, or exit 6 saying the filesystem is why.

    Both pin sources need one before they can start, and an OSError from here
    is neither a stalled remote nor a missing npm -- left alone it was a
    traceback and exit 1 out of `latest_tag`, and `npm-missing` out of
    `npm_latest`, since tempfile raises the same FileNotFoundError a missing
    executable does.

    Untested, and this is the one branch in these scripts that cannot be:
    tempfile walks $TMPDIR, /tmp, /var/tmp, /usr/tmp and then the cwd, so
    reaching it needs every one of them unusable at once, which a test cannot
    arrange without breaking the machine it runs on. Kept because the
    classification is the whole point of it.
    """
    try:
        holder = tempfile.TemporaryDirectory()
    except OSError as exc:  # pragma: no cover - see above
        pin_failed(
            source,
            target,
            f"no scratch directory to pin in: {exc}",
            "filesystem",
            npm_path=clean(scratch_root()),
        )
    seal_scratch(holder.name, source, target)
    return holder


def seal_scratch(where: str, source: str, target: str) -> None:
    """Stop git and npm walking out of the scratch directory into a project.

    Neither can be told to ignore an enclosing one: git discovers the first
    `.git` above cwd and npm the first `package.json`, and a TMPDIR under
    anybody's repository -- not only the one being configured -- therefore hands
    that repository the vote this directory exists to deny it. It can rewrite a
    catalog URL with `url.<other>.insteadOf` or name the registry, and the pin
    that comes back looks like any other.

    Both tools stop at the first marker they find, so the scratch is given its
    own: an empty repository and an empty project, whose configuration is
    nothing. Sealing rather than refusing, because the location is the
    environment's to choose -- `/tmp` is itself inside a git repository on at
    least one machine this was written on -- and a run that can make itself
    isolated has no business demanding the machine be rearranged.

    Fails closed: an unsealed scratch is not one to pin from.
    """
    rc, _, err = git(where, "init", "--quiet", isolated=True)
    if rc != 0:
        pin_failed(source, target, f"could not isolate a scratch directory: {err}", "not-isolated")
    try:
        for name, body in (("package.json", b"{}\n"), (".npmrc", b"")):
            atomic_write_bytes(os.path.join(where, name), body)
    except OSError as exc:  # pragma: no cover - a directory just created
        pin_failed(source, target, f"could not isolate a scratch directory: {exc}", "not-isolated")


def pin_failed(source: str, target: str, msg: str, cause: str, **fields: object) -> NoReturn:
    """Refuse a pin, saying what could not be pinned and why, machine-readably.

    Nothing has been written at this point and nothing will be: every version is
    fetched before the first byte of config, so this exit is always a repository
    left exactly as it was found. That half was already true; what was missing
    is a `cause` the agent can act on. An unwritable cache, a 404 and a dropped
    connection each need a different sentence to the user, and working that out
    from npm's stderr is a guess -- which is the one thing SKILL.md is not
    supposed to ask a model to do.

    Both pin sources exit the same way. `git ls-remote` failing and `npm view`
    failing are the same event to the caller, and a contract that numbered them
    differently would be one more thing to remember and get wrong.
    """
    # Impossible states, asserted rather than relayed: both are contracts with
    # SKILL.md, and a value it has no sentence for is worse than a crash here.
    if cause not in PIN_CAUSES:
        die(f"internal: {cause!r} is not one of the declared pin causes")
    if undeclared := sorted(set(fields) - set(PIN_FIELDS)):
        die(f"internal: undeclared pin fields {undeclared}")
    die(
        msg,
        code=EXIT_PIN_FAILED,
        reason="version_pin_failed",
        source=source,
        target=clean(target),
        cause=cause,
        **fields,
    )


def latest_tag(repo_url: str) -> str:
    """The newest release tag on a hook repository, fetched live.

    Only tags that are purely a version are considered, so a `nightly` or a
    `v2-beta` ref can never be pinned as though it were a release.
    """
    url = refuse_option_like(repo_url, "repo url", die)

    # A remote that stalls past make_git's timeout used to leave through the
    # plain die(): exit 1 with no JSON, losing the exit-6 contract in the case
    # where a caller most wants to be told it was reachability and not their
    # repository. Only the timeout is rerouted -- make_git's other refusal is a
    # missing git, which is a prerequisite this run never had and not a pin that
    # failed. Untested here for the reason the npm timeout is: reaching it costs
    # a real two-minute wait. `make_git` dispatching to the hook at all is
    # tested in test_shared.py, which can fake the clock.
    def stalled(msg: str) -> NoReturn:  # pragma: no cover - see above
        pin_failed("git", url, msg, "timeout")

    # In a scratch directory, with no system or global config: see make_git's
    # `isolated`. Running this under the target repo's config would let that
    # repo decide which server answers for a catalog URL.
    pinning_git = make_git(die, on_timeout=stalled)
    scratch = scratch_or_pin_failed("git", url)
    try:
        with scratch as elsewhere:
            try:
                rc, out, err = pinning_git(
                    elsewhere, "ls-remote", "--tags", "--refs", url, isolated=True
                )
            except OSError as exc:  # pragma: no cover - see below
                # make_git handles a git that is absent and one that hangs; a
                # git that is *there and will not start* -- the wrong bits on
                # the file, a bad interpreter -- comes out of it as a plain
                # OSError. Caught here rather than by the cleanup handler
                # below, which would tell the user their temporary filesystem
                # was at fault and send them to look at the wrong thing
                # entirely.
                #
                # Untested: exec walks PATH past a file it cannot start, so a
                # broken git first on PATH finds the real one behind it, and a
                # PATH holding only the broken one fails at the first git call
                # this run makes, long before pinning.
                pin_failed("git", url, f"could not run git for {repo_url}: {exc}", "unrunnable")
    except OSError as exc:  # pragma: no cover - only the cleanup reaches here
        # With the path: an empty `npm_path` is documented as "no scratch
        # directory could be made", and this one was made and would not go.
        pin_failed(
            "git",
            url,
            f"scratch directory would not go away: {exc}",
            "filesystem",
            npm_path=clean(scratch.name),
        )
    if rc != 0:
        pin_failed(
            "git",
            url,
            f"git ls-remote failed for {repo_url}: {err}",
            "git-ls-remote",
            detail=err,
        )
    tags = []
    for line in out.splitlines():
        if "refs/tags/" not in line:
            continue
        ref = line.split("refs/tags/", 1)[1].strip()
        if VER_RE.match(ref):
            tags.append(ref)
    if not tags:
        pin_failed(
            "git",
            url,
            f"no version tags found for {repo_url}",
            "no-version-tags",
        )
    tags.sort(key=version_key)
    return tags[-1]


# npm's own machine-readable discriminants, not its prose. Classifying by
# English sentence is how an outcome ends up matching no bucket and vanishing;
# these three lines are a stable contract that says the same thing in every
# locale. Both prefixes are matched because npm 8 and earlier print `npm ERR!`
# where npm 9 and later print `npm error`, and a tool that exists to pin
# versions outlives one npm major.
#
# `\S+` and not `npm`, because the word at the front is the `heading` config and
# a user may set it to anything. `--heading=npm` is passed as well; this is the
# half that holds when a flag is not honoured, the way the ANSI strip backs up
# `--no-color`.
NPM_FIELD_RE = re.compile(r"^\S+ (?:ERR!|error) (code|syscall|path) (.+)$", re.M)

# npm colours that prefix, and `color=always` in a user or global .npmrc makes
# it do so into a pipe -- which this deliberately honours, like the rest of
# their npm configuration. The escapes land between `npm` and `error`, so the
# pattern above matches nothing and every classified failure arrives `unknown`.
# `--no-color` on the command line prevents it and this removes what prevention
# missed, because the two together are cheap and the parse decides the advice.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Each cause is a different sentence to the user: a cache they cannot write is
# theirs to fix, a 404 means the package name is wrong, and a network failure is
# worth simply retrying. `unknown` is a bucket on purpose -- an unmatched code
# must still arrive named rather than disappear into the default.
NPM_CAUSES: tuple[tuple[str, frozenset[str]], ...] = (
    ("auth", frozenset({"E401", "ENEEDAUTH", "EAUTHUNKNOWN", "EAUTHIP", "EOTP"})),
    # Not `auth`. npm labels any HTTP failure `E<status>` and special-cases
    # only 401, so a 403 is as likely a corporate registry refusing a package
    # by policy to an account that authenticated perfectly well. "Your
    # credentials are missing" is the wrong sentence for that, and the wrong
    # thing to go and check.
    ("forbidden", frozenset({"E403"})),
    ("not-found", frozenset({"E404"})),
    # Name resolution, then the connect errnos the kernel hands back when the
    # route or the host is not there. All of these exit normally, which is why
    # none of them is a `TimeoutExpired`.
    (
        "network",
        frozenset(
            {
                "ENOTFOUND",
                "ECONNREFUSED",
                "ECONNRESET",
                "ECONNABORTED",
                "EPROTO",
                "EPIPE",
                "ENETUNREACH",
                "ENETDOWN",
                "ENETRESET",
                "EHOSTUNREACH",
                "EHOSTDOWN",
                "EADDRNOTAVAIL",
            }
        ),
    ),
    # npm gave up on a slow socket by itself, and exited normally doing it -- so
    # this never reaches the TimeoutExpired handler, which only fires when the
    # whole command outlives NPM_TIMEOUT. Reported as `network` these were a
    # `timeout` bucket that almost nothing could ever land in, under a code
    # literally named ETIMEDOUT.
    # ETIMEDOUT is the socket's; the four E*TIMEOUTs are @npmcli/agent's own
    # names for giving up on connect, idle, response and transfer. All of them
    # exit normally, so none reaches the TimeoutExpired handler.
    (
        "timeout",
        frozenset(
            {
                "ETIMEDOUT",
                "ESOCKETTIMEDOUT",
                "ERR_SOCKET_TIMEOUT",
                "ECONNECTIONTIMEOUT",
                "EIDLETIMEOUT",
                "ERESPONSETIMEOUT",
                "ETRANSFERTIMEOUT",
            }
        ),
    ),
    # ENOENT belongs here rather than under "missing": npm reports the failure to
    # create its own cache directory that way, which is the whole of issue #16.
    (
        "filesystem",
        frozenset(
            {
                "EACCES",
                "EPERM",
                "EROFS",
                "ENOSPC",
                "EDQUOT",
                "EFBIG",
                "EIO",
                "ELOOP",
                "ENAMETOOLONG",
                "ENOTEMPTY",
                "EEXIST",
                "EXDEV",
                "EBUSY",
                "ETXTBSY",
                "EISDIR",
                "ENOTDIR",
                "ENOENT",
            }
        ),
    ),
)


def npm_scalar(out: str) -> str:
    """npm's answer as a plain string, whether or not it came back as JSON.

    The format is asked for rather than assumed. A `json=true` in a user or
    global .npmrc -- configuration this deliberately honours -- otherwise quotes
    every value npm prints, and the version check then rejects a perfectly good
    lookup as garbage and refuses to write anything at all. Asking for `--json`
    and parsing makes the shape ours instead of theirs, and covers `parseable`
    and `long` in the same move rather than one config at a time.

    Falling back to the raw text, because an npm that does not honour the flag
    should keep working rather than fail differently.
    """
    text = out.strip()
    if not text:
        return ""
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return value if isinstance(value, str) else ""


def npm_fields(stderr: str) -> dict[str, str]:
    """npm's `code`/`syscall`/`path` lines, last one winning."""
    return {key: value.strip() for key, value in NPM_FIELD_RE.findall(ANSI_RE.sub("", stderr))}


def npm_error(stdout: str, stderr: str) -> tuple[dict[str, str], str]:
    """What npm said went wrong: its fields, and its own words.

    Two sources, because npm has two and the user's configuration decides which
    one carries anything. Under `--json` the failure arrives as an object on
    stdout, and that is the copy to trust: the stderr lines are prefixed with
    whatever the `heading` config says -- `npm` by default, but it is a string
    a user may set to anything -- and suppressed altogether by
    `loglevel=silent`. Both are honoured here like the rest of their npm
    configuration, so neither can be assumed, and a pattern anchored on `^npm`
    was assuming both.

    stderr stays as the fallback rather than the source, for an npm whose
    `--json` does not carry the error.
    """
    fields: dict[str, str] = {}
    words = ""
    try:
        payload = json.loads(stdout.strip() or "null")
    except ValueError:
        payload = None
    reported = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(reported, dict):
        for key in ("code", "syscall", "path"):
            value = reported.get(key)
            if isinstance(value, str) and value.strip():
                fields[key] = value.strip()
        words = " ".join(
            reported[key]
            for key in ("summary", "detail")
            if isinstance(reported.get(key), str) and reported[key].strip()
        )
    # Merged, not chosen between. npm's JSON error carries `code` but not
    # `path`, which only ever appears on stderr -- so taking the object whole
    # when it had anything at all dropped the one field SKILL.md tells the agent
    # to name, and dropped it into a meaning: an empty `npm_path` is documented
    # as "the scratch directory could not be made", which is a different failure
    # from a write inside one that was.
    for key, value in npm_fields(stderr).items():
        fields.setdefault(key, value)
    return fields, stderr if stderr.strip() else words


PUBLIC_NPM_HOST = "registry.npmjs.org"
PUBLIC_NPM_SCHEME = "https"
PUBLIC_NPM_PORT = 443


def is_public_registry(url: str) -> bool:
    """Whether that is npm's own registry root, however it happens to be spelled.

    `npm config get registry` returns what the user wrote, so the same registry
    arrives with or without a trailing slash -- and comparing the string in
    SKILL.md then reads npmjs as a company mirror and tells someone a bug in
    this catalog is theirs to go and fix. Whether two URLs name one endpoint is
    a fact, so it is settled here rather than described there.

    Exhaustive by construction, not clause by clause. This grew a rule per
    review round -- hostname, then port, then path, then query and fragment --
    because npm appends the package name to the configured string whole, so
    every part of that string is a part that can make it a different endpoint
    wearing npmjs's name. So each component `urlsplit` produces is named below,
    and anything a future reader adds to that list defaults to "must be empty".

    Userinfo is the one deliberate exception: credentials change who is asking,
    not who answers, and npmjs with a token in front of it is still npmjs. (The
    payload gets that URL redacted; see `redact_urls`.)

    https only. Over plain http nothing authenticates the far end, so a proxy or
    any intermediary can answer for that name -- including with a 404 -- and
    this field's whole job is telling "npmjs said no" apart from "something else
    said no". An unencrypted endpoint cannot support the claim.

    Wrong in the `True` direction is the expensive one -- it sends someone to
    report a bug here about a package their own registry does not carry -- so
    anything unrecognised is `False`.
    """
    try:
        parts = urlsplit(url)
        port = parts.port  # a property, and it raises on a port that is not one
    except ValueError:
        return False
    return (
        parts.scheme == PUBLIC_NPM_SCHEME
        and parts.hostname == PUBLIC_NPM_HOST
        and port in (None, PUBLIC_NPM_PORT)
        and parts.path in ("", "/")
        and not parts.query
        and not parts.fragment
    )


def npm_registry_for(pkg: str) -> str | None:
    """The registry npm would ask for THIS package, or None if it will not say.

    `@scope:registry` routes a scoped package on its own while `registry` still
    reads as npmjs -- and every npm package this catalog pins is scoped. Asking
    only the default therefore names the wrong server in the one field SKILL.md
    uses to decide whether a 404 is the user's mirror or a bug in this catalog,
    and names it confidently.

    None, and not `""`, when the answer cannot be had. An empty string flowed
    into `is_public_registry` and came back False, which reads as "a mirror
    answered" -- inventing the very fact this field exists to supply, in the
    direction that sends someone to fix a registry that may be fine. Nothing
    known is reported as nothing known.
    """
    if pkg.startswith("@") and "/" in pkg:
        scoped = npm_config(f"{pkg.split('/', 1)[0]}:registry")
        if scoped:
            return scoped
    return npm_config("registry")


# node surfaces OpenSSL's certificate-verify strings verbatim, and they are NOT
# recognisable by name: UNABLE_TO_VERIFY_LEAF_SIGNATURE, CRL_HAS_EXPIRED,
# SUBJECT_ISSUER_MISMATCH and half the rest carry no CERT/TLS/SSL token at all.
# A first attempt at "a family, not a list" matched on those three words and
# quietly dropped UNABLE_TO_VERIFY_LEAF_SIGNATURE from `network` into `unknown`,
# which is the failure an intercepting proxy actually produces.
#
# So: the closed list where the list is closed. These come from OpenSSL's
# X509_verify_cert_error_string and change about once a decade, and a code that
# is missed still arrives as `unknown` with npm's own words attached -- an
# unhelpful answer rather than a wrong one.
OPENSSL_VERIFY_CODES = frozenset(
    {
        "UNABLE_TO_GET_ISSUER_CERT",
        "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
        "UNABLE_TO_GET_CRL",
        "UNABLE_TO_GET_CRL_ISSUER",
        "UNABLE_TO_DECRYPT_CERT_SIGNATURE",
        "UNABLE_TO_DECRYPT_CRL_SIGNATURE",
        "UNABLE_TO_DECODE_ISSUER_PUBLIC_KEY",
        "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
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
        "CERT_CHAIN_TOO_LONG",
        "CERT_REVOKED",
        "CERT_UNTRUSTED",
        "CERT_REJECTED",
        "INVALID_CA",
        "INVALID_PURPOSE",
        "PATH_LENGTH_EXCEEDED",
        "HOSTNAME_MISMATCH",
        "SUBJECT_ISSUER_MISMATCH",
        "AKID_SKID_MISMATCH",
        "AKID_ISSUER_SERIAL_MISMATCH",
        "KEYUSAGE_NO_CERTSIGN",
        "UNHANDLED_CRITICAL_EXTENSION",
    }
)

# node's own TLS errors, which unlike OpenSSL's *are* a prefix family and an
# open-ended one -- ERR_TLS_CERT_ALTNAME_INVALID, ERR_SSL_WRONG_VERSION_NUMBER.
# A pattern is right here for the same reason it was wrong above.
TLS_CODE_RE = re.compile(r"^ERR_(TLS|SSL)_")

# getaddrinfo's codes that mean "the name did not resolve", and only those.
# `^EAI_` looked like a family the way `ERR_TLS_*` is one, and it is not: the
# prefix also covers EAI_BADFLAGS, EAI_MEMORY and EAI_OVERFLOW, which are bad
# arguments, no memory and a full buffer -- none of them reachability, and all
# of them told to go and retry the network. Enumerated, like the OpenSSL codes
# and for the same reason: the shared prefix is not a shared meaning.
RESOLVER_CODES = frozenset({"EAI_AGAIN", "EAI_FAIL", "EAI_NONAME", "EAI_NODATA"})


def npm_cause(code: str) -> str:
    for cause, codes in NPM_CAUSES:
        if code in codes:
            return cause
    if code in OPENSSL_VERIFY_CODES or code in RESOLVER_CODES or TLS_CODE_RE.match(code):
        return "network"
    return "unknown"


def npm_latest(pkg: str) -> str:
    name = refuse_option_like(pkg, "npm package", die)
    # Run in a scratch directory so the repository being configured cannot
    # supply an .npmrc. Pinning also sets an explicit cache path in that scratch
    # directory so inherited npm cache settings (for example an unwritable
    # NPM_CONFIG_CACHE) cannot break version lookup.
    #
    # The registry is deliberately NOT forced. The isolation this wants is from
    # the *repository being configured*, and cwd is the whole of that. The
    # user's own npm configuration -- a user or global .npmrc, NPM_CONFIG_REGISTRY,
    # the proxy variables -- is honoured, because on the machines this failure
    # was reported from a mirror or proxy is the only route to a registry at
    # all, and a hardcoded --registry would turn a working environment into a
    # broken one to defend against a threat the user already controls.
    # Outside the handlers below, because it happens before npm is involved at
    # all: tempfile raises the same FileNotFoundError a missing executable does,
    # and reported as `npm-missing` that sends someone off to install something
    # they already have.
    scratch = scratch_or_pin_failed("npm", name)
    try:
        with scratch as elsewhere:
            cache = os.path.join(elsewhere, "npm-cache")
            try:
                out = subprocess.run(
                    # Every part of this is spelled out because each is otherwise
                    # taken from the user's npm configuration, which this honours:
                    # `@latest` because a `tag=next` would pin whatever that
                    # points at, `--json` because `json=true` quotes the answer,
                    # and `--no-color` because `color=always` writes escapes
                    # through the middle of `npm error code`.
                    [
                        "npm",
                        "view",
                        f"{name}@latest",
                        "version",
                        "--cache",
                        cache,
                        "--json",
                        "--no-color",
                        # `loglevel=silent` otherwise leaves nothing on stderr
                        # to fall back to, and nothing to quote to the user.
                        "--loglevel=error",
                        # The word npm puts at the front of every log line, which
                        # is otherwise theirs to choose -- and `path` is only
                        # ever on stderr, so losing the prefix loses the field.
                        "--heading=npm",
                    ],
                    cwd=elsewhere,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=NPM_TIMEOUT,
                )
            except FileNotFoundError:
                # An npm on PATH whose shebang interpreter is gone raises the
                # same FileNotFoundError as no npm at all, and the contract
                # distinguishes them: `npm-missing` says install it,
                # `unrunnable` says the one you have is broken. `which` is what
                # tells them apart, since the exception cannot.
                if shutil.which("npm") is None:
                    pin_failed(
                        "npm", name, f"npm not found; it is needed to pin {pkg}", "npm-missing"
                    )
                pin_failed(
                    "npm",
                    name,
                    f"npm is on PATH but would not start; it is needed to pin {pkg}",
                    "unrunnable",
                )
            except subprocess.TimeoutExpired:  # pragma: no cover - see below
                # Before the generic handler: TimeoutExpired is a SubprocessError,
                # and "could not run npm" is the wrong sentence for a registry
                # that answered too slowly rather than not at all.
                #
                # Untested on purpose. Reaching it costs a real 90-second wait,
                # and the alternative is a test-only override of NPM_TIMEOUT in
                # shipped code -- the first environment knob in these scripts
                # that exists for the suite rather than for a user. A two-line
                # handler is not worth either.
                pin_failed("npm", name, f"npm view {pkg} timed out after {NPM_TIMEOUT}s", "timeout")
            except (OSError, subprocess.SubprocessError) as exc:
                pin_failed("npm", name, f"could not run npm for {pkg}: {exc}", "unrunnable")
    except OSError as exc:  # pragma: no cover - only the cleanup reaches here
        # See latest_tag: the directory existed, so it is named.
        pin_failed(
            "npm",
            name,
            f"scratch directory would not go away: {exc}",
            "filesystem",
            npm_path=clean(scratch.name),
        )
    if out.returncode != 0:
        fields, words = npm_error(out.stdout, out.stderr)
        code = fields.get("code", "")
        # Bound once and used twice. npm's stderr is a registry's text, the
        # same category as git's remote-server text, and SKILL.md relays the
        # sentence as well as the field.
        detail = bounded_err(words)
        cause = npm_cause(code)
        extra: dict[str, object] = {}
        if cause == "not-found":
            # Which registry said no, because honouring the user's own is a
            # decision this file made (see below) and a mirror that does not
            # carry a package answers E404 exactly like a wrong package name.
            # Asked only here: it costs a subprocess, and only this one cause
            # cannot be acted on without knowing.
            registry = npm_registry_for(name)
            # Redacted for the payload only. npm returns the registry exactly
            # as configured, credentials and all, and SKILL.md hands this field
            # to the agent -- classification below keeps the whole URL.
            extra["registry"] = clean(redact_urls(registry or ""))
            # Omitted rather than guessed when npm would not say which registry
            # it asked. A `False` here is a claim, and SKILL.md acts on it.
            if registry:
                extra["registry_is_public"] = is_public_registry(registry)
        pin_failed(
            "npm",
            name,
            f"npm view {pkg} failed: {detail}",
            cause,
            npm_code=code,
            npm_path=clean(fields.get("path", "")),
            detail=detail,
            **extra,
        )
    version = npm_scalar(out.stdout)
    if not VER_RE.match(version):
        answered = bounded_err(version)
        pin_failed(
            "npm",
            name,
            f"npm returned an unexpected version for {pkg}: {answered!r}",
            "invalid-version",
            detail=answered,
        )
    return version


# -- repository scan ---------------------------------------------------------
# Bounded on purpose: the scan is a recommendation input, not an inventory, and
# an unbounded walk of someone's monorepo is a hang rather than an answer.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "dist",
        "build",
        "target",
        "vendor",
    }
)
MAX_SCAN_DEPTH = 3
MAX_SCAN_FILES = 4000
MAX_MERMAID_PROBES = 200
MAX_PROBE_BYTES = 200_000  # a probe is a look for one fence, not a file read
MERMAID_FENCE = re.compile(r"^\s*(?:```+|~~~+)\s*mermaid\b", re.MULTILINE)


def walk_repo(directory: str) -> list[str]:
    """Repo-relative paths, depth- and count-bounded, in a stable order."""
    found: list[str] = []
    root_depth = directory.rstrip(os.sep).count(os.sep)
    for base, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        if base.count(os.sep) - root_depth >= MAX_SCAN_DEPTH:
            dirs[:] = []
        for name in sorted(files):
            found.append(os.path.relpath(os.path.join(base, name), directory))
            if len(found) >= MAX_SCAN_FILES:
                return found
    return found


def prerequisites() -> dict[str, str]:
    """External tools a catalog entry needs, and whether they are here.

    Computed rather than probed by the caller: SKILL.md used to tell the agent
    to run `command -v npm && command -v node` and interpret the output, which
    is a shell command and a judgement for something shutil.which answers
    exactly. One fewer bash block in the body, and one fewer thing to get wrong.

    `binaries present` and not `present`, because that is all this proves: the
    two executables are on PATH. Whether the version pin can reach the registry
    is settled in Step 3 and nowhere else. SKILL.md branches on this string
    literally, so it is pinned by a test rather than by intent.
    """
    missing = sorted(tool for tool in ("npm", "node") if shutil.which(tool) is None)
    return {"mermaid": "missing: " + ", ".join(missing) if missing else "binaries present"}


def detect_markers(directory: str) -> tuple[list[Recommendation], list[str], list[str]]:
    """Which catalog entries the repo's contents call for, and the file that says so.

    The `reason` is always a path that was actually seen, never a category. A
    recommendation the user cannot check is one they have to take on faith.
    """
    paths = walk_repo(directory)
    markers: list[str] = []
    trigger_paths: list[str] = []
    recs: list[Recommendation] = []

    markdown = [p for p in paths if p.lower().endswith((".md", ".markdown"))]
    # The FIRST one that is a real regular file, not simply the first one. This
    # path goes into trigger_paths, which SKILL.md writes to a file and passes
    # as `--files-file` -- so `pre-commit run --files` points the autofixing
    # hooks at it. A tracked `notes.md -> ~/.ssh/id_rsa` picked as the trigger
    # gets that file rewritten, and gitleaks reads and prints it. The mermaid
    # probe below already refuses to READ through a symlink; naming one as the
    # target was the same threat with the guard missing.
    safe_markdown = [p for p in markdown if not os.path.islink(os.path.join(directory, p))]
    if safe_markdown:
        recs.append({"name": "markdownlint", "reason": clean(safe_markdown[0])})
        markers.append(f"markdown ({clean(safe_markdown[0])})")
        trigger_paths.append(safe_markdown[0])
    if markdown:
        for rel in markdown[:MAX_MERMAID_PROBES]:
            # Through the same guarded reader as everything else. walk_repo
            # lists symlinks (git tracks them as ordinary blobs), so a tracked
            # `notes.md -> ~/.ssh/id_rsa` would otherwise be opened and scanned
            # during --recommend -- the first, unconfirmed step -- and a named
            # pipe at a .md path would block forever. SymlinkRefused,
            # NotARegularFile and TooLarge all subclass OSError, so an
            # unreadable candidate is skipped exactly as before.
            try:
                raw = read_bytes_nofollow(os.path.join(directory, rel), MAX_PROBE_BYTES)
            except OSError:
                continue
            text = raw.decode("utf-8", "replace")
            if MERMAID_FENCE.search(text):
                recs.append({"name": "mermaid", "reason": clean(rel)})
                markers.append(f"mermaid fence ({clean(rel)})")
                trigger_paths.append(rel)
                break

    # Offered for every repo: a secret scan is not conditional on what the tree
    # happens to contain today.
    recs.append({"name": "gitleaks", "reason": "any repo -- secret scan"})
    return recs, markers, trigger_paths


# -- config helpers ----------------------------------------------------------
def target_path(directory: str) -> str:
    return os.path.join(directory, TARGET_NAME)


def read_config(directory: str) -> cfgmod.Config | None:
    """Scan the existing config, or None when there is not one."""
    path = target_path(directory)
    if not os.path.lexists(path):
        return None
    if os.path.islink(path):
        die(f"{path} is a symlink -- refusing to follow it")
    raw = read_bytes_or_die(path, die)
    try:
        # utf-8-sig: identical to utf-8 with no BOM, and strips exactly the BOM
        # when there is one. Without this a file saved as "UTF-8 with BOM" has
        # the mark folded into its first key, so `repos:` reads as a different
        # key entirely and the config is refused for having no `repos:` at all.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        die(f"{path} is not valid UTF-8: {exc}")
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        scanned = cfgmod.scan(text)
        scanned.had_bom = had_bom
        return scanned
    except cfgmod.ConfigRefused as exc:
        die(
            f"{exc}. This tool only extends configs whose shape it can prove it "
            "understands; add the hook by hand, or simplify the construct.",
            code=EXIT_REFUSED,
            reason="config-refused",
            line=exc.line_no,
        )


# The stages at which `pre-commit run` fires a hook in the ordinary flow.
# `commit` is the pre-3.0 spelling of `pre-commit`. Everything else -- push,
# manual, commit-msg, post-commit -- means the hook does not scan content on an
# ordinary commit, however much its presence suggests otherwise.
RUNS_ON_COMMIT = frozenset({"commit", "pre-commit"})


def parse_stages(raw: str) -> set[str]:
    """The stage names in a `stages:` value, flow or block form.

    Membership, not a substring test: "commit" is a substring of "commit-msg",
    so a hook restricted to commit-msg -- which never scans content on an
    ordinary commit -- read as running on every one.
    """
    return {part.strip().strip("'\"") for part in raw.strip("[]").split(",") if part.strip()}


def looks_disabled(hook: cfgmod.Hook) -> str | None:
    """What would stop this hook running, or None.

    "Already present" was decided on the hook id alone, so an entry could carry
    the right id and never fire: `stages: [manual]` keeps it off the commit
    path, and a hook-level `files:`/`exclude:` can match nothing. The tool then
    counted the catalog entry as covered, stopped offering it, and the user was
    told a secret scan was in force that was not.
    """
    settings = hook.settings
    stages = settings.get("stages", "")
    if stages and not (parse_stages(stages) & RUNS_ON_COMMIT):
        return f"stages: {stages}"
    # always_run: true makes the hook fire whatever files: and exclude: say,
    # which is exactly why config.py captures it. Reading stages first is
    # deliberate -- always_run does not put a hook back on a stage it was
    # excluded from.
    if settings.get("always_run", "").lower() in ("true", "yes", "on"):
        return None
    if settings.get("exclude"):
        return f"exclude: {settings['exclude']}"
    if settings.get("files"):
        return f"files: {settings['files']}"
    return None


def disabled_hooks(cfg: cfgmod.Config, key: str) -> list[str]:
    """Present hooks for `key` carrying something that stops them running.

    For a catalog entry identified by hook id rather than repo URL -- mermaid is
    the only one -- the id must be matched too. `repo: local` is a bucket
    anybody's hooks can sit in, so matching the URL alone attributed every
    disabled local hook in the file to mermaid. That is the mirror image of the
    false-coverage bug this function exists to catch: instead of calling a dead
    hook live, it calls somebody else's dead hook ours.
    """
    meta = CATALOG[key]
    url = meta.get("rev_repo") or "local"
    wanted_id = meta.get("local_hook_id") if not meta.get("rev_repo") else None
    out = []
    for entry in cfg.repos:
        if entry.url != url:
            continue
        for hook in entry.hooks:
            if wanted_id is not None and hook.id != wanted_id:
                continue
            why = looks_disabled(hook)
            if why:
                out.append(f"{clean(hook.id)} ({clean(why)})")
    return out


def exclude_pattern(cfg: cfgmod.Config) -> str | None:
    """The config's top-level `exclude:` value, or None when it has none.

    Surfaced because a broad one silently switches every hook off, and being
    pre-existing it shows up in no diff this run produces.
    """
    value = cfgmod.top_level_scalar(cfg, "exclude")
    return clean(value) if value is not None else None


def present_keys(cfg: cfgmod.Config | None) -> list[str]:
    """Catalog keys the config already carries."""
    if cfg is None:
        return []
    urls = {e.url for e in cfg.repos}
    local_ids = cfg.local_hook_ids()
    have = []
    for key, meta in CATALOG.items():
        if (meta.get("rev_repo") and meta["rev_repo"] in urls) or (
            meta.get("local_hook_id") and meta["local_hook_id"] in local_ids
        ):
            have.append(key)
    return have


def load_fragment(key: str) -> tuple[str, cfgmod.RepoEntry, str | None]:
    """The catalog fragment's text, its parsed entry, and the version pinned.

    The fragment is *text*, and stays text all the way into the file: the
    placeholders are substituted and the block is inserted verbatim, so the
    comment above each entry and the exact formatting a human wrote survive.
    Parsing it is only to learn its url and hook ids -- and to prove, on every
    run, that our own templates are a shape the scanner accepts.
    """
    meta = CATALOG[key]
    path = os.path.join(TEMPLATES, meta["fragment"])
    text = read_bytes_or_die(path, die).decode("utf-8")
    version: str | None = None
    if meta.get("rev_repo"):
        version = latest_tag(meta["rev_repo"])
        text = text.replace("__REV__", version)
    if meta.get("npm"):
        version = npm_latest(meta["npm"])
        text = text.replace("__NPM__", version)
    if "__REV__" in text or "__NPM__" in text:
        die(f"catalog fragment {meta['fragment']} still has an unfilled placeholder")
    try:
        parsed = cfgmod.scan("repos:\n" + text)
    except cfgmod.ConfigRefused as exc:
        die(f"catalog fragment {meta['fragment']} is malformed: {exc}")
    if len(parsed.repos) != 1:
        die(f"catalog fragment {meta['fragment']} must declare exactly one repo entry")
    return text, parsed.repos[0], version


def fragment_hook_blocks(text: str, entry: cfgmod.RepoEntry, wanted: set[str]) -> list[list[str]]:
    """The lines of each named hook in a fragment, at the fragment's own indent."""
    lines = ("repos:\n" + text).splitlines()
    return [lines[h.start : h.end + 1] for h in entry.hooks if h.id in wanted]


# -- generate ----------------------------------------------------------------
def read_templates_file(path: str) -> list[str]:
    """The user's selection, one catalog key per line.

    A file rather than argv: these names are free text the user typed, and free
    text must never reach a command line. An unknown name is a recoverable
    error -- the caller re-asks, quoting the near matches printed here.
    """
    raw = read_bytes_or_die(path, die).decode("utf-8", "replace")
    keys: list[str] = []
    for line in raw.splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if name not in keys:
            keys.append(name)
    if not keys:
        die(f"no catalog keys in {path}")
    unknown = [k for k in keys if k not in CATALOG]
    if unknown:
        for name in unknown:
            near = difflib.get_close_matches(name, list(CATALOG), n=3, cutoff=0.6)
            hint = f" -- did you mean {', '.join(near)}?" if near else ""
            print(f"precommit: {clean(name)!r} is not a catalog key{hint}", file=sys.stderr)
        die(
            f"unknown catalog key(s): {', '.join(clean(u) for u in unknown)}",
            code=EXIT_UNKNOWN_KEY,
            reason="unknown-key",
            unknown=[clean(u) for u in unknown],
        )
    return keys


def files_this_run_would_write(directory: str, keys: list[str]) -> list[str]:
    """Every repo-relative path the run may create, config first."""
    paths = [TARGET_NAME]
    for key in keys:
        for _src, rel in CATALOG[key].get("assets", []):
            if not os.path.lexists(os.path.join(directory, rel)):
                paths.append(rel)
    return paths


def refuse_if_dirty(directory: str, paths: list[str]) -> None:
    """Stop when a file this run would write already has an uncommitted change.

    Without this the run would merge into the user's in-progress edit and then
    have no honest way to commit it: the whole file would be the diff, and part
    of it would be theirs. Their edit is theirs to commit, stash or discard --
    and then the run starts again from the scan.
    """
    if not is_work_tree(git, directory):
        return  # not a work tree: nothing is tracked, so nothing can be pending
    # A failure here is NOT "found nothing dirty" -- returning would merge into
    # whatever the user had in progress, which is the one thing this function
    # exists to prevent. safe_porcelain owns that policy; the exit code is ours,
    # so it goes in through a die that closes over EXIT_DIRTY.
    out = safe_porcelain(
        git,
        directory,
        paths,
        lambda msg: die(msg, code=EXIT_DIRTY, reason="check-failed"),
        what="whether this run's files are already modified",
    )
    dirty = [ln[3:].strip() for ln in out.splitlines() if ln[:2] != "??" and ln[3:].strip()]
    if dirty:
        listed = ", ".join(clean(d) for d in dirty)
        die(
            f"{listed} already carries an uncommitted change. This run would have to "
            "commit your edit along with its own work, so it stops here. Commit, stash "
            "or discard that change, then start again from the scan.",
            code=EXIT_DIRTY,
            reason="dirty",
            paths=[clean(d) for d in dirty],
        )


def plan(
    cfg: cfgmod.Config, keys: list[str], *, pre_existing: bool
) -> tuple[list[cfgmod.Insertion], list[tuple[str, str]], dict[str, str], dict[str, set[str]]]:
    """Work out every insertion, without touching the file.

    `pre_existing` says whether the config came from the user or from our own
    skeleton, which is the difference between "you already had an `exclude`, so
    .gitignore is not covered" and a note about a line we just wrote ourselves.
    """
    insertions: list[cfgmod.Insertion] = []
    # Per key, the hook ids this run intends to put in the file. verify_written
    # checks exactly these afterwards: the ids already present need no proving,
    # and a needs-manual key deliberately inserts nothing, so demanding its ids
    # would fail a documented exit-0 outcome.
    intended: dict[str, set[str]] = {}
    # (tag, prose). The tag is the classification; the prose is for the human.
    # Deriving one from the other by substring-matching display text is how two
    # real outcomes -- hooks added to an existing entry, and a hooks: list this
    # tool cannot extend -- went missing from the facts entirely while still
    # printing to stderr.
    report: list[tuple[str, str]] = []
    versions: dict[str, str] = {}

    seq_indent = cfg.repos_seq_indent if cfg.repos_seq_indent is not None else 2
    if cfg.repos_key_line is None:  # scan() guarantees otherwise
        die("internal check failed: config has no `repos:` key")
    append_at = (cfg.repos_end + 1) if cfg.repos_end is not None else cfg.repos_key_line + 1

    # Top matter first, so it lands above `repos:` whatever follows.
    for key, value in (("minimum_pre_commit_version", '"4.0.0"'), ("exclude", r"'^\.gitignore$'")):
        if key not in cfg.top_keys:
            insertions.append(
                cfgmod.Insertion(at=cfg.repos_key_line, block=[f"{key}: {value}"], what=key)
            )
            report.append(("added", f"added {key}"))
        elif key == "exclude" and pre_existing:
            # The VALUE, not just the fact that one exists. A config arriving
            # with `exclude: '.*'` makes every hook match zero files -- the
            # newly added gitleaks entry still shows up in the diff, so the run
            # reads as a success while nothing is actually being scanned. The
            # line is pre-existing, so it appears in no inserted hunk and the
            # Step 5 diff never shows it either.
            pattern = exclude_pattern(cfg) or ""
            report.append(
                (
                    "kept",
                    f"exclude: left as-is (pattern: {pattern or '?'}) -- .gitignore is NOT "
                    "excluded unless you add it yourself, and anything this pattern matches "
                    "is skipped by EVERY hook, including the ones just added",
                )
            )

    # Adopt the file's own sequence-indentation convention; see config.hook_delta.
    want_delta = cfgmod.observed_hook_delta(cfg)

    for key in keys:
        text, entry, version = load_fragment(key)
        if version:
            versions[key] = version
        block = cfgmod.render_entry(text, entry, seq_indent, want_delta)

        if entry.url == "local":
            have = cfg.local_hook_ids()
            missing = [h.id for h in entry.hooks if h.id not in have]
            if not missing:
                ids = ", ".join(h.id for h in entry.hooks)
                report.append(("kept", f"local ({ids}): already present"))
                continue
            insertions.append(cfgmod.Insertion(at=append_at, block=block, what=f"local:{key}"))
            intended[key] = set(missing)
            report.append(("added", f"local: added ({', '.join(missing)})"))
            continue

        existing = cfg.repo(entry.url)
        if existing is None:
            insertions.append(cfgmod.Insertion(at=append_at, block=block, what=entry.url))
            intended[key] = {h.id for h in entry.hooks}
            report.append(("added", f"{entry.url}: added (rev {entry.rev})"))
            continue

        have_ids = cfg.hook_ids(entry.url)
        missing = [h.id for h in entry.hooks if h.id not in have_ids]
        if not missing:
            disabled = disabled_hooks(cfg, key)
            note = (
                f" -- but {', '.join(disabled)}: present, and looks like it will NOT run on commit"
                if disabled
                else ""
            )
            report.append(
                (
                    "kept",
                    f"{clean(entry.url)}: already present "
                    f"(rev {clean(existing.rev) if existing.rev else '?'}){note}",
                )
            )
            continue
        if not existing.hooks or existing.hook_item_indent is None:
            report.append(
                (
                    "needs-manual",
                    f"{clean(entry.url)}: present "
                    f"(rev {clean(existing.rev) if existing.rev else '?'}) but its `hooks:` "
                    f"list is not a shape this tool can extend -- "
                    f"add {', '.join(missing)} by hand",
                )
            )
            continue
        shift = existing.hook_item_indent - (entry.hook_item_indent or 0)
        merged: list[str] = []
        for chunk in fragment_hook_blocks(text, entry, set(missing)):
            merged.extend(cfgmod.reindent("\n".join(chunk), shift).split("\n"))
        insertions.append(
            cfgmod.Insertion(at=existing.hooks[-1].end + 1, block=merged, what=f"{entry.url}:hooks")
        )
        intended[key] = set(missing)
        report.append(
            (
                "added",
                f"{clean(entry.url)}: present "
                f"(rev {clean(existing.rev) if existing.rev else '?'}) -- added hooks "
                f"{', '.join(missing)}",
            )
        )

    return insertions, report, versions, intended


def merge_same_position(insertions: list[cfgmod.Insertion]) -> list[cfgmod.Insertion]:
    """Fold insertions sharing a line into one, keeping the order they were added.

    apply_insertions splices from the bottom up, which would otherwise reverse
    two blocks planned for the same position.
    """
    merged: dict[int, cfgmod.Insertion] = {}
    for ins in insertions:
        if ins.at in merged:
            merged[ins.at].block.extend(ins.block)
            merged[ins.at].what += f", {ins.what}"
        else:
            merged[ins.at] = cfgmod.Insertion(at=ins.at, block=list(ins.block), what=ins.what)
    return list(merged.values())


def refuse_path_escaping_repo(directory: str, rel: str) -> str:
    """Resolve an asset destination, refusing anything that leaves the repo.

    This is the one write that does not go to a fixed filename in the repo root:
    the mermaid asset lands at ``scripts/lint-mermaid.mjs``. `os.makedirs` and
    `shutil.copyfile` both FOLLOW a symlink -- at the final component and at
    every intermediate one -- so a repo that ships a ``scripts`` symlink
    pointing anywhere writable turns this into an arbitrary file write.

    That matters more than it first looks, because the path to it is not
    exotic: a Markdown file containing a ```mermaid fence is enough for
    detect_markers to recommend the entry, and the write happens in Step 3 --
    before any diff, commit or push confirmation. So the check is refuse, not
    follow, matching read_config's treatment of a symlinked config.
    """
    root = os.path.realpath(directory)
    dest = os.path.join(directory, rel)
    # Every component from the repo root down, not just the parent: a symlink
    # anywhere along the way redirects the write just as effectively.
    walked = directory
    for part in rel.split(os.sep)[:-1]:
        walked = os.path.join(walked, part)
        if os.path.islink(walked):
            die(
                f"{os.path.join(rel)}: {part!r} is a symlink -- refusing to write through it. "
                "Remove or replace it, then run again."
            )
    resolved = os.path.realpath(os.path.dirname(dest))
    if resolved != root and not resolved.startswith(root + os.sep):
        die(f"{rel} resolves outside the repository -- refusing to write it")
    return dest


def foreign_assets(keys: list[str], directory: str) -> list[str]:
    """Executed assets already present whose content is not what we ship.

    Separated from copy_assets so it can run before anything is written: the
    answer decides whether the run may proceed at all.
    """
    found = []
    for key in keys:
        if not CATALOG[key].get("executes_assets"):
            continue
        for src, rel in CATALOG[key].get("assets", []):
            dest = refuse_path_escaping_repo(directory, rel)
            if not os.path.lexists(dest):
                continue
            existing = read_bytes_or_die(dest, die)
            if hashlib.sha256(existing).hexdigest() != sha256_of(os.path.join(ASSETS, src)):
                found.append(rel)
    return found


def copy_assets(key: str, directory: str) -> tuple[list[str], list[str], list[str]]:
    """Copy a catalog entry's repo-side files, never over one already there.

    Read and written through the same guarded helpers as every other file the
    skill touches: read_bytes_or_die refuses to follow a symlink or read a
    FIFO, and atomic_write_bytes replaces the destination rather than writing
    through it. shutil.copyfile did neither.
    """
    written, kept, foreign = [], [], []
    for src, rel in CATALOG[key].get("assets", []):
        dest = refuse_path_escaping_repo(directory, rel)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # lexists, not exists: a dangling symlink is still something the user
        # put there, and following it to write is exactly what is refused.
        if os.path.lexists(dest):
            # An asset that is already there is NOT automatically ours. The
            # mermaid fragment hardcodes `entry: node scripts/lint-mermaid.mjs`,
            # so a repo shipping its own file at that path gets it wired up as
            # a hook and executed on every commit -- while `kept` files are
            # unchanged on disk, so git status and the Step 5 diff show
            # nothing at all. Compare it to what we would have written.
            # Only for an asset the config will EXECUTE. Keeping a linter
            # config the user has customised is the documented behaviour and
            # exactly what they want; keeping a *program* we are about to wire
            # up as a hook is another matter entirely.
            if CATALOG[key].get("executes_assets"):
                existing = read_bytes_or_die(dest, die)
                if hashlib.sha256(existing).hexdigest() != sha256_of(os.path.join(ASSETS, src)):
                    foreign.append(rel)
            kept.append(rel)
            continue
        payload = read_bytes_or_die(os.path.join(ASSETS, src), die)
        atomic_write_bytes(dest, payload, mode=default_file_mode())
        written.append(rel)
    return written, kept, foreign


def load_facts_if_present(path: str) -> dict[str, Any]:
    """Whatever is already in the facts file, or {} when there is nothing yet.

    Step 1 may have seeded it with what the scan detected; the write step adds
    to that rather than replacing it, so a run that skipped --recommend still
    works and one that used it keeps those rows.
    """
    if not os.path.lexists(path):
        return {}
    return read_json_or_die(path, die)


def sha256_of(path: str) -> str:
    return hashlib.sha256(read_bytes_or_die(path, die)).hexdigest()


def verify_written(
    path: str, keys: list[str], after: cfgmod.Config, expected_ids: dict[str, set[str]]
) -> None:
    """Every selected catalog entry really is in the file that was just written.

    The HOOK IDS, not just the repo URL. The commonest merge is inserting a few
    missing hook ids into a repo entry whose URL the file already carries --
    hygiene has seven ids under one URL, and a repo already using a subset hits
    this every time. Checking the URL alone passed trivially in exactly that
    case, so a splice that dropped or misplaced the new hook block still
    reported success, and SKILL.md Step 3 promises the opposite: the merged file
    is re-scanned and every selected entry confirmed present.
    """
    urls = {e.url for e in after.repos}
    for key in keys:
        meta = CATALOG[key]
        url = meta.get("rev_repo")
        # mermaid is the one entry with no rev_repo: it lives under `repo:
        # local`, so its ids come from the local bucket rather than a URL.
        present = (after.hook_ids(url) if url in urls else set()) if url else after.local_hook_ids()
        missing = sorted(expected_ids.get(key, set()) - present)
        if missing:
            die(
                f"{path} was written but {key} is not in it "
                f"(missing: {', '.join(clean(m) for m in missing)}); "
                "refusing to report success"
            )


def normalise_empty_repos(cfg: cfgmod.Config, lines: list[str]) -> tuple[list[str], bool]:
    """Turn `repos: []` into a block-sequence key, proving nothing else moved.

    This is the ONE line the tool rewrites rather than inserts around: a flow
    empty list cannot be appended to as a block sequence. Its content is fully
    known, it carries no user data, and it happens only when the list is empty.
    """
    if not cfg.empty_repos or cfg.repos_key_line is None:
        return lines, False
    baseline = list(lines)
    match = cfgmod.EMPTY_REPOS_LINE.match(baseline[cfg.repos_key_line])
    if match is None:
        die("internal check failed: `repos: []` did not match its own pattern")
    baseline[cfg.repos_key_line] = f"{match.group('indent')}repos:"
    changed = [i for i, (a, b) in enumerate(zip(lines, baseline, strict=True)) if a != b]
    if changed != [cfg.repos_key_line] or len(baseline) != len(lines):
        die("internal check failed: normalising `repos: []` changed more than that line")
    return baseline, True


def cmd_generate(args: argparse.Namespace) -> int:
    directory = args.dir
    refuse_facts_inside_repo(directory, args.facts_out, die)
    keys = read_templates_file(args.templates_file)

    will_write = files_this_run_would_write(directory, keys)
    refuse_if_dirty(directory, will_write)

    existing = read_config(directory)
    if existing is not None and not args.force:
        die(f"{target_path(directory)} exists -- re-run with --force to update it")
    prev_keys = present_keys(existing)
    prev_repos = len(existing.repos) if existing else 0

    if existing is None:
        base = read_bytes_or_die(os.path.join(TEMPLATES, "base.yaml"), die).decode("utf-8")
        cfg = cfgmod.scan(base)
    else:
        cfg = existing

    baseline, rewrote_empty = normalise_empty_repos(cfg, list(cfg.lines))
    # Once: plan() fetches every pinned version over the network.
    planned, report, versions, intended = plan(cfg, keys, pre_existing=existing is not None)
    insertions = merge_same_position(planned)
    result = cfgmod.apply_insertions(baseline, insertions)
    try:
        cfgmod.verify_additive(baseline, result, insertions)
    except cfgmod.ConfigRefused as exc:
        die(str(exc))

    text = cfg.newline.join(result) + (cfg.newline if cfg.ends_with_newline else "")
    if cfg.had_bom:
        text = "\ufeff" + text
    # Prove the result is still a config this tool can read, BEFORE it is
    # written: a merge that produced something unscannable would otherwise be
    # discovered by the next run, on the user's file.
    try:
        after = cfgmod.scan(text)
    except cfgmod.ConfigRefused as exc:
        die(f"the merged config did not scan back cleanly ({exc}); nothing was written")
    # Scoped to the blocks this run inserts. Scanning the whole file made a
    # single pre-existing character -- a zero-width joiner, which is what holds
    # a compound emoji together in an ordinary comment -- refuse every future
    # run permanently, with no line number and no entry in the error table.
    # What this check is for is that WE never introduce one; a character the
    # user already had is theirs, and `--detect` reports it rather than
    # blocking on it.
    inserted = "\n".join(line for ins in insertions for line in ins.block)
    if has_suspicious_chars(inserted):
        die("the blocks this run would insert hold control or text-reordering characters")

    # Everything that can still refuse runs BEFORE the write. The config being
    # produced here wires `entry: node scripts/lint-mermaid.mjs`, so discovering
    # a foreign file at that path *after* writing would leave exactly the
    # half-applied state this skill otherwise refuses: a live config pointing at
    # someone else's program, on a run that reported failure.
    foreign = foreign_assets(keys, directory)
    if foreign:
        # Refused rather than reported: this run has just written a config that
        # tells pre-commit to EXECUTE these files. Reporting a fact the user
        # has to act on correctly, in a step whose output they may skim, is not
        # good enough when the failure mode is running someone else's code on
        # every commit.
        listed = ", ".join(clean(f) for f in foreign)
        die(
            f"{listed} already exists and is NOT the file this skill ships. The config "
            "would run it as a hook on every commit, and because the file is unchanged "
            "it would appear in no diff. Move or delete it and run again, or drop the "
            "entry that needs it. Nothing has been written."
        )
    # A corrupt one would otherwise be discovered after the repo was mutated.
    existing_out = load_facts_if_present(args.facts_out) if args.facts_out else {}

    path = target_path(directory)
    atomic_write_bytes(path, text.encode("utf-8"), mode=preserved_mode(path))

    written, kept, late_foreign = [TARGET_NAME], [], []
    for key in keys:
        wrote, keep, alien = copy_assets(key, directory)
        written.extend(wrote)
        kept.extend(keep)
        late_foreign.extend(alien)
    if late_foreign:
        # foreign_assets() gates the write, but the file can be planted in the
        # window between that check and this one -- and the `kept` branch would
        # then leave it wired as `entry: node ...` on a run reporting success.
        # copy_assets already computes the answer; discarding it reopened
        # exactly the hole the pre-check closed.
        listed = ", ".join(clean(f) for f in late_foreign)
        die(
            f"{listed} appeared between the pre-check and the write, and is NOT the "
            "file this skill ships. The config has been written and would run it as a "
            "hook; remove that file and re-run before committing anything."
        )

    verify_written(path, keys, after, intended)
    # Hashed from disk, not from the string in memory: what the commit step
    # stages is the file, so the file is what gets bound to these facts.
    managed: list[ManagedFile] = [
        {"path": rel, "sha256": sha256_of(os.path.join(directory, rel))} for rel in written
    ]

    # Exact, and free: the difference between what the file held before and what
    # it holds now. Re-reading the fragments would fetch every pinned version a
    # second time.
    before_ids = {h.id for e in (existing.repos if existing else []) for h in e.hooks}
    added_ids = sorted({h.id for e in after.repos for h in e.hooks} - before_ids)
    # Of those, the ones that are scoped by a `files:` filter. A hygiene hook
    # reporting "no files to check" is ordinary -- there is simply no JSON in
    # the repo. A markdownlint or mermaid hook doing it means the .md that
    # caused it to be recommended never reached the run.
    scoped_ids = sorted(
        {
            hook.id
            for key in keys
            if CATALOG[key].get("file_scoped")
            for entry in after.repos
            if entry.url == (CATALOG[key].get("rev_repo") or "local")
            for hook in entry.hooks
        }
        & set(added_ids)
    )
    added = [text for tag, text in report if tag == "added"]
    left = [text for tag, text in report if tag == "kept"]
    needs_manual = [text for tag, text in report if tag == "needs-manual"]
    facts: Facts = {
        "scan": {
            "git_repo": is_work_tree(git, directory),
            "config": "existing" if existing is not None else "fresh",
            "prev_repos": prev_repos,
        },
        "hooks": {
            "added": added,
            "left_as_is": left,
            # What the user picked, so a recommendation they turned down can be
            # shown as declined rather than simply vanishing.
            "selected": keys,
            "needs_manual": needs_manual,
            # The hook ids this run put in the file, and the subset of those
            # whose silence would mean they were never exercised.
            "added_ids": added_ids,
            "scoped_ids": scoped_ids,
            "versions": versions,
        },
        "files": {"written": written, "kept": kept},
        "net": {
            "prev_repos": prev_repos,
            "new_repos": len(after.repos),
            "delta": " ".join(f"+{k}" for k in keys if k not in prev_keys),
        },
        "internal": {"managed_files": managed},
    }
    if args.facts_out:
        merged = dict(existing_out)
        for section, value in facts.items():
            if isinstance(value, dict) and isinstance(merged.get(section), dict):
                merged[section] = {**merged[section], **value}
            else:
                merged[section] = value
        write_json_or_die(args.facts_out, merged, die)

    print(f"Wrote {path}  ({'updated' if existing is not None else 'new'})", file=sys.stderr)
    if rewrote_empty:
        print("  normalised `repos: []` to an empty block sequence", file=sys.stderr)
    for _tag, line in report:
        print(f"  {line}", file=sys.stderr)
    for rel in written[1:]:
        print(f"  asset {rel}: written", file=sys.stderr)
    for rel in kept:
        print(f"  asset {rel}: exists -- left as-is", file=sys.stderr)
    if versions:
        print("  versions: " + ", ".join(f"{k}={v}" for k, v in versions.items()), file=sys.stderr)

    emit(
        {
            "path": path,
            "mode": "updated" if existing is not None else "new",
            "report": [text for _tag, text in report],
            "needs_manual": needs_manual,
            "written": written,
            "kept": kept,
            "versions": versions,
            "prev_repos": prev_repos,
            "new_repos": len(after.repos),
            "facts": args.facts_out,
        }
    )
    return 0


# -- read-only modes ---------------------------------------------------------
def cmd_catalog() -> int:
    for key, meta in CATALOG.items():
        print(f"{key}\t{meta['desc']}")
    return 0


def cmd_detect(directory: str) -> int:
    cfg = read_config(directory)
    if cfg is None:
        emit({"config": "none", "repos": [], "present": []})
        return 0
    # Every one of these comes from the target repo's own config and is relayed
    # straight to the agent, and from there to the user, before summary.py ever
    # sees it. describe() and render() already clean their equivalents.
    repos = [
        {
            "repo": clean(e.url),
            "rev": clean(e.rev) if e.rev else None,
            "hooks": [clean(h.id) for h in e.hooks],
        }
        for e in cfg.repos
    ]
    emit(
        {
            "config": "existing",
            "repos": repos,
            "present": present_keys(cfg),
            "exclude": exclude_pattern(cfg),
            # Not refused -- a local hook repo is a real workflow -- but never
            # again carried across in silence: whatever pre-commit clones from
            # here comes off this disk, not from a named host.
            "local_repo_sources": [clean(u) for u in cfgmod.local_repo_sources(cfg)],
            "suspicious_characters": has_suspicious_chars(cfg.text),
        }
    )
    return 0


def cmd_recommend(directory: str, facts_out: str | None = None) -> int:
    cfg = read_config(directory)
    previous = present_keys(cfg)
    # An entry that is present but switched off is not coverage. Reported
    # separately so the agent can say so rather than the user being told a
    # catalog entry is already handled.
    disabled = {k: disabled_hooks(cfg, k) for k in previous if cfg} if cfg else {}
    disabled = {k: v for k, v in disabled.items() if v}
    recs, markers, trigger_paths = detect_markers(directory)
    recs = [r for r in recs if r["name"] not in previous]
    proposed = [k for k in ALWAYS_ON if k not in previous] + [
        r["name"] for r in recs if r["name"] not in ALWAYS_ON
    ]
    if facts_out:
        # Seeded here so the summary can show what was detected and what was
        # recommended, with the file that triggered each. cmd_generate merges
        # into this rather than replacing it.
        facts: Facts = {
            # `detected` is prose for a human to read; `detected_paths` is the
            # bare list for anything that has to be passed to a command. One
            # field cannot be both, and asking the caller to parse the path back
            # out of "markdown (README.md)" is the re-derive-by-eye this design
            # exists to prevent.
            "scan": {"detected": markers, "detected_paths": trigger_paths},
            # `disabled` too: a catalog entry that is present but never fires is
            # exactly what a later reader of the summary needs to know, and it
            # was being said once, in the moment, and then forgotten.
            "hooks": {"recommended": recs, "disabled": sorted(disabled)},
        }
        write_json_or_die(facts_out, dict(facts), die)
    # Annotated, so the shape shared.RecommendReport documents is the shape mypy
    # actually enforces here. It had drifted two fields while claiming otherwise.
    report: RecommendReport = {
        "always_on": list(ALWAYS_ON),
        "recommended": recs,
        "previous": previous,
        "disabled": disabled,
        "prerequisites": prerequisites(),
        "local_repo_sources": [clean(u) for u in cfgmod.local_repo_sources(cfg)] if cfg else [],
        "proposed": proposed,
        "detected": markers,
        "detected_paths": trigger_paths,
        "prev_repos": len(cfg.repos) if cfg else 0,
        "config": "existing" if cfg else "none",
    }
    emit(report)
    return 0


# -- verify ------------------------------------------------------------------


def run_precommit(directory: str, *args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["pre-commit", *args],
            cwd=directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            env=env,
        )
    except FileNotFoundError:
        die("pre-commit not found on PATH; install it before verifying")
    except subprocess.TimeoutExpired:
        die(f"pre-commit {' '.join(args)} timed out after 1800s")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def creatable_dir(path: str) -> bool:
    """Whether a process could write inside `path`, creating it if need be.

    Checked, never created: making `/root/.npm` on someone's behalf is the very
    write the sandbox is refusing, and this skill has no business writing
    outside the repository it was pointed at. So the nearest ancestor that does
    exist is asked instead -- if npm could create the directory, npm may.

    A relative path is refused rather than resolved. It would be resolved
    against *this* process's cwd, which is not the cwd npm will run with, so the
    answer would be about a different directory than the one being judged.
    """
    if not os.path.isabs(path):
        return False
    probe = path
    while True:
        if os.path.exists(probe):
            return os.path.isdir(probe) and os.access(probe, os.W_OK | os.X_OK)
        parent = os.path.dirname(probe)
        if parent == probe:  # pragma: no cover - an absolute path reaches a root
            return False
        probe = parent


def npm_config(key: str) -> str | None:
    """What npm says one of its settings is, or None if it will not say.

    Asked rather than reconstructed. npm resolves each of these from the command
    line, the environment, a user `.npmrc` and a global one, in that order, and
    a reimplementation of that precedence here would be a guess that breaks
    quietly on the next npm major -- while npm itself answers exactly, offline,
    and without needing anything to exist.

    Run in a scratch directory for the same reason `npm_latest` is: the
    repository being configured must not get a vote via its own `.npmrc`.
    """
    try:
        with tempfile.TemporaryDirectory() as elsewhere:
            out = subprocess.run(
                [
                    "npm",
                    "config",
                    "get",
                    refuse_option_like(key, "npm config key", die),
                    "--json",
                    "--no-color",
                ],
                cwd=elsewhere,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = npm_scalar(out.stdout)
    # npm prints the string `undefined` for a config it has no value for.
    return value if value and value != "undefined" else None


def npm_cache_dir() -> str | None:
    return npm_config("cache")


def npm_cache_env(cfg: cfgmod.Config | None, scratch: str) -> dict[str, str] | None:
    """An environment giving `pre-commit` a usable npm cache, or None to inherit.

    The mermaid entry is a `language: node` hook with `additional_dependencies`,
    so `pre-commit` builds its environment by running `npm install` -- and
    pre-commit sets `npm_config_prefix` and unsets `npm_config_userconfig`, but
    never touches the cache. An unwritable cache path (`/root/.npm` in a sandbox
    with no writable HOME) therefore kills the hook install exactly the way it
    used to kill the version pin, one step later and with the config already
    written. `npm_latest` passes `--cache` for the same reason.

    Only when the inherited cache is unusable, and only for a config that
    actually carries an npm-backed entry: overriding unconditionally would throw
    away a warm cache every run and re-download the CLI for nothing.
    """
    if not [k for k in present_keys(cfg) if CATALOG[k].get("npm")]:
        return None
    if shutil.which("npm") is None:
        return None
    cache = npm_cache_dir()
    if cache is None or creatable_dir(cache):
        return None
    # Every spelling dropped before one is set. npm lower-cases `npm_config_*`
    # environment names, so leaving an inherited `npm_config_cache` beside a new
    # `NPM_CONFIG_CACHE` leaves npm two values for one key and the winner to
    # process.env's ordering.
    env = {k: v for k, v in os.environ.items() if k.lower() != "npm_config_cache"}
    env["NPM_CONFIG_CACHE"] = os.path.join(scratch, "npm-cache")
    return env


def dirty_paths(directory: str) -> set[str]:
    """Everything dirty right now, or a hard stop.

    Used twice around the hook run to work out what the autofixers rewrote --
    and Step 5 promises to disclose that list before the commit question. A
    failed check returning an empty set would either invent autofixes (if the
    BEFORE call failed) or silently drop the disclosure entirely (if the AFTER
    one did) -- the failure shared.safe_porcelain refuses on everyone's behalf.
    """
    out = safe_porcelain(git, directory, (), die, what="what the hooks changed")
    return {p for ln in out.splitlines() if (p := porcelain_path(ln))}


def cmd_verify(args: argparse.Namespace) -> int:
    """Install the hook, run it, and judge what came back.

    Two outcomes look like success and are not, which is the whole reason this
    is a program rather than a paragraph: the vacuous pass described in
    :func:`is_vacuous`, and the autofixing hooks, which rewrite files and exit
    non-zero on their FIRST run. A clean second pass is the success there, and
    the first exit is not a failure.
    """
    directory = args.dir
    refuse_facts_inside_repo(directory, args.facts, die)
    existing_facts = load_facts_if_present(args.facts) if args.facts else {}
    # Every argument is checked BEFORE anything is installed or run. `install`
    # writes .git/hooks/pre-commit -- a mutation of the user's repository -- and
    # a refusal that fires after it has already happened is not a refusal. It
    # also made these guards conditional on the environment: with pre-commit
    # absent from PATH the run died at the install with a different message, so
    # a caller pointing the autofixing hooks at /etc/hosts was never told no.
    # CI, which has no pre-commit in the test job, is where that showed up.
    files = list(args.files)
    if args.files_file:
        # Repo filenames are arbitrary: git permits quotes, `$`, backticks and
        # semicolons in a path. They must not be typed into the command the
        # agent runs, for the same reason catalog keys, remote names and commit
        # messages all go through files.
        if files:
            die("pass --files or --files-file, not both")
        listed = read_bytes_or_die(args.files_file, die).decode("utf-8", "replace")
        files = [ln.strip() for ln in listed.splitlines() if ln.strip()]
        if not files:
            die(f"no paths in {args.files_file}")
    # `--` and a per-value check: these come from the caller, and pre-commit
    # reads a leading dash as one of its own options. Without this a value like
    # "--hook-stage" silently changes what the run does, inside a step whose
    # whole point is that the user approved its scope.
    for name in files:
        refuse_option_like(name, "file", die)
        # And inside the repo. pre-commit resolves these against cwd, so an
        # absolute path or a ../ traversal points the autofixing hooks at a
        # file outside the tree (which they rewrite) and gitleaks at one it
        # will read and print. Rejecting a leading dash was only half of it.
        resolved = os.path.realpath(os.path.join(directory, name))
        root = os.path.realpath(directory)
        if resolved != root and not resolved.startswith(root + os.sep):
            die(f"{name!r} resolves outside the repository; refusing to check it")

    # One scratch directory for the whole run: `install` and both `run`s have to
    # see the same npm cache, or the second one re-downloads what the first
    # fetched. See npm_cache_env -- usually None, and then nothing is overridden.
    with tempfile.TemporaryDirectory() as scratch:
        env = npm_cache_env(read_config(directory), scratch)
        rc, install_out = run_precommit(directory, "install", env=env)
        if rc != 0:
            die(f"pre-commit install failed (exit {rc}): {clean(install_out)}")

        before = dirty_paths(directory)
        run_args = ["run", "--files", "--", *files] if files else ["run", "--all-files"]
        rc, output = run_precommit(directory, *run_args, env=env)
        autofixed: list[str] = []

        if rc != 0:
            autofixed = sorted(dirty_paths(directory) - before)
            rc, output = run_precommit(directory, *run_args, env=env)

    # This run's own files, from the facts it was given: the two halves of
    # `autofixed` get opposite sentences, so the split has to know which is
    # which. normpath on both sides, because one list comes from git status and
    # the other from the facts file.
    # isinstance, because the facts file is on disk between steps and a
    # rewritten one can hold anything. A malformed entry is somebody else's
    # error to report -- gitwork.managed() does that with a sentence -- not a
    # traceback out of this list comprehension.
    managed_now = {
        os.path.normpath(str(entry.get("path", "")))
        for entry in (existing_facts.get("internal") or {}).get("managed_files") or []
        if isinstance(entry, dict)
    }
    vacuous = is_vacuous(output)
    skipped = skipped_hooks(output)
    # Which hook ids this run actually put in the config; anything of ours that
    # reported no files was not exercised, however green the run looks.
    scoped_ids = set((existing_facts.get("hooks") or {}).get("scoped_ids") or [])
    unchecked = sorted(name for name in skipped if name in scoped_ids)
    run_ok = rc == 0 and not vacuous and not unchecked
    if vacuous:
        summary = (
            "vacuous pass -- every hook reported (no files to check). --all-files covers "
            "only tracked files, so nothing was actually checked; re-run naming the paths "
            "explicitly with --files-file."
        )
    elif unchecked:
        summary = (
            f"passed, but {', '.join(unchecked)} had no files to check -- "
            "a hook this run added was never exercised. Re-run naming files it "
            "matches -- scan.detected_paths holds exactly those."
        )
    elif rc == 0 and autofixed:
        summary = f"passed on the second run; hooks autofixed {len(autofixed)} file(s)"
    elif rc == 0:
        summary = "passed"
    else:
        summary = f"failed (exit {rc})"

    if args.facts:
        facts = read_json_or_die(args.facts, die)
        # The autofixing hooks act on whole-file content, so this run's OWN
        # files can be among what they just rewrote -- and then the commit
        # gate, which compares against the sha256 Step 3 recorded, refuses a
        # change this run itself caused, with no way for the caller to tell
        # that apart from tampering. Re-hash from the settled tree instead.
        managed = (facts.get("internal") or {}).get("managed_files") or []
        for entry in managed:
            if not isinstance(entry, dict):
                die("a managed_files entry is not a JSON object")
            full = os.path.join(directory, str(entry.get("path", "")))
            if os.path.isfile(full) and not os.path.islink(full):
                entry["sha256"] = sha256_of(full)
        facts["verify"] = {
            "scope": "these-files" if files else "all-files",
            "install": "git hook installed",
            "run": summary,
            "run_ok": run_ok,
            "vacuous": vacuous,
            "autofixed": autofixed,
            "unchecked": unchecked,
        }
        write_json_or_die(args.facts, facts, die)

    if output.strip():
        # Repo-controlled text, like every other string this file relays. The
        # hooks echo the paths they check, gitleaks prints match context, and a
        # `repo: local` block supplies its own hook `name:` -- so this is a
        # channel from the repository straight to the agent, and it was the one
        # that skipped clean(). Bounded too: a hook can print a whole file, and
        # the agent has to read this to judge the run.
        shown = clean(output)
        if len(shown) > MAX_HOOK_OUTPUT:
            shown = shown[:MAX_HOOK_OUTPUT] + "\n...(truncated; re-run pre-commit to see it all)"
        if has_suspicious_chars(output):
            print(
                "precommit: WARNING - the hook output contained control or "
                "text-reordering characters; they have been neutralised, and what "
                "a hook printed may not be what its files say.",
                file=sys.stderr,
            )
        print(shown, file=sys.stderr)
    emit(
        {
            "install": "git hook installed",
            "run": summary,
            "run_ok": run_ok,
            "vacuous": vacuous,
            "autofixed": autofixed,
            "unchecked": unchecked,
            # Split here, not by the agent. SKILL.md used to tell it to
            # partition this against files.written + files.kept -- set
            # arithmetic over two lists it had to hold in its head, to answer a
            # question this function already has the inputs for. The two halves
            # get opposite sentences: ours IS committed (cmd_verify re-hashes
            # the managed files precisely so an autofixed one still passes the
            # commit gate), theirs is not.
            # The exact list a vacuous re-run needs, so the recovery path stops
            # being a set union the agent performs by hand -- on the one path
            # that is already running because verification went wrong once.
            "rerun_files": sorted(
                {*(existing_facts.get("files") or {}).get("written", [])}
                | {*(existing_facts.get("scan") or {}).get("detected_paths", [])}
            ),
            "autofixed_ours": [f for f in autofixed if os.path.normpath(f) in managed_now],
            "autofixed_elsewhere": [f for f in autofixed if os.path.normpath(f) not in managed_now],
            "skipped": skipped,
            "scope": "these-files" if files else "all-files",
            "exit": rc,
        }
    )
    return 0 if run_ok else EXIT_ERROR


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("--dir", default=".", help="repository root")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--catalog", action="store_true", help="list catalog keys")
    mode.add_argument("--detect", action="store_true", help="report the existing config")
    mode.add_argument("--recommend", action="store_true", help="what this repo calls for, and why")
    mode.add_argument("--templates-file", help="the user's selection, one catalog key per line")
    mode.add_argument("--verify", action="store_true", help="install the git hook and run it")
    ap.add_argument("--force", action="store_true", help="update an existing config")
    ap.add_argument("--facts-out", help="write the run's facts JSON here")
    ap.add_argument("--facts", help="the run's facts JSON, to record into")
    ap.add_argument("--files", nargs="*", default=[], help="verify: check these paths explicitly")
    ap.add_argument(
        "--files-file",
        help="verify: read the paths to check from this file, one per line",
    )
    args = ap.parse_args()

    if args.catalog:
        return cmd_catalog()
    if not os.path.isdir(args.dir):
        die(f"directory not found: {args.dir}")
    if args.detect:
        return cmd_detect(args.dir)
    if args.recommend:
        refuse_facts_inside_repo(args.dir, args.facts_out, die)
        return cmd_recommend(args.dir, args.facts_out)
    if args.verify:
        if not args.facts:
            # Without it there are no scoped_ids, so `unchecked` is always empty
            # and a run whose new hooks saw nothing still reports run_ok -- and
            # the VERIFY section never reaches the summary at all. Every gitwork
            # subcommand that needs facts declares it required; this one only
            # behaved as though it did not.
            die("--verify needs --facts: without it nothing can be checked or recorded")
        return cmd_verify(args)
    return cmd_generate(args)


if __name__ == "__main__":
    raise SystemExit(main())
