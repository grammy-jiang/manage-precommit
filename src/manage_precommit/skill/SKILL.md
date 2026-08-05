---
name: manage-precommit
description: Set up or update a repository's pre-commit hooks from a curated catalog (base hygiene, yamllint, markdownlint, a bundled mermaid-diagram validator, gitleaks), pinning the latest versions and merging into any existing .pre-commit-config.yaml without clobbering the user's own hooks, revs, or comments. Then install, test, review the diff, and — with confirmation — commit and push only the pre-commit files. Use when the user asks to add, set up, configure, refresh, or update pre-commit hooks / a .pre-commit-config.yaml, add a linter/formatter/secret-scan/markdown/mermaid/yaml check, or "set up pre-commit".
allowed-tools:
  - Read
  - Write
  - Bash
  - AskUserQuestion
---

# manage-precommit

A `.pre-commit-config.yaml` here is two things: the **catalog entries** this skill
manages, inserted as whole blocks of text with their versions pinned live, and
**everything else the user wrote** — their own hooks, revs, comments, formatting
and top-level keys — which is carried across byte for byte.

## Division of labour

**Anything a program can decide, a program decides.** The scripts in `scripts/`
own every mechanical step — scanning the repo, recommending, merging, pinning,
verifying the write, judging the hook run, tracked-vs-untracked, the diff, the
commit, and what a push may do.

**Yours:** ask the user, judge their answers, write the commit message, relay
what the tools say. That is all. Never re-derive a tool's answer by eye, never
reformat its output into your own numbers, and never run `git add`/`commit`/`push`
or `pre-commit` yourself — `scripts/gitwork.py` is the only path to a mutation,
and it fails closed.

## Placeholders

`<skill-dir>` is the directory holding this SKILL.md — usually
`~/.claude/skills/manage-precommit`. `<repo>` is the repository being worked on.
Substitute both; never run a command with the angle brackets still in it.

The scripts need only Python 3.10+, `git`, and — for the `mermaid` entry — `npm`.
They import each other from their own directory, so they run from wherever the
skill is installed, with nothing to install first. If one is missing, say so and
stop; do not fall back to hand-written git or a hand-written config.

**Any non-zero exit stops that action** — report it verbatim. The recoverable
exceptions are documented where they occur.

**If AskUserQuestion is unavailable** (headless), stop at the first choice and
say which confirmation is needed. Never assume an answer.

**Pick `<facts.json>` once, in Step 1, and pass that same path to every `--facts`
and `--facts-out` after it.** A different path is not an error — it silently loses everything recorded
so far. It must be a `mktemp` path **outside** the repo; the tools refuse one
inside it.

## Step 0 — Inspect only

If the user only wants to *see* what a repo already runs:

```bash
python3 "<skill-dir>/scripts/precommit.py" --dir "<repo>" --detect
```

Relay it and stop. Nothing is written, so there is nothing to review or commit.

## Step 1 — Scan

```bash
python3 "<skill-dir>/scripts/precommit.py" --dir "<repo>" --recommend \
  --facts-out "<facts.json>"
```

`--facts-out` records what was detected and what was recommended, so the closing
summary can show them. Later steps add to that file rather than replacing it.

Returns JSON: `always_on` (fixed policy), `recommended` (`[{name, reason}]`, where
`reason` is the file that triggered it), `previous` (catalog entries the config
already has), `proposed` (the starting set for Step 2), and `detected` (the
markers actually seen). Do not scan the tree yourself or second-guess a `reason`.

## Step 2 — Ask

**`always_on` is not up for a vote.** State it as a fact with its reason:
"always included (repo-independent hygiene — whitespace, EOF and merge-conflict
markers turn up whatever this project is written in, and the config file itself
is YAML): `<the names Step 1 returned>`."

Ask about `recommended`, each with its `reason` — `markdownlint ← README.md`.
Anything already in `previous` is not offered again; say it is already there.
Offer a free-text "Other" — *exact catalog names, comma-separated*. A near-miss is
rejected by the tool, not quietly corrected. Show the catalog with `--catalog` if
asked.

Before offering `mermaid`, check its prerequisite and say what you found:

```bash
command -v npm >/dev/null && command -v node >/dev/null && echo present || echo missing
```

If either is missing, say so in the question — **and say that picking it anyway
aborts the whole write, not just that entry**: the version pin happens before
anything is written, so a missing `npm` means none of the other selected hooks
get written either. Also say the hook downloads a headless Chromium the first
time it runs (large, one-off) unless one is already reusable.

