# Recovering a verify run that did not really check anything

Loaded when `--verify` came back with `run_ok: false` for a reason that is *not*
a hook failing. Two of the three outcomes here look like success, which is why
the tool judges them rather than leaving it to a reading of the output.

`rerun_files` in the verify result is the list a re-run needs, already worked
out. Write it to a `mktemp` file with the Write tool, one path per line, and
pass `--files-file`:

```bash
python3 "<skill-dir>/scripts/precommit.py" --dir "<repo>" --verify \
  --facts "<facts.json>" --files-file "<paths.txt>"
```

A file, not the command line: a repository can name a file anything, backticks
and semicolons included — the same reason catalog keys, remote names and commit
messages go through files. Delete it once the command returns, and say which
form produced the result you report.

## `vacuous: true`

Every hook reported `(no files to check)`. `--all-files` covers only git-*tracked*
files, so in a repo where the setup files are still untracked the run passes
having checked nothing. Re-running by name works on untracked files without
touching the index.

`rerun_files` is `files.written` plus every entry of `scan.detected_paths` — the
files that caused each hook to be recommended are the ones that exercise it. If
you ever build this list yourself, use `detected_paths` and never `detected`:
`detected` is prose for a human (`markdown (README.md)`), and passing those
strings makes pre-commit look for files that do not exist, so the check silently
proves nothing.

## `unchecked` non-empty

The run was green overall, but a hook *this run added* reported it had no files
to check. `--all-files` covering the whole repo is not enough on its own:
hygiene's hooks match anything and turn the run green while `markdownlint` or
`mermaid`, added because a `.md` was detected, sat idle.

Re-run as above and confirm `unchecked` comes back empty. It is not a pass until
it does.

## `autofixed` non-empty

The autofixing hooks (trailing-whitespace, end-of-file-fixer, mixed-line-ending)
rewrote files and exited non-zero on the first run; the tool re-ran once, and a
clean second pass is the success. Those edits **may touch files anywhere in the
repo**. That is expected. Step 4 splits them into `autofixed_ours` and
`autofixed_elsewhere` for the disclosure Step 5 requires.

## A genuine failure

gitleaks finding a secret, or a linter erroring, is real — report it; the user
fixes the content or adjusts the config. **Never weaken a hook to make the run
pass.**
