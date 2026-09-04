---
name: manage-precommit
description: Set up or update a repository's pre-commit hooks from a curated catalog (base hygiene, yamllint, markdownlint, two bundled mermaid-diagram checks — one parses without a browser, one renders — gitleaks), pinning the latest versions and merging into any existing .pre-commit-config.yaml without clobbering the user's own hooks, revs, or comments. Then install, test, review the diff, and — with confirmation — commit and push only the pre-commit files. Use when the user asks to add, set up, configure, refresh, or update pre-commit hooks / a .pre-commit-config.yaml, add a linter/formatter/secret-scan/markdown/mermaid/yaml check, or "set up pre-commit".
license: MIT
compatibility: Linux and macOS; not Windows, which lacks the POSIX shell this procedure needs. Requires python3 3.10+, git and pre-commit on PATH; the mermaid entries also need node and npm. Writes only the pre-commit setup files of the repository it is pointed at, plus temporary files outside it. Runs under Claude Code, Codex and GitHub Copilot CLI, though its questions need a host that can reach a user.
allowed-tools: Bash(python3:*) Bash(mktemp:*) Bash(rm:*) Read Write
metadata:
  homepage: https://github.com/grammy-jiang/manage-precommit
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

`<skill-dir>` is the directory holding this SKILL.md —
`~/.claude/skills/manage-precommit` under Claude Code,
`~/.agents/skills/manage-precommit` under Codex or GitHub Copilot. `<repo>` is
the repository being worked on. Substitute both; never run a command with the
angle brackets still in it.

The scripts need Python 3.10+ and `git`; `pre-commit` itself from Step 4
onward; and `npm` for the `mermaid-parse` and `mermaid` entries.
They import each other from their own directory, so they run from wherever the
skill is installed, with nothing to install first. If one is missing, say so and
stop; do not fall back to hand-written git or a hand-written config.

**Any non-zero exit stops that action** — report it verbatim. The recoverable
exceptions are documented where they occur.

## What this skill needs from you

Two capabilities, named by what they do rather than by any one agent's tool
names, because this skill runs under several:

- **Ask a question and wait for the answer.** Claude Code has AskUserQuestion,
  which renders the options as a menu; elsewhere, ask in prose and wait. The
  options given at each step are the options — never add one, never drop one,
  never assume an answer, and never proceed on silence. **If no user can be
  reached at all** — a headless or non-interactive run — stop at the first
  choice and say which confirmation is missing. This skill installs git hooks
  and can commit and push; not one of those happens unasked.
- **Write and read a file directly.** Where a step says to write a file, use
  your file-write tool rather than a shell heredoc. Repo filenames and commit
  messages are arbitrary text, and putting them through a shell is how a
  quote or a backtick becomes a command.

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
already has), `disabled` (of those, the ones that look switched off, with why),
`detected` (the markers, as prose) and `detected_paths` (the same files as bare
paths). Do not scan the tree yourself or second-guess a `reason`.

The payload also carries `proposed`. **Ignore it here.** It is the union Step 2
would arrive at if the user accepted everything, and reading it as a default is
the one way to get Step 2 wrong: nothing is pre-selected, and every recommended
item starts unchosen.

## Step 2 — Ask

**`always_on` is not up for a vote.** State it as a fact with its reason:
"always included (repo-independent hygiene — whitespace, EOF and merge-conflict
markers turn up whatever this project is written in, and the config file itself
is YAML): `<the names Step 1 returned>`."

Ask about `recommended`, each with its `reason` — `markdownlint ← README.md`.
**For `gitleaks`, say what it actually covers as part of the offer**: it scans
each future *staged commit*, not existing history, so anything already
committed is not covered (that is `gitleaks-full`, which this catalog does not
install). Someone who wanted history scanned has to learn that before they say
yes, not at Step 5 with the config already written.
Anything already in `previous` is not offered again; say it is already there —
**unless it also appears in `disabled`**. That means the entry exists but looks
like it will not run: `stages` that exclude the commit, a `files`/`exclude`
scope that lets no file in the repository through, a pattern pre-commit would
refuse to load, a `types`/`types_or`/`exclude_types` filter no file the entry is
for can pass (`exclude_types: [markdown]` on a Markdown check), `pass_filenames:
false` on a hook whose program reads its file list, or a scope that never
reaches the file the scan found the fence in. One reason is worded **not
shown**: a filter this tool could not read stands between the hook and that
file — a block-scalar pattern, or a type filter only `identify` could judge —
and then say the coverage is not shown, not that it is absent. Say
which, and say it is not the coverage it appears to be. **Do not offer to add a working one — this skill cannot.** The fragment
declares the same hook id that is already present, so selecting it again writes
nothing and changes nothing; the merge only ever inserts and never edits an
existing entry's `stages`/`files`/`exclude`. The fix is a hand edit of that
entry, the same as a `needs_manual` case. Being told "gitleaks is already
there" about a scanner configured never to run is worse than not being told at
all — and being told the skill will repair it is worse still. One exception,
which the tool decides for you: a switched-off mermaid entry does not stop the
scan recommending its alternative — `mermaid-parse` beside a dead `mermaid`,
`mermaid` in place of a dead `mermaid-parse` — because that is a different hook
id and can be added. When it appears in `recommended`, offer it the ordinary
way — as a working check beside the dead one, not as a repair of it — and
still say the dead one is dead.
Offer a free-text "Other" — *exact catalog names, comma-separated*. A near-miss is
rejected by the tool, not quietly corrected. Show the catalog with `--catalog` if
asked.