The final list is `always_on` plus whatever the user selected. **Write it to a
file with the Write tool**, **one name per line**, at a `mktemp` path outside
the repo — free-text names must never reach a command line, for the same reason
commit messages go through a file.

An "Other" answer arrives comma-separated; the file is not. Split it on commas,
trim each name, and write one per line. Written as a single `gitleaks, mermaid`
line the whole string is read as one catalog name, and the run fails with a
near-match for something the user never typed.

## Step 3 — Write

```bash
python3 "<skill-dir>/scripts/precommit.py" --dir "<repo>" --force \
  --templates-file "<keys.txt>" --facts-out "<facts.json>"
```

Pass `--force` whenever a config already exists; it is safe precisely because the
merge only ever *inserts*. The tool verifies its own write before reporting
success — the merged file is re-scanned, every selected entry is confirmed
present, and any control or text-reordering character aborts the write.

You delete both temp files once each command returns: `rm -f "<keys.txt>"`, and
`rm -f "<msgfile>"` after Step 5. The tools never unlink their inputs.

**On a non-zero exit:**

- **exit 3, unknown catalog key** — the only recoverable case. Re-ask *for the
  rejected names only*, quoting the near matches it printed (`"gitleeks" is not a
  catalog key — did you mean gitleaks?`). Then write a fresh `<keys.txt>`
  containing the **ENTIRE corrected selection** — every key that was already
  accepted, plus the corrected one(s) — never the corrected name alone. The file
  is read as the whole selection with no memory of the previous attempt, so
  writing only the fix silently drops every other hook the user asked for.
  **Retry once.** If that fails, report the near matches and stop.
- **exit 4, a file already carries an uncommitted change** — that edit is the
  user's to commit, stash or discard; no rebuild can honestly commit it as this
  run's work. Relay it and stop. Once they have dealt with it the run starts
  again from Step 1.
- **exit 5, the config uses YAML this tool will not read** — anchors, aliases,
  merge keys, flow sequences, more than one document. It says which line. Tell
  the user the hook has to be added by hand, or the construct simplified. Do not
  edit the file yourself.
- **anything else** — nothing usable was written. Report it and end the run.

On success relay its report: entries **added** vs **already present (left
as-is)**, assets **written** vs **kept**, and the pinned **versions**. If it says
`exclude: left as-is`, tell the user `.gitignore` will not be excluded unless
they add it themselves.

**If `needs_manual` is non-empty, say so plainly.** That entry exists but its
`hooks:` list is not a shape this tool can extend, so the hook the user asked
for **was not installed** and they have to add it by hand. It is an exit-0
outcome that otherwise reads as success — name each one.

## Step 4 — Verify

**Ask before running it.** This is the step that changes files, and it is not
scoped to this run's work: `--all-files` runs *every* hook — including ones the
user already had — over *every tracked file*, and the autofixing ones rewrite
what they touch. Step 3's guard covers only the files this run writes, so an
unrelated file holding uncommitted work can be rewritten here. Say that, then
AskUserQuestion: **Run the hooks over all files** / **Only this run's files** /
**Skip verification**.

- *All files* — the full check, and the honest one; autofixes elsewhere are
  reported in Step 5 and never committed by this skill.
- *Only this run's files* — pass `--files` with `files.written` from the facts.
  Narrower, and it will not tell you whether the hooks pass on the rest of the
  repo.
- *Skip* — nothing is installed and nothing is checked. `pre-commit install`,
  which writes `.git/hooks/pre-commit`, runs only inside this step, so skipping
  it leaves the config written and **no hook active**: the next `git commit`
  triggers nothing. Say exactly that, go to Step 5, and record
  `--note "verification skipped; git hook not installed"`. They can install it
  later by re-running this step, or by hand with `pre-commit install`.

```bash
python3 "<skill-dir>/scripts/precommit.py" --dir "<repo>" --verify --facts "<facts.json>"
```

Installs the git hook and runs it. **Read `run_ok`, not the exit code of your own
reading of the output** — the tool already judged two outcomes that look like
success and are not.

That applies only when there *is* JSON. `pre-commit` missing from PATH, a failed
install, or a timeout all stop before anything is emitted: non-zero exit, empty
stdout. There is no `run_ok` to read, so the general rule applies — report it
verbatim and stop.

