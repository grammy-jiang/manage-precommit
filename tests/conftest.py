"""Shared fixtures.

Two rules shape everything here.

**No test touches the network.** A stub `git` and a stub `npm` go on PATH, so
the real version-pinning path -- including the tag filtering and the "no version
tags" refusal -- runs offline against canned output. The stub `git` forwards
every other subcommand to the real binary, because the rest of this suite tests
code that commits and pushes, and a mock that agrees with a wrong assumption is
worse than no test.

**Scripts are run the way the skill runs them**: as subprocesses, by path, with
PYTHONPATH *removed*. The skill installs as a bare symlink under the user's
system python3, so a green run here is evidence that they are self-contained
rather than an assertion that they are.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "src" / "manage_precommit" / "skill"
SCRIPTS = SKILL / "scripts"

REAL_GIT = shutil.which("git")

# What the stub `git ls-remote` prints. Deliberately mixed: two real versions, a
# non-version tag that must be ignored, and an out-of-order one so the sort is
# actually exercised.
LS_REMOTE_TAGS = """\
1111111111111111111111111111111111111111\trefs/tags/v1.2.0
2222222222222222222222222222222222222222\trefs/tags/v10.0.1
3333333333333333333333333333333333333333\trefs/tags/nightly
4444444444444444444444444444444444444444\trefs/tags/v2.30.4
"""

NPM_VERSION = "11.99.0"


@pytest.fixture(scope="session", autouse=True)
def _require_git() -> None:
    if REAL_GIT is None:
        pytest.skip("git is not installed", allow_module_level=True)


# Per-repo tag sets, so a swapped rev_repo shows up as a wrong pinned version
# rather than passing silently. Every set still exercises the same filtering:
# a non-version tag to ignore and an out-of-order one to sort.
REPO_TAGS = {
    "pre-commit-hooks": "v1.2.0 v10.0.1 nightly v2.30.4",
    "yamllint": "v1.9.0 v1.38.0 latest",
    "markdownlint-cli2": "v0.9.0 v0.23.2",
    "gitleaks": "v8.30.1 v8.9.0",
}


@pytest.fixture
def stubs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A PATH dir whose `git` answers ls-remote offline and forwards the rest.

    It answers *per URL* and logs its argv to `<dir>/calls.log`, so a test can
    assert which repository or package was actually asked about -- a stub that
    replies the same thing to everything cannot catch a wrong URL.
    """
    d = tmp_path_factory.mktemp("stubs")
    log = d / "calls.log"
    cases = "\n".join(
        f'    *{name}*) printf "$(tags_for "{tags}")" ;;' for name, tags in REPO_TAGS.items()
    )
    git = d / "git"
    git.write_text(
        "#!/bin/sh\n"
        f'echo "git $*" >> "{log}"\n'
        "tags_for() { for t in $1; do printf '%s\\trefs/tags/%s\\n' 0000000 \"$t\"; done; }\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "ls-remote" ]; then\n'
        '    url="$(eval echo \\${$#})"\n'
        '    case "$url" in\n'
        f"{cases}\n"
        "    *) exit 0 ;;\n"
        "    esac\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    npm = d / "npm"
    npm.write_text(f'#!/bin/sh\necho "npm $*" >> "{log}"\necho {NPM_VERSION}\n')
    for f in (git, npm):
        f.chmod(0o755)
    return d


def stub_calls(stubs: Path) -> str:
    """Everything the stubs were asked, for tests that assert on the argument."""
    log = stubs / "calls.log"
    return log.read_text() if log.exists() else ""


@pytest.fixture
def no_tags_stub(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A PATH directory whose `git ls-remote` returns no version tags at all."""
    d = tmp_path_factory.mktemp("notags")
    git = d / "git"
    git.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "ls-remote" ]; then\n'
        "    printf '5555555555555555555555555555555555555555\\trefs/tags/nightly\\n'\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        f'exec {REAL_GIT} "$@"\n'
    )
    git.chmod(0o755)
    return d


def run(script: str, *args: str, stubs: Path | None = None, cwd: Path | None = None):
    """Run one of the skill's scripts exactly as SKILL.md does."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # prove the scripts resolve each other unaided
    if stubs is not None:
        env["PATH"] = f"{stubs}{os.pathsep}{env['PATH']}"
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
        timeout=120,
    )


def out_json(proc: subprocess.CompletedProcess) -> dict:
    assert proc.stdout.strip(), f"no JSON on stdout; stderr was:\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one commit, and identity configured."""
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run([REAL_GIT, "init", "-q", "-b", "main", str(d)], check=True)
    for key, value in (("user.email", "t@example.invalid"), ("user.name", "Test")):
        subprocess.run([REAL_GIT, "-C", str(d), "config", key, value], check=True)
    (d / "README.md").write_text("# hello\n")
    subprocess.run([REAL_GIT, "-C", str(d), "add", "-A"], check=True)
    subprocess.run([REAL_GIT, "-C", str(d), "commit", "-qm", "init"], check=True)
    return d


@pytest.fixture
def keys_file(tmp_path: Path):
    """Write a selection file outside the repo, the way Step 2 does."""

    def _write(*names: str) -> Path:
        p = tmp_path / "keys.txt"
        p.write_text("".join(f"{n}\n" for n in names))
        return p

    return _write


@pytest.fixture
def facts_path(tmp_path: Path) -> Path:
    """A facts path outside the repo -- the tools refuse one inside it."""
    return tmp_path / "facts.json"


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        [REAL_GIT, "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()
