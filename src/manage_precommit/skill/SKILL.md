---
name: manage-precommit
description: Set up or update a repository's pre-commit hooks from a curated catalog (base hygiene, yamllint, markdownlint, a bundled mermaid-diagram validator, gitleaks), pinning the latest versions and merging into any existing .pre-commit-config.yaml without clobbering the user's own hooks, revs, or comments. Then install, test, review the diff, and — with confirmation — commit and push only the pre-commit files. Use when the user asks to add, set up, configure, refresh, or update pre-commit hooks / a .pre-commit-config.yaml, add a linter/formatter/secret-scan/markdown/mermaid/yaml check, or "set up pre-commit".
---

# manage-precommit (catalog → .pre-commit-config.yaml, keep user hooks)

Build or update `.pre-commit-config.yaml` from a small **catalog**. Each catalog
entry is a YAML **template fragment**; the engine fills it with the latest
upstream version and merges it in. Two invariants:

- **Latest, pinned** — each added repo gets the newest release tag (fetched live).
- **Never clobber** — an existing config keeps its comments, formatting, top-level
  keys, repo `rev`s, and any hooks outside the catalog. Only *missing* pieces are
  added; nothing is duplicated.

Bundled (in this skill's dir):

- `precommit.py` — deterministic engine (`--catalog`, `--detect`, generate/merge).
  Uses `ruamel.yaml` for a comment-preserving merge.
- `templates/` — one YAML fragment per catalog entry + a base skeleton.
- `assets/` — files copied into the repo: `lint-mermaid.mjs`, `.yamllint.yaml`,
  `.markdownlint.yaml`.
- `render-summary.py` — the Step 6 summary (color on a TTY, plain when piped).

```bash
python3 "<skill-dir>/precommit.py" --catalog                    # list catalog keys
python3 "<skill-dir>/precommit.py" --dir "<repo>" --detect      # existing config
python3 "<skill-dir>/precommit.py" --dir "<repo>" [--force] KEY...  # generate/merge
```

**Catalog keys:** `hygiene`, `yamllint`, `markdownlint`, `mermaid`, `gitleaks`.

**Any non-zero exit from a tool or git command stops the run** — report it
verbatim, never hand-edit files to work around it. **If AskUserQuestion is
unavailable** (headless), stop at any step needing a choice; never assume.

## Step 0 — Prerequisite: ruamel.yaml

`precommit.py` needs `ruamel.yaml`. If it exits code 3 (missing), install it once
and retry: `python3 -m pip install --user ruamel.yaml`.

## Step 1 — Scan the repository

- Git work tree? `git rev-parse --is-inside-work-tree` (must exit 0 **and** print
  `true`). Needed for Step 5.
- Existing config? `precommit.py --dir "<repo>" --detect` — reports current repos,
  revs, hook ids, and which catalog keys are already present. If it exits
  non-zero, report and stop (don't treat a failed read as "no config").
- Repo content, for recommendations: `find . -maxdepth 2 -not -path './.git/*'`.
  Note markers you actually see (name them — reproducible).
- Prerequisites for the hooks you'll propose: `pre-commit --version`; for
  `mermaid`, `node --version` and a browser (`which google-chrome || which chromium`)
  — the mermaid hook downloads Chromium (~one-time, large) if none is reused.

## Step 2 — Catalog defaults + recommendations

**Always-on (every repo):**

- `hygiene` — whitespace/EOF/large-files/merge-conflict/check-yaml/json.
- `yamllint` — the config file itself is YAML, and most repos carry YAML.

**Recommend from repo content** (name the marker):

- `*.md` present → `markdownlint`
- `*.md` containing a ` ```mermaid ` fence → `mermaid`
- any repo → offer `gitleaks` (secret scan) as a strong default.

Fold in whatever `--detect` reported as already present (don't re-add). Keep the
set relevant to what the repo actually contains.

## Step 3 — Ask the user

AskUserQuestion (multiSelect): confirm the always-on set + recommendations, offer
`gitleaks`, and note any catalog key can be added via free-text "Other". Show the
catalog with `--catalog` if asked. Do not add hooks the user didn't pick.

## Step 4 — Generate/merge, install, and test

1. Merge the selection (add `--force` when a config already exists — safe, because
   the merge preserves everything else):

   ```bash
   python3 "<skill-dir>/precommit.py" --dir "<repo>" --force <keys...>
   ```

   Read the report: repos **added** vs **already present (left as-is)**, assets
   **written** vs **kept**, and the pinned **versions**. If it reports that
   `exclude` was *not* added because the config already had one, tell the user
   `.gitignore` won't be excluded unless they add it.
   Stop on any non-zero exit (unknown key → re-ask Step 3; network/symlink/other →
   report verbatim).

2. Activate: `pre-commit install`.

3. Test: `pre-commit run --all-files`.
   - **`--all-files` only covers git-*tracked* files.** In a repo where the setup
     files (and the content they lint) are still untracked, every hook reports
     `(no files to check) Skipped` and exits 0 — a vacuous pass, not a real test.
     If you see that, re-run naming the paths explicitly, which works on untracked
     files without touching the index:
     `pre-commit run --files <setup files> <other files to check>`
     Report which form produced the result.
   - Autofixing hooks (trailing-whitespace, end-of-file-fixer, mixed-line-ending)
     **modify files and exit non-zero on first run**. Re-run once; a clean second
     pass is success. Explain this rather than treating the first exit as failure.
   - These autofixers may touch files **beyond** the pre-commit setup (anywhere in
     the repo). That is expected; those edits are the user's to review — the skill
     will **not** commit them (Step 5 commits only the setup files).
   - Genuine failures (gitleaks finds a secret, a linter errors) are real — report
     them; the user fixes the content or adjusts config. Never weaken a hook just
     to make the run pass.

## Step 5 — Review the diff, then commit (setup files ONLY)

This skill commits **only** the pre-commit setup files it created/changed:
`.pre-commit-config.yaml`, and whichever of `.yamllint.yaml`, `.markdownlint.yaml`,
`scripts/lint-mermaid.mjs` it wrote. Never bundle other changes (including hook
autofixes elsewhere), and never suggest doing so.

**Check the exit status of every git command; stop on any non-zero.**

1. Show the setup diff: `git diff -- <setup files>` (or `git status` for new ones).
   Sanity-check the merge: user hooks/comments intact, no duplicates, latest revs
   on added repos.
2. If **not** a git repo, stop and report the files written.
3. AskUserQuestion — exactly: **Commit + push** / **Commit only** / **Don't commit**
   (no "also commit other changes" option).
4. On commit, stage and commit ONLY the setup files:
   - Write the commit message to a file with the **Write tool**.
   - `git add -- <setup files>`
   - `git commit --only -F "<msgfile>" -- <setup files>`
   - Verify: `git show --name-only --format= HEAD` lists only those files. If it
     lists anything else, stop and report.
5. Push only if chosen and an upstream exists. Compare first — never force blindly:
   - `git fetch`, then `git rev-list --left-right --count HEAD...@{u}` → `<ahead>\t<behind>`.
   - **behind == 0**: `git push` (fast-forward, no force).
   - **behind > 0**: force needed → STOP, show what force would drop
     (`git log --oneline HEAD..@{u}`) and what replaces it (`git log --oneline @{u}..HEAD`)
     plus the counts, explain it's irreversible, then AskUserQuestion
     **Force-push** / **Skip push**. Only on "Force-push":
     `git push --force-with-lease origin <branch>`.

## Step 6 — Final summary

Assemble run facts into a JSON file with the **Write tool** (schema at the top of
`render-summary.py`), then:

```bash
python3 "<skill-dir>/render-summary.py" <facts.json>
```

Show its output as the closing summary. Cover, with the reason beside each choice:
scan; hooks added vs left-as-is + recommendation reasons + pinned versions; files
written vs kept; verify (install + test result); commit/push; net repos + diffstat.
Put pre-run context (e.g. a history reset) in `notes`.

## Rules

- Never clobber: existing hooks, revs, comments, and non-catalog config stay as the
  user left them. Only add what's missing; never duplicate.
- Added repos get the latest release tag (fetched live); the mermaid dep gets the
  latest npm version. Do not hardcode versions.
- Commit **only** the pre-commit setup files. Never stage, commit, or suggest
  committing anything else — including files autofixed by the hooks.
- Never hand-write or hand-edit `.pre-commit-config.yaml` to work around a tool
  error. Report the error and stop.
- Commit messages: write to a file with the Write tool and use `git commit -F`.
  Never a shell heredoc or `-m "$(...)"` (this environment injects ANSI bytes into
  heredocs/command substitution — see the `shell-colorizes-file-writes` memory).
- Never `git push` without explicit confirmation; never force-push without the
  compare-and-confirm in Step 5.
