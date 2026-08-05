# A worked run

What Step 6 prints, and what produced each row. Colour is added on a terminal
and dropped when piped; the alignment is identical either way.

```text
manage-precommit - run summary
==============================

NOTES
  * history was reset before this run

SCAN
  repo         git repository
  .pre-commit  existing -- 2 repos
  detected     markdown (README.md), mermaid fence (docs/arch.md)

HOOKS
  added        https://github.com/gitleaks/gitleaks: added (rev v8.30.1), local: added (mermaid-lint)
  left as-is   https://github.com/pre-commit/pre-commit-hooks: already present (rev v6.0.0),
               https://github.com/adrienverge/yamllint: already present (rev v1.38.0)
  recommended  markdownlint  <- README.md
               gitleaks  <- any repo -- secret scan
  versions     gitleaks=v8.30.1, mermaid=11.16.0

FILES
  written  .pre-commit-config.yaml, scripts/lint-mermaid.mjs
  kept     .yamllint.yaml

VERIFY
  install    git hook installed
  run        passed on the second run; hooks autofixed 3 file(s)
  autofixed  README.md, a.py, b.py

COMMIT
  choice  commit + push
  commit  abc1234  chore: add pre-commit hooks
  scope   2 pre-commit setup files only  (1 other file untouched)
  push    abc1234 -> origin/main

NET
  repos  2 -> 4  +gitleaks +mermaid
  diff   2 files changed, 39 insertions(+)
```

## Where each row comes from

| Row | Written by |
| --- | --- |
| `NOTES` | `gitwork.py facts --note` — the only prose field in the file |
| `SCAN detected` | `precommit.py --recommend`, from markers it actually saw |
| `HOOKS added` / `left as-is` | the merge report, per **catalog** entry |
| `HOOKS recommended` | `--recommend`; the `<-` names the file that triggered it |
| `HOOKS versions` | fetched live at merge time (`git ls-remote`, `npm view`) |
| `FILES` | what the write step created versus what it found already there |
| `VERIFY` | `precommit.py --verify` — `run_ok`, `vacuous` and `autofixed` are its verdict |
| `COMMIT choice` | derived by `gitwork.py facts` from the recorded hash and push |
| `COMMIT commit` / `scope` / `push` | `gitwork.py commit` and `push`, from verified state |
| `NET diff` | the diffstat of the commit that exists |

Nothing in that table is assembled by the agent. If a number is wrong, the fix
is in the script that computed it, not in the wording here.

**`HOOKS` reports on the catalog only.** Both rows iterate the five catalog
entries, so a hook the user already had that this skill does not manage never
appears in either. That is not an omission -- it is preserved untouched, and
saying nothing about it is the honest report. The place to see it is the diff
in Step 5.

## The two rows worth reading twice

**`VERIFY run`** shows as a warning, not green, when `vacuous` is true:

```text
VERIFY
  install  git hook installed
  run      vacuous pass -- every hook reported (no files to check). --all-files
           covers only tracked files, so nothing was actually checked; re-run
           naming the paths explicitly with --files.
```

That is the state a first run lands in when the setup files are still untracked.
`pre-commit` exits 0, and nothing was checked.

**`COMMIT scope`** names what was deliberately left alone:

```text
  scope   2 pre-commit setup files only  (1 other file untouched)
```

The hooks' autofixes routinely touch files elsewhere in the tree. Those are the
user's to review and commit; this skill never does.