- `vacuous: true` — every hook reported `(no files to check)`. `--all-files`
  covers only git-*tracked* files, so in a repo where the setup files are still
  untracked the run passes having checked nothing. Re-run naming the paths
  explicitly, which works on untracked files without touching the index:

  ```bash
  python3 "<skill-dir>/scripts/precommit.py" --dir "<repo>" --verify \
    --facts "<facts.json>" --files <the setup files> <other files to check>
  ```

  Say which form produced the result you report.
- `autofixed` non-empty — the autofixing hooks (trailing-whitespace,
  end-of-file-fixer, mixed-line-ending) rewrote files and exited non-zero on the
  first run; the tool re-ran once and a clean second pass is the success. Those
  edits **may touch files anywhere in the repo**. That is expected, they are the
  user's to review, and Step 5 will not commit them.

A genuine failure (gitleaks finds a secret, a linter errors) is real — report it;
the user fixes the content or adjusts config. Never weaken a hook to make the run
pass.

## Step 5 — Review, commit, push (this run's files ONLY)

Never stage, commit, or suggest committing any other file. Step 3 refused to
start from a setup file that already carried an uncommitted edit, so everything
the diff shows here is this run's work. That is what makes it honest to commit
those files whole.

### 1. Show the diff

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" status --facts "<facts.json>"
```

`files` is what this run may commit; `states` gives each one's git state. It
picks the right comparison per file and returns the real diff — show it. If
`suspicious_characters` is true, say so: the terminal may not be rendering what
the files say.

Two outcomes skip the rest of Step 5 — go to Step 6 with
`--choice "not committed"`, no `--hash`, and a `--note` saying which:

- `is_repo: false` → `--note "not a git repo"`
- `changed: false` → `--note "no change: the config already matched"`

Read the diff for what *the user* should weigh — a hook that will reformat their
whole tree, an `exclude` that is not what they expected. If `gitleaks` is being
added, say what it actually does: it scans each future **staged commit**, not
existing history. This catalog installs the staged-diff hook, not
`gitleaks-full`, so anything already committed is not covered.

### 2. Ask

Assemble everything the answer depends on first, so the user approves the actual
change and not an intention:

- **Draft the commit message now, on one line**, and show it. The summary
  records only the subject, so a body would be approved and never shown back —
  and `commit` **refuses outright** if the message file holds more than one
  non-blank line. It does not truncate. If the user supplies several lines,
  take the first, show it back, and write only that line to the file. If a
  commit does fail citing the line count, rewrite the file with one line and
  re-run the same command.
- **Say what else this run touched.** If Step 4 reported a non-empty
  `autofixed`, say so plainly *before* asking: "verifying the hooks also
  modified `<those files>` elsewhere in your tree. This run will not stage or
  commit them — they are yours to review and commit separately." Without this
  the user approves a commit believing their tree is as clean as the diff they
  were shown, and only finds out from the summary, after the fact.
- **Say the files are already written.** *Don't commit* leaves them on disk; it
  does not undo them. Name the right discard for the `state` that `status`
  reported for each file, because they differ:
  - `modified` (unstaged only) → `git checkout -- <path>`
  - `staged` → `git restore --staged --worktree -- <path>`. Plain
    `git checkout --` here restores the work tree *from the index*, which for a
    staged file changes nothing and leaves it staged.
  - `untracked` (the common first run) → `rm <path>`
- **Name where a push would go**, from the tool rather than by re-deriving it:

  ```bash
  python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" push-plan
  ```

  Say its `guidance` sentence — it already names the destination *and its URL*,
  because a remote's nickname says nothing about where code goes. If
  `suspicious_characters` is true, say that too.

  **This plan describes the state before the commit exists.** On a branch level
  with its upstream it returns `stop-up-to-date` — "nothing to push" — which
  stops being true the moment item 3 commits. So use it to name the
  *destination*, not to predict whether a push will happen: never tell the user
  a push looks unlikely on the strength of `permits_push` here. Step 5.4
  recomputes it after the commit, and that is the one that decides. The three
  options below never change either way.

Then AskUserQuestion — exactly these, never an "also commit other changes"
option: **Commit + push** / **Commit only** (local) / **Don't commit**.

### 3. Commit

Only on *Commit + push* or *Commit only*. Write **the exact text shown in item
2** to a `mktemp` file with the Write tool — do not redraft it — then:

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" commit \
  --message-file "<msgfile>" --facts "<facts.json>"
```

It stages only this run's files, commits with `--only` so the rest of the index
survives, and proves the commit holds exactly those files *and the content this
run verified*.

**Read `verdict`:**

