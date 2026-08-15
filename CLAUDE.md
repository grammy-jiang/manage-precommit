# manage-precommit — notes for whoever edits this next

`README.md` is for someone using the skill. This is for someone changing it.

## Layout

| Path | What it is |
| --- | --- |
| `src/manage_precommit/cli.py` | the console script — an installer, and nothing else |
| `src/manage_precommit/agents.py` | which agents exist and where each reads skills from |
| `src/manage_precommit/skill/SKILL.md` | the procedure the agent follows, Steps 0–6 |
| `src/manage_precommit/skill/scripts/` | the tools SKILL.md drives; all the work is here |
| `src/manage_precommit/skill/templates/` | one catalog fragment per hook set |
| `src/manage_precommit/skill/assets/` | files copied into the target repo (`lint-mermaid.mjs`, the two linter configs) |
| `src/manage_precommit/skill/references/` | loaded on demand: `push-safety.md`, `example-output.md` |
| `tests/` | the suite, plus `check_coverage.py` (the per-file floor) |
| `scripts/lint-mermaid.mjs` | symlink into `skill/assets/`, so this repo runs its own mermaid hook |

The five scripts, in dependency order:

| Script | Owns |
| --- | --- |
| `shared.py` | sanitiser, guarded file IO, the hardened git runner, the `facts.json` contract |
| `config.py` | the strict YAML line scanner and the additive text writer |
| `precommit.py` | the catalog, detection, recommendation, generation, `--verify` |
| `gitwork.py` | every git mutation: status, commit, push-plan, push, facts |
| `summary.py` | renders `facts.json` for the closing message |

## Four rules that hold the shape

1. **No third-party runtime dependency, ever.** The skill installs as a bare
   symlink and its scripts run under the user's *system* `python3`. Nothing
   would install anything on their behalf, so `import ruamel` is a crash on
   somebody else's machine, not a packaging inconvenience. This is why the YAML
   round-trip is a hand-written scanner. `tests/test_shared.py` holds it with an
   AST check, not a convention.
2. **The checkout tree and the wheel tree are the same tree.** No
   `force-include`, no build-time remap. A path in a traceback is therefore
   traceable to this repository by relative position.
3. **Anything a program can decide, a program decides.** The scripts write
   `facts.json`; the agent reads it and relays. The agent asks, judges, and
   writes the commit message — it does not compute, count, or diff. If you find
   yourself asking the model to work something out, that belongs in a script.
4. **Every git mutation goes through `gitwork.py`, fail-closed.** SKILL.md never
   runs `git commit`, `git push`, `git add`, or `git checkout` itself. A gate
   that cannot determine its answer refuses; it does not proceed.

## Commands

```sh
make verify      # lint + typecheck + test -- run this before shipping anything
make test        # pytest
make coverage    # the suite under coverage, then the per-file 90% floor
make lint        # ruff check + ruff format --check   (both; one is not the other)
make format      # ruff format + ruff check --fix     (writes)
make typecheck   # mypy
make install     # symlink this checkout into ~/.claude/skills/
make uninstall
```

`make install` links the *working tree* into `~/.claude/skills/`, so an edit is
live on the next Claude Code restart with no rebuild. The published package's
`manage-precommit install` does the same from site-packages, for whichever of
Claude Code, Codex and Copilot CLI it finds. The published package does the same thing from
site-packages via `manage-precommit install`.

## Testing

Two rules shape `tests/conftest.py`:

- **No test touches the network.** A stub `git` and a stub `npm` go on PATH, so
  the real version-pinning path — tag filtering, the "no version tags" refusal —
  runs offline against canned output. The stub `git` forwards every other
  subcommand to the real binary: the suite tests code that commits and pushes,
  and a mock that agrees with a wrong assumption is worse than no test.
- **Scripts run the way the skill runs them** — as subprocesses, by path, with
  `PYTHONPATH` removed. A green run is therefore evidence that they are
  self-contained, not an assertion that they are.

`run(..., only_path=True)` *replaces* PATH instead of prepending to it. That is
the only way to test a genuinely missing tool; prepending leaves the real one
findable behind the stub, and the test passes while proving nothing.

**When you fix a safety property, mutate it back and watch the suite go red.**
Every guard in these scripts was added that way, and several "fixes" were caught
being vacuous by exactly this step — including a `-k` filter that matched no
tests at all and scored as "caught" on pytest's exit 5.

