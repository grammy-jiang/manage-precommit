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
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from typing import NoReturn

import config as cfgmod
from shared import (
    Facts,
    ManagedFile,
    Recommendation,
    RecommendReport,
    atomic_write_bytes,
    clean,
    default_file_mode,
    has_suspicious_chars,
    is_work_tree,
    make_git,
    porcelain_path,
    preserved_mode,
    read_bytes_nofollow,
    read_bytes_or_die,
    read_json_or_die,
    refuse_facts_inside_repo,
    refuse_option_like,
    safe_porcelain,
    write_json_or_die,
)

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(SKILL_DIR, "templates")
ASSETS = os.path.join(SKILL_DIR, "assets")
TARGET_NAME = ".pre-commit-config.yaml"

EXIT_ERROR = 1
EXIT_UNKNOWN_KEY = 3
EXIT_DIRTY = 4
EXIT_REFUSED = 5

CATALOG: dict[str, dict] = {
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


def die(msg: str, code: int = EXIT_ERROR) -> NoReturn:
    print(f"precommit: {msg}", file=sys.stderr)
    sys.exit(code)


git = make_git(die)


def emit(payload: Mapping[str, object]) -> None:
    json.dump(dict(payload), sys.stdout, indent=2)
    sys.stdout.write("\n")


# -- versions ----------------------------------------------------------------
VER_RE = re.compile(r"^v?\d+(?:\.\d+)*$")


def version_key(tag: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", tag)]


def latest_tag(repo_url: str) -> str:
    """The newest release tag on a hook repository, fetched live.

    Only tags that are purely a version are considered, so a `nightly` or a
    `v2-beta` ref can never be pinned as though it were a release.
    """
    url = refuse_option_like(repo_url, "repo url", die)
    # In a scratch directory, with no system or global config: see make_git's
    # `isolated`. Running this under the target repo's config would let that
    # repo decide which server answers for a catalog URL.
    with tempfile.TemporaryDirectory() as elsewhere:
        rc, out, err = git(elsewhere, "ls-remote", "--tags", "--refs", url, isolated=True)
    if rc != 0:
        die(f"git ls-remote failed for {repo_url}: {err}")
    tags = []
    for line in out.splitlines():
        if "refs/tags/" not in line:
            continue
        ref = line.split("refs/tags/", 1)[1].strip()
        if VER_RE.match(ref):
            tags.append(ref)
    if not tags:
        die(f"no version tags found for {repo_url}")
    tags.sort(key=version_key)
    return tags[-1]


def npm_latest(pkg: str) -> str:
    name = refuse_option_like(pkg, "npm package", die)
    # Same reasoning as latest_tag: run somewhere the repository being
    # configured cannot supply an .npmrc that redirects the registry.
    try:
        with tempfile.TemporaryDirectory() as elsewhere:
            out = subprocess.run(
                ["npm", "view", name, "version"],
                cwd=elsewhere,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
    except FileNotFoundError:
        die(f"npm not found; it is needed to pin {pkg}")
    except (OSError, subprocess.SubprocessError) as exc:
        die(f"could not run npm for {pkg}: {exc}")
    if out.returncode != 0:
        die(f"npm view {pkg} failed: {clean(out.stderr)}")
    version = out.stdout.strip()
    if not VER_RE.match(version):
        die(f"npm returned an unexpected version for {pkg}: {clean(version)!r}")
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
        lambda msg: die(msg, code=EXIT_DIRTY),
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
        )


def plan(
    cfg: cfgmod.Config, keys: list[str], *, pre_existing: bool
) -> tuple[list[cfgmod.Insertion], list[tuple[str, str]], dict]:
    """Work out every insertion, without touching the file.

    `pre_existing` says whether the config came from the user or from our own
    skeleton, which is the difference between "you already had an `exclude`, so
    .gitignore is not covered" and a note about a line we just wrote ourselves.
    """
    insertions: list[cfgmod.Insertion] = []
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
            report.append(("added", f"local: added ({', '.join(missing)})"))
            continue

        existing = cfg.repo(entry.url)
        if existing is None:
            insertions.append(cfgmod.Insertion(at=append_at, block=block, what=entry.url))
            report.append(("added", f"{entry.url}: added (rev {entry.rev})"))
            continue

        have_ids = {h.id for h in existing.hooks}
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
        report.append(
            (
                "added",
                f"{clean(entry.url)}: present "
                f"(rev {clean(existing.rev) if existing.rev else '?'}) -- added hooks "
                f"{', '.join(missing)}",
            )
        )

    return insertions, report, versions


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


def load_facts_if_present(path: str) -> dict:
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


def verify_written(path: str, keys: list[str], after: cfgmod.Config) -> None:
    """Every selected catalog entry really is in the file that was just written."""
    urls = {e.url for e in after.repos}
    local_ids = after.local_hook_ids()
    for key in keys:
        meta = CATALOG[key]
        if meta.get("rev_repo"):
            if meta["rev_repo"] not in urls:
                die(f"{path} was written but {key} is not in it; refusing to report success")
        elif meta.get("local_hook_id") and meta["local_hook_id"] not in local_ids:
            die(f"{path} was written but {key} is not in it; refusing to report success")


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
    planned, report, versions = plan(cfg, keys, pre_existing=existing is not None)
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

    verify_written(path, keys, after)
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
SKIPPED_NO_FILES = re.compile(r"\(no files to check\)\s*Skipped")
HOOK_RESULT_LINE = re.compile(r"\.{3,}.*\b(Passed|Failed|Skipped)\b")


def run_precommit(directory: str, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["pre-commit", *args],
            cwd=directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
    except FileNotFoundError:
        die("pre-commit not found on PATH; install it before verifying")
    except subprocess.TimeoutExpired:
        die(f"pre-commit {' '.join(args)} timed out after 1800s")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def skipped_hooks(output: str) -> list[str]:
    r"""Hooks that ran but reported they had no files to check.

    is_vacuous() is all-or-nothing: one hook with something to do flips it off.
    But hygiene's hooks match any file, so they alone turn a run green while
    markdownlint and mermaid -- filtered to `\.(md|markdown)$`, and added
    precisely because a .md was detected -- are still checking nothing. That is
    a partial vacuity the whole-output verdict cannot express.
    """
    names = []
    for line in output.splitlines():
        if HOOK_RESULT_LINE.search(line) and SKIPPED_NO_FILES.search(line):
            names.append(re.split(r"\.{3,}", line, maxsplit=1)[0].strip())
    return names


def is_vacuous(output: str) -> bool:
    """True when every hook that ran reported it had nothing to check.

    `pre-commit run --all-files` covers only git-*tracked* files, so in a repo
    where the setup files are still untracked every hook prints "(no files to
    check) Skipped" and the command exits 0. That is a pass which tested
    nothing, and reporting it as success is the failure this catches.
    """
    results = [ln for ln in output.splitlines() if HOOK_RESULT_LINE.search(ln)]
    if not results:
        # No parseable result line at all -- no hooks configured, output in a
        # shape this cannot read, a wrapper that swallowed it. Whatever the
        # cause, nothing was observed to run, and reporting that as a clean
        # pass is precisely the false positive this function exists to catch.
        return True
    return all(SKIPPED_NO_FILES.search(ln) for ln in results)


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

    rc, install_out = run_precommit(directory, "install")
    if rc != 0:
        die(f"pre-commit install failed (exit {rc}): {clean(install_out)}")

    before = dirty_paths(directory)
    run_args = ["run", "--files", "--", *files] if files else ["run", "--all-files"]
    rc, output = run_precommit(directory, *run_args)
    autofixed: list[str] = []

    if rc != 0:
        autofixed = sorted(dirty_paths(directory) - before)
        rc, output = run_precommit(directory, *run_args)

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
        print(output, file=sys.stderr)
    emit(
        {
            "install": "git hook installed",
            "run": summary,
            "run_ok": run_ok,
            "vacuous": vacuous,
            "autofixed": autofixed,
            "unchecked": unchecked,
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