Before offering `mermaid-parse`, relay `prerequisites.mermaid-parse` from Step 1
— the scan already looked. `binaries present` means `node` and `npm` are on
PATH and nothing beyond that: the version pins are attempted in Step 3 and can
still fail there. If it is anything else, say so in the question — **and say
that picking it anyway aborts the whole write, not just that entry**: the
version pin happens before anything is written, so a missing `npm` means none
of the other selected hooks get written either.

**Say what `mermaid-parse` checks, as part of the offer.** It parses each fenced
diagram with Mermaid's own parser and no browser, so it catches syntax errors
and only those: a diagram that fails only when it is *rendered* gets through.
Name `mermaid` as the alternative — the same fences, rendered with mermaid-cli,
which catches those too, at the cost of a headless Chromium the hook downloads
the first time it runs (large, one-off) unless one is already reusable. They
check the same thing, so offer **one or the other, not both**; the scan never
recommends `mermaid`, and a user who wants it asks for it by name. Whichever is
chosen, `prerequisites.<that key>` is the value to relay for it.

**This applies whenever either entry ends up in the final selection**,
including when the user types one into "Other" after seeing `--catalog`. The
catalog line carries no live check and no warning that a missing `npm` voids
every other hook they chose. Run the check and say all of it before accepting
the selection, not only when you were the one offering it.

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
- **exit 4** — two different causes, and the message says which. **Relay it
  verbatim rather than narrating either from memory.**
  - *a file already carries an uncommitted change* — that edit is the user's to
    commit, stash or discard; no rebuild can honestly commit it as this run's
    work. Once they have dealt with it the run starts again from Step 1.
  - *the check itself could not run* ("could not check whether this run's files
    are already modified") — a locked or corrupt index, not an edit. There is
    nothing for the user to discard; the run stops because an unknown state is
    not a clean one. Suggest they resolve the git error and start again.
- **exit 5, the config uses YAML this tool will not read** — anchors, aliases,
  merge keys, flow sequences, more than one document, or a `repo:` naming a git
  *transport helper* (`ext::`, `fd::` and friends). It says which line. The last
  of those is not a formatting quibble: such a URL names a program git runs when
  `pre-commit` clones it, and Step 4 runs `pre-commit`, which uses its own git
  and none of this skill's hardening. Say plainly that the existing config
  contains one and that this skill will not carry it forward. Otherwise tell the
  user the hook has to be added by hand, or the construct simplified. Do not
  edit the file yourself.
- **exit 6, a version could not be pinned** — **nothing was written.** Every
  version is fetched before the first byte of config, so this is always a repo
  left exactly as it was found; say so, because "it failed partway" is the
  reasonable assumption and it is wrong. The JSON on stdout carries `source`
  (`npm`, `git`, or `scratch` for a failure that precedes both), `target` (the
  package or repository), and `cause`. Use the cause — do not re-derive it from
  the English:
  - `filesystem` — something could not be written. **Read `npm_path` before
    saying what to fix, and check whether it is set at all.** Pinning always
    works in a temporary directory and passes its own `--cache` inside it, so
    the *temporary filesystem* is the usual subject — full, read-only, or a
    `TMPDIR` this process cannot use — and telling them to change
    `NPM_CONFIG_CACHE` or `HOME` is advice that cannot work. A set `npm_path` is
    the directory to name. An **empty** one means the scratch directory itself
    could not be made, so there is no path to name: say that the temporary
    filesystem is unusable and quote the message, which carries the error.
  - `auth` — the registry wants credentials this environment does not have.
  - `forbidden` — the registry refused the request, and that is *not* the same
    as wanting credentials: npm labels any HTTP failure `E<status>`, so a 403 is
    as likely a company registry blocking a package by policy, or an account
    that authenticated and is not permitted. Say both possibilities and let them
    tell which; do not send them to fix a login that may be working.
  - `not-found` — a registry answered that there is no such package. `registry`
    names it and **`registry_is_public` says whether it was npm's own** — do not
    compare the URL yourself, the same registry is written several ways. False
    means the likely cause is a mirror or proxy that does not carry this
    package: theirs to fix by pointing npm somewhere that does, and not a bad
    package name — this skill deliberately does not override their registry.
    Only when it is true is the catalog itself wrong, and that is a bug here
    rather than anything they can do. **An empty `registry`, with no
    `registry_is_public` beside it, means npm would not say which registry it
    asked.** Attribute nothing then: report that the package was not found and
    that the registry could not be identified. Two ordinary causes are worth
    offering — npm withholds a registry carrying credentials, and it refuses
    every `npm config` command outright when their npm configuration selects a
    workspace (`workspace=` in an `.npmrc`). Either way it is theirs to look at
    with `npm config get registry`, not something to guess at from here.
  - `network` — DNS, connection or TLS. Worth retrying once; say that it is a
    reachability problem and not their repository.
  - `timeout` — a remote answered too slowly rather than not at all. **Say which
    one, from `source`**: `npm` is the registry, `git` is the hook repository's
    host, and sending someone to check the wrong service is worse than saying
    nothing. Otherwise the same advice as `network`.
  - `npm-missing` / `unrunnable` — the tool named by `source` is absent, or is
    there and would not start. For `npm` that is only the mermaid entries'
    problem (`mermaid-parse`, `mermaid`): every other selection succeeds
    without it. For `git` it stops the whole run, and "would not start" means
    something on their PATH is broken rather than missing — quote the message,
    which names it.
  - `invalid-version` — the registry answered with something that is not a
    version, and it was refused rather than written into their config.
  - `git-ls-remote` — the hook repository's lookup failed, and that is *all*
    this one means. git offers no machine-readable code the way npm does, so
    everything the run knows is in `detail` — **relay it verbatim.** It covers
    an unreachable host, TLS, credentials and a repository that is not there,
    none of which is a version-tag problem, and none of which is worth guessing
    at from the wording.
  - `no-version-tags` — the repository answered, and carries no tag that is
    purely a version. Nothing to retry: either this catalog's `rev_repo` is
    wrong, which is a bug here, or upstream has stopped tagging releases.
  - `not-isolated` — nothing was attempted. The scratch directory pinning works
    in could not be sealed off from whatever project encloses it, so git or npm
    might have taken configuration from a repository that has no business
    choosing which server answers for a catalog URL. The message says what
    failed. Not the user's configuration to fix — relay it and stop.
  - `unknown` — an npm code with no bucket here. Relay `npm_code` and `detail`
    verbatim and say it is unclassified, rather than picking the nearest label.
- **anything else** — report it verbatim and end the run. Two of these happen
  *after* the config has been written, and the message says so: a foreign
  executable asset appearing between the pre-check and the copy ("The config has
  been written and would run it as a hook"), and the post-write verification
  failing ("was written but `<key>` is not in it"). In those two cases tell the
  user a live `.pre-commit-config.yaml` is now in their tree, name any file the
  message named, and say to inspect or delete both before doing anything else.
  Every other exit here wrote nothing.

On success relay its report: entries **added** vs **already present (left
as-is)**, assets **written** vs **kept**, and the pinned **versions**.

If it says `exclude: left as-is`, **show the pattern it printed**. Two things
follow from it, and only the first is obvious: `.gitignore` will not be excluded
unless they add it themselves, and anything the existing pattern matches is
skipped by *every* hook — including the ones just added. A broad one (`.*`, or
something matching most of the tree) means the hooks are installed and scanning
nothing. That line is pre-existing, so it appears in no diff this run produces;
if you do not say it, nobody sees it.

**If `needs_manual` is non-empty, say so plainly.** That entry exists but its
`hooks:` list is not a shape this tool can extend, so the hook the user asked
for **was not installed** and they have to add it by hand. It is an exit-0
outcome that otherwise reads as success — name each one.

## Step 4 — Verify

**First, find out what is actually at risk.** This is the step that changes
files, and it is not scoped to this run's work: `--all-files` runs *every* hook
— including ones the user already had — over *every tracked file*, and the
autofixing ones rewrite what they touch. Step 3's guard covers only the files
this run writes, so an unrelated file holding uncommitted work can be rewritten
here. Do not leave that as a hypothetical the user has to imagine:

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" status --facts "<facts.json>"
```

Read `dirty_elsewhere`. **Name those files in the question**, or say plainly
that there are none. A user choosing "all files" is accepting that those exact
files may be rewritten; asked in the abstract, they accept a risk they cannot
see, and by the time Step 5.1 shows them the real state the rewrite has already
happened.

**Then ask** — **Run the hooks over all files** / **Only this
run's files** / **Skip verification**. Each option has its own command — run the
one that matches the answer, and nothing else.

Two things belong in the question text itself, not only in the handling below,
because they are what the answer turns on: the `dirty_elsewhere` files named
above, and — on the Skip option — that **skipping leaves no git hook installed**,
so the next `git commit` runs nothing. `pre-commit install` happens only inside
this step. Someone picking Skip to avoid a slow check is not choosing to leave
the repo unprotected, and finding that out afterwards is finding out too late.

- *All files* — the full check, and the honest one; autofixes elsewhere are
  reported in Step 5 and never committed by this skill.

  ```bash
  python3 "<skill-dir>/scripts/precommit.py" --dir "<repo>" --verify --facts "<facts.json>"
  ```

- *Only this run's files* — write `files.written` to a `mktemp` file with the
  Write tool and pass `--files-file`, never `--files` (see the recovery block
  below for why). Narrower, and it will not tell you whether the hooks pass on
  the rest of the repo.

  ```bash
  python3 "<skill-dir>/scripts/precommit.py" --dir "<repo>" --verify \
      --facts "<facts.json>" --files-file "<paths.txt>"
  ```

- *Skip* — **run neither command.** Nothing is installed and nothing is checked.
  `pre-commit install`, which writes `.git/hooks/pre-commit`, runs only inside
  this step, so skipping it leaves the config written and **no hook active**:
  the next `git commit` triggers nothing. Say exactly that, then continue to
  Step 5 as normal; when you reach Step 6, add `--note "verification skipped;
  git hook not installed"` to the `gitwork.py facts` call — `--note` is a Step 6
  flag and no Step 5 subcommand accepts it. They can install it later by
  re-running this step, or by hand with `pre-commit install`.

Whichever command ran installs the git hook and runs it. **Read `run_ok`, not the exit code of your own
reading of the output** — the tool already judged two outcomes that look like
success and are not.

That applies only when there *is* JSON. `pre-commit` missing from PATH, a failed
install, or a timeout all stop before anything is emitted: non-zero exit, empty
stdout. There is no `run_ok` to read, so the general rule applies — report it
verbatim and stop.

- `vacuous: true` — every hook reported `(no files to check)`, so the run
  checked nothing. **Not a pass.**
- `unchecked` non-empty — a hook *this run added* never saw a file it matches.
  **Not a pass**, however green the run looks.
- `autofixed` non-empty — the autofixing hooks rewrote files and the tool
  re-ran; a clean second pass is the success, and those edits may touch files
  anywhere in the repo. Step 5 discloses them.

For any of the three, recover with
[references/verify-recovery.md](references/verify-recovery.md) before treating
the run as done. A genuine hook failure — gitleaks finds a secret, a linter
errors — is real: report it, and **never weaken a hook to make the run pass.**

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

**Relay `native_hooks` and `local_overrides` if either is non-empty**, before
the question in item 2:

- `native_hooks` — git hooks *this skill did not install* (`pre-push`,
  `commit-msg` and friends) that will run during the commit and push you are
  about to ask about. They execute code that appears in no diff. A repository
  that arrived with its `.git` intact — a tarball, a zip, a clone of someone
  else's — can carry them. Name them and say they will run.
- `local_overrides` — settings in this repository's own config that make git
  run a program or hand over credentials. Name them too.

Neither is refused: a repo legitimately having its own hooks is ordinary, and
so is a deploy key. What is not ordinary is finding out afterwards.

**If `next` is `summary`**, the rest of Step 5 does not apply: go to Step 6 with
`--choice` and `--note` set to the `record_choice` and `record_note` the tool
returned, and no `--hash`.

Read the diff for what *the user* should weigh — a hook that will reformat their
whole tree, an `exclude` that is not what they expected. If `gitleaks` is being
added, repeat the scope you gave at Step 2 as a reminder: it scans each future
**staged commit**, not existing history.

### 2. Ask

Assemble everything the answer depends on first, so the user approves the actual
change and not an intention:

- **Draft the commit message now, on one line**, and show it. The summary
  records only the subject, so a body would be approved and never shown back —
  and `commit` **refuses outright** if the message file holds more than one
  non-blank line. It does not truncate. If the user supplies several lines,
  take the first, show it back, and write only that line to the file -- and
  **say that the rest was dropped**: "only the first line becomes the commit
  subject; the rest of what you wrote is not included". Shown a tidy one-liner
  with no such note, a user has no signal that the body they wrote is gone. If a
  commit does fail citing the line count, rewrite the file with one line and
  re-run the same command.
- **Note an unresolved Step 4 failure**, if the verify run was not a clean pass
  — a genuine hook failure, not a vacuous run or an autofix. Mention it here,
  and restate it **last**, in the block directly above the question. It may have
  scrolled well out of view, and approving *Commit + push* while a secret scan
  or a linter is still failing is a decision nobody would make knowingly.
- **Say what else this run touched.** Step 4 splits its `autofixed` list for
  you; relay whichever halves are non-empty:

  - `autofixed_ours` — "the hooks also reformatted `<file>`, one of this run's
    own files — that is in the diff you saw and it **will** be committed."
  - `autofixed_elsewhere` — "verifying the hooks also modified `<those files>`
    elsewhere in your tree. This run will not stage or commit them — they are
    yours to review and commit separately."

  Without this the user approves a commit believing their tree is as clean as
  the diff they were shown, and only finds out from the summary, after the fact.
- **Say the files are already written.** *Don't commit* leaves them on disk; it
  does not undo them. `status` returns a `discards` map — the exact command per
  file, derived from the state it reported. Relay those; do not compose them
  from the state yourself. (They differ in ways that are easy to get wrong:
  `git checkout --` on a *staged* file restores the work tree from the index,
  which discards nothing and leaves it staged.)
- **Name where a push would go**, from the tool rather than by re-deriving it:

  ```bash
  python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" push-plan
  ```

  Say its **`destination`** — name and URL, because a remote's nickname says
  nothing about where code goes. Quote `destination`, not `guidance`: before a
  commit exists a synced branch's guidance literally reads "nothing to push",
  which is the misleading prediction the next paragraph forbids, and putting it
  beside a live *Commit + push* option is exactly the confusion to avoid. If
  `suspicious_characters` is true, say that too. If `local_overrides` is
  non-empty, name those settings: this repository's own config sets something
  that makes git run a program or hand over credentials on a push, and the user
  should weigh that before approving one.

  **This plan describes the state before the commit exists**, and that matters
  for two of its answers only. `stop-up-to-date` and `stop-behind-only` stop
  being true the moment item 3 commits, so do not repeat them as a prediction —
  Step 5.4 recomputes after the commit and that is the one that decides.

  Every other `permits_push: false` answer is a stable fact about the
  repository that committing will not change: `stop-no-remote`,
  `stop-detached-head`, `stop-fetch-failed`, `stop-compare-failed`. **Say those
  plainly, before the question.** Offering *Commit + push* with no warning to
  someone whose repo has no remote gets them a commit and a failed push they
  were not warned about. The three options below stay the same either way.

**Last, adjacent to the question**, if and only if Step 4 ended in a genuine
failure, say it again on its own line and mark it so it survives a skim:

```text
STILL FAILING: <hook> — this commit would carry it.
```

Nothing goes between that line and the question. If the verify run passed, was
vacuous, only autofixed, **or reported `unchecked`**, this line does not appear
at all — a marker that shows up routinely stops being read.

`unchecked` is the third thing that makes `run_ok` false, and it is not a
failure: the run passed, but a hook this run just added never saw a file it
matches. Say that in its own words — "the run passed, but `<hook>` was never
exercised; nothing here has been checked by it" — and do not dress it as
STILL FAILING. Reporting a pass as a failure and a non-check as a pass are the
same mistake in opposite directions.

Then ask — exactly these, never an "also commit other changes"
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
- anything else — **do not push**, and do not pass the hash to Step 6. See
  [references/commit-failures.md](references/commit-failures.md).

A non-zero exit with no verdict means nothing was committed. That path is in the
same reference, and it still ends at Step 6: **never stop without a summary.**

### 4. Push

Only if the user chose a push option.

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" push-plan
```

- `permits_push: false` → report `guidance` and go to Step 6. Two of these are
  not problems: `stop-up-to-date` is a success, and `stop-behind-only` says the
  branch has nothing new to send. The rest each describe something the user must
  resolve before any push is possible — no remote configured, a detached HEAD, a
  fetch that failed, an ahead/behind count that could not be read. Say which it
  is rather than implying nothing is wrong.
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