## Coverage

```sh
make coverage
```

`MP_COVER_SUBPROCESS=1` is what makes the number real. Most of the suite drives
the scripts as subprocesses, which a plain coverage run cannot see — without it
the report understates them by roughly two thirds. `COVERAGE_FILE` is absolute
because those subprocesses start in throwaway repositories.

The floor is **95%, per file, not per project**: a project total hides a hole, and
the barely-covered file is where the next bug is. `tests/check_coverage.py` is
the enforcer, and CI runs that same script rather than a second threshold that
could drift from it.

## Platforms

Linux and macOS. The `installer` CI job runs `test_cli.py` and `test_agents.py`
on `windows-latest` and `macos-latest`, and both pass — but that is the
installer, not the skill. `SKILL.md` runs `mktemp`, `rm -f` and `command -v`
and drives `pre-commit`; making that portable is a separate piece of work, and
until it is done the classifiers, `compatibility:` and the README all say the
same thing.

Windows taught the suite something worth keeping: `Path.home()` reads
`USERPROFILE` there, so tests that set `$HOME` were operating on the runner's
real home. The `home` fixture in `test_cli.py` patches the lookup itself, and a
test asserts the redirection happened. Prefer that fixture over `monkeypatch.setenv`
for anything that resolves a home directory.

## Python versions

`requires-python = ">=3.10"`, and CI runs the suite on 3.10 through 3.14. mypy
and the lint job pin **3.10** on purpose: a construct newer than that should
fail here, not on a user's machine. Watch for `typing.NotRequired` (3.11) in the
`Facts` TypedDicts and `tomllib` (3.11) — both need a guard or an alternative.

## Safety properties worth not breaking

These were each found by review and each has a test. Deleting the guard must
turn the suite red.

- **Nothing is read or written through a symlink.** `read_bytes_nofollow` opens
  with `O_NOFOLLOW|O_NONBLOCK` and checks `S_ISREG`. Both directions matter: a
  symlinked asset directory wrote outside the repo, and a symlinked `.md` was
  read during `--recommend`.
- **Every path is proven inside the repo** before anything is copied to it.
- **Repo-derived text is `clean()`ed before it reaches the agent.** Config
  values, revs, remote URLs, branch names — SKILL.md relays some of these
  verbatim, before `summary.py` ever runs.
- **Git runs hardened.** `GIT_TERMINAL_PROMPT=0`, `protocol.ext.allow=never`,
  `protocol.file.allow=user`, `core.fsmonitor=` (which runs a configured command
  before any question is asked), and `--no-ext-diff --no-textconv` on anything
  that produces a diff — `textconv` can forge the diff a human is reviewing.
  `-c diff.external=` is *not* the mechanism: git then execs the empty string.
- **The push destination shown is the one used.** `remote get-url --push --all`,
  because `remote.<name>.pushurl` can be set repeatedly and `git push` sends the
  update to every one of them.
- **`repo: ext::…` is refused.** pre-commit's own clone would execute it.
- **A hook configured never to fire is not coverage.** `stages: [manual]`,
  `always_run`, and an `exclude` that matches everything are all detected, and
  the stages check runs *before* `always_run` — `always_run` cannot put a hook
  back on a stage it was excluded from.
- **`--verify` requires `--facts`.** Without them nothing can be checked against
  or recorded, and a vacuous pass reports success.

## Releasing

1. Bump the version in **both** `pyproject.toml` and
   `src/manage_precommit/__init__.py` — `check-tag` compares all three against
   the tag and fails in seconds if they disagree.
2. `make verify` and `make coverage` locally.
3. Tag `vX.Y.Z` and push the tag.

`release.yml` then runs `check-tag` → `gates` → `build` → `github-release` →
`pypi`. `gates` **calls `ci.yml` as a reusable workflow** rather than repeating
its steps: a copy drifts, and the copy is the one a release would be trusting.
PyPI is last and uses Trusted Publishing (OIDC) — no token is stored. A failed
`pypi` job uploads nothing, so the version is not burned; the artifacts are
already on the GitHub Release either way.

## Provenance

The architecture here — the strict scanner and additive writer instead of a YAML
round-trip, `gitwork.py` as the sole mutator, and scripts-write-facts /
agent-relays-facts — was ported from the sibling project `manage-gitignore`,
which settled it first. If you are about to solve one of those problems again,
look there before you do.
