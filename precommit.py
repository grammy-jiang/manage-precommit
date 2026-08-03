#!/usr/bin/env python3
"""Generate or merge a .pre-commit-config.yaml from the manage-precommit catalog.

Deterministic engine for the manage-precommit skill. Each catalog entry is a
small YAML template fragment; this fills it with the latest upstream version and
merges it into the repo's config with ruamel.yaml, so an existing file keeps its
comments and formatting. It also copies the repo-side assets a hook needs (the
mermaid script, linter configs).

The merge never clobbers: an existing repo entry keeps its rev; only missing
hooks are added; existing custom config outside the catalog is left untouched.

Modes:
  precommit.py --catalog                       # list catalog keys
  precommit.py --dir REPO --detect             # report existing config
  precommit.py --dir REPO [--force] KEY...     # generate/merge + copy assets

Catalog keys: hygiene, yamllint, markdownlint, mermaid, gitleaks
Requires ruamel.yaml (pip install --user ruamel.yaml).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(SKILL_DIR, "templates")
ASSETS = os.path.join(SKILL_DIR, "assets")

CATALOG = {
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
        "desc": "Markdown linter (config: .markdownlint.yaml)",
    },
    "mermaid": {
        "fragment": "mermaid.yaml",
        "rev_repo": None,
        "npm": "@mermaid-js/mermaid-cli",
        "assets": [("lint-mermaid.mjs", "scripts/lint-mermaid.mjs")],
        "desc": "Mermaid diagram validator (local hook; needs node + a browser)",
    },
    "gitleaks": {
        "fragment": "gitleaks.yaml",
        "rev_repo": "https://github.com/gitleaks/gitleaks",
        "assets": [],
        "desc": "secret scanner",
    },
}


def die(msg: str, code: int = 1) -> "typing.NoReturn":  # noqa: F821
    print(f"precommit: {msg}", file=sys.stderr)
    sys.exit(code)


def ensure_ruamel():
    try:
        from ruamel.yaml import YAML  # noqa: F401
    except ModuleNotFoundError:
        die("ruamel.yaml not installed. Run: python3 -m pip install --user ruamel.yaml", code=3)


def yaml_rt():
    from ruamel.yaml import YAML

    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # do not wrap long lines (e.g. regex excludes)
    y.indent(mapping=2, sequence=4, offset=2)
    return y


VER_RE = re.compile(r"^v?\d+(?:\.\d+)*$")


def version_key(tag: str):
    return [int(n) for n in re.findall(r"\d+", tag)]


def latest_tag(repo_url: str) -> str:
    try:
        out = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", repo_url],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        die(f"could not reach {repo_url}: {exc}")
    if out.returncode != 0:
        die(f"git ls-remote failed for {repo_url}: {out.stderr.strip()}")
    tags = []
    for line in out.stdout.splitlines():
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
    try:
        out = subprocess.run(["npm", "view", pkg, "version"], capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        die(f"could not run npm for {pkg}: {exc}")
    if out.returncode != 0:
        die(f"npm view {pkg} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def repo_url(entry) -> str:
    return entry.get("repo")


def hook_ids(entry) -> list:
    return [h.get("id") for h in (entry.get("hooks") or []) if isinstance(h, dict)]


def load_fragment(key: str, yaml) -> tuple:
    """Return (repo_entry, version_str) with latest version filled in."""
    with open(os.path.join(TEMPLATES, CATALOG[key]["fragment"])) as fh:
        seq = yaml.load(fh)
    repo = seq[0]
    meta = CATALOG[key]
    version = None
    if meta.get("rev_repo"):
        version = latest_tag(meta["rev_repo"])
        repo["rev"] = version
    if meta.get("npm"):
        version = npm_latest(meta["npm"])
        for hook in repo.get("hooks", []):
            deps = hook.get("additional_dependencies") or []
            for i, dep in enumerate(deps):
                if str(dep).startswith(meta["npm"] + "@"):
                    deps[i] = f'{meta["npm"]}@{version}'
    return repo, version


def merge_repo(config, new_repo, report):
    repos = config["repos"]
    url = repo_url(new_repo)
    new_ids = hook_ids(new_repo)

    if url == "local":
        present = set()
        for entry in repos:
            if repo_url(entry) == "local":
                present |= set(hook_ids(entry))
        missing = [h for h in new_repo["hooks"] if h.get("id") not in present]
        if not missing:
            report.append(f"local ({', '.join(new_ids)}): already present — left as-is")
            return
        repos.append(new_repo)
        report.append(f"local: added ({', '.join(new_ids)})")
        return

    for entry in repos:
        if repo_url(entry) == url:
            present = set(hook_ids(entry))
            added = []
            for hook in new_repo.get("hooks", []):
                if hook.get("id") not in present:
                    entry["hooks"].append(hook)
                    added.append(hook.get("id"))
            rev = entry.get("rev", "?")
            if added:
                report.append(f"{url}: present (rev {rev}) — added hooks {', '.join(added)}")
            else:
                report.append(f"{url}: already present (rev {rev}) — left as-is")
            return

    repos.append(new_repo)
    report.append(f"{url}: added (rev {new_repo.get('rev')})")


def ensure_top_matter(config, report):
    from ruamel.yaml.scalarstring import SingleQuotedScalarString as SQ

    if "minimum_pre_commit_version" not in config:
        config["minimum_pre_commit_version"] = "4.0.0"
        report.append("added minimum_pre_commit_version")
    if "exclude" not in config:
        config["exclude"] = SQ(r"^\.gitignore$")
        report.append(r"added exclude '^\.gitignore$'")
    if config.get("repos") is None:
        config["repos"] = []


def copy_assets(key, directory, report):
    for src, rel in CATALOG[key].get("assets", []):
        dest = os.path.join(directory, rel)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.lexists(dest):
            report.append(f"asset {rel}: exists — left as-is")
            continue
        shutil.copyfile(os.path.join(ASSETS, src), dest)
        report.append(f"asset {rel}: written")


def atomic_dump(config, target, yaml):
    directory = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".pre-commit-config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            yaml.dump(config, fh)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_catalog():
    for key, meta in CATALOG.items():
        print(f"{key}\t{meta['desc']}")


def cmd_detect(directory):
    target = os.path.join(directory, ".pre-commit-config.yaml")
    if not os.path.exists(target):
        print("config: none")
        return
    yaml = yaml_rt()
    with open(target) as fh:
        config = yaml.load(fh) or {}
    repos = config.get("repos") or []
    print(f"config: existing ({len(repos)} repos)")
    for entry in repos:
        print(f"  {repo_url(entry)}  rev={entry.get('rev', '-')}  hooks=[{','.join(hook_ids(entry))}]")
    present_urls = {repo_url(e) for e in repos}
    have = []
    for key, meta in CATALOG.items():
        if meta.get("rev_repo") in present_urls:
            have.append(key)
        elif key == "mermaid" and any(
            repo_url(e) == "local" and "mermaid-lint" in hook_ids(e) for e in repos
        ):
            have.append(key)
    print("catalog present: " + (",".join(have) if have else "(none)"))


def cmd_generate(directory, keys, force):
    unknown = [k for k in keys if k not in CATALOG]
    if unknown:
        die(f"unknown catalog key(s): {', '.join(unknown)} (see --catalog)")
    if not os.path.isdir(directory):
        die(f"target dir not found: {directory}")

    yaml = yaml_rt()
    target = os.path.join(directory, ".pre-commit-config.yaml")
    if os.path.islink(target):
        die(f"{target} is a symlink — refusing to follow it")

    existed = os.path.exists(target)
    if existed:
        if not force:
            die(f"{target} exists — re-run with --force to update it")
        with open(target) as fh:
            config = yaml.load(fh)
        if config is None:
            with open(os.path.join(TEMPLATES, "base.yaml")) as fh:
                config = yaml.load(fh)
    else:
        with open(os.path.join(TEMPLATES, "base.yaml")) as fh:
            config = yaml.load(fh)

    report = []
    ensure_top_matter(config, report)

    versions = {}
    for key in keys:
        repo, version = load_fragment(key, yaml)
        if version:
            versions[key] = version
        merge_repo(config, repo, report)
        copy_assets(key, directory, report)

    atomic_dump(config, target, yaml)

    print(f"Wrote {target}  ({'updated' if existed else 'new'})")
    for line in report:
        print(f"  {line}")
    if versions:
        print("Versions: " + ", ".join(f"{k}={v}" for k, v in versions.items()))


def main():
    ap = argparse.ArgumentParser(description="manage-precommit config engine")
    ap.add_argument("--dir", default=".")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--detect", action="store_true")
    ap.add_argument("--catalog", action="store_true")
    ap.add_argument("keys", nargs="*")
    args = ap.parse_args()

    if args.catalog:
        cmd_catalog()
        return

    ensure_ruamel()

    if args.detect:
        cmd_detect(args.dir)
        return

    if not args.keys:
        die("no catalog keys given (see --catalog)")
    cmd_generate(args.dir, args.keys, args.force)


if __name__ == "__main__":
    main()