- `ok` — keep the returned `hash` for Step 6.
- anything else — **do not push.** The JSON carries `remedy` (what the user can
  run) and `record_choice` / `record_note` (what Step 6 must record). Relay the
  remedy; **never run it yourself** — discarding a commit that exists is the
  user's call. Do not pass the hash to Step 6.

A non-zero exit with no verdict means nothing was committed. The index is as
you found it **except** in one case, which the tool says out loud: if stderr
carries `AND the cleanup reset also failed`, this run's files may still be
staged. Relay that message verbatim, tell the user to check `git status` before
doing anything else, and stop. Otherwise report the error and stop.

### 4. Push

Only if the user chose a push option.

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" push-plan
```

- `permits_push: false` → report `guidance` and go to Step 6. None of these is an
  error to fix; `stop-up-to-date` is a success.
- `action: "diverged"` → [references/push-safety.md](references/push-safety.md).
  Keep `upstream_sha`; the force needs it.
- `action: "no-upstream"` **and `remote` is `null`** (several remotes, no
  `origin`) → ask first. Show each candidate **with its URL** from
  `remote_urls`. Then **write the chosen name to a file with the Write tool**,
  at a `mktemp` path outside the repo, and pass the file:

  ```bash
  python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" push \
    --remote-file "<remote.txt>" --facts "<facts.json>"
  ```

  Never interpolate the name into the command instead. Remote names come from
  repository config and git permits `"`, `;`, `$` and backticks in them, so a
  crafted name would close the quote and run as a second shell command — the
  same reason catalog selections and commit messages go through files. Delete
  the file once the command returns.

  Running the plain command here instead returns `error: "ambiguous-remote"` and
  exit 5. That is the tool asking for this question, not a failed push — ask it
  rather than abandoning a legitimate first push. `error: "unknown-remote"` (also
  exit 5) means the name did not match; re-show the candidates and ask again,
  once.
- otherwise:

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" push --facts "<facts.json>"
```

`push` recomputes the plan and executes only what it permits, by explicit
refspec, so `push.default=matching` can never widen it. It refuses to force
outside a diverged branch.

**Except for the two questions above, a push that did not happen appears two ways** — JSON with `pushed: false`, or a
non-zero exit with no JSON. Treat both the same: report it, and go to Step 6 with
no push recorded.

## Step 6 — Summary

The ordinary path passes neither `--choice` nor `--hash`:

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" facts --facts "<facts.json>" \
  --note "<why, when needed>"
```

**Do not pass `--choice` for the ordinary path.** The outcome is already
recorded — `commit` wrote the hash, `push` wrote where it landed — so the tool
derives it. Passing a hand-typed value would be re-deriving recorded state from
a prose table, which is exactly what this design exists to avoid.

Pass it only when nothing was attempted and there is therefore nothing to
derive:

| situation | add |
| --- | --- |
| the user said *Don't commit* | `--choice "not committed"` |
| a Step 5 item 1 shortcut (not a repo / no change) | `--choice "not committed" --note "<which>"` |
| a bad commit (`verdict` ≠ `ok`) | the JSON's own `record_choice` and `record_note` |
| verification was skipped | `--note "verification skipped; git hook not installed"` |

`--hash` is never required: `commit` already recorded the hash it verified.
Pass it only to have the tool re-check a specific commit, and only when
`verdict` was `ok` — it is verified, not believed. `--note` repeats, and appends
without touching computed fields — never hand-edit the file.

```bash
python3 "<skill-dir>/scripts/summary.py" "<facts.json>"
```

That output *is* the closing summary; do not hand-format a second one. Then
`rm -f "<facts.json>"`. A worked example is in
[references/example-output.md](references/example-output.md).

## Rules

- This skill manages **its catalog** plus whatever the config already contains. A
  request for a hook outside the catalog (`--catalog` lists it) is an ordinary
  edit outside this skill — say so plainly rather than approximating with a
  nearby entry.
- Never hand-write or hand-edit `.pre-commit-config.yaml`, and never edit it to
  work around a tool error. If the tool refuses, report the refusal and stop.
- Never run `git add`/`commit`/`push`, or `pre-commit`, directly.
- This skill modifies and commits **only** the files it wrote this run, listed in
  the facts. Never bundle anything else — including files the hooks autofixed
  elsewhere — and never suggest doing so.
- Commit messages go through a file and `--message-file`. Never a heredoc or
  `-m "$(...)"` — some shells inject ANSI bytes into both, and those end up
  stored in the commit.
- Never push without explicit confirmation, and never force without the separate
  confirmation in [references/push-safety.md](references/push-safety.md).
