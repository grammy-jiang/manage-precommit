# When the commit does not go through

Loaded when `gitwork.py commit` returned something other than `verdict: "ok"`,
or exited non-zero. Both paths end at Step 6 — a run that stops without a
summary is the one case where the user gets no account of what is now in their
tree.

## A verdict other than `ok`

**Do not push.** The JSON carries what is needed:

- `remedy` — what the user can run. Relay it; **never run it yourself.**
  Discarding a commit that already exists is the user's call, not this skill's.
- `record_choice` and `record_note` — what Step 6 must record. Pass them through
  verbatim rather than composing your own wording.

Do not pass the hash to Step 6.

## A non-zero exit with no verdict

Nothing was committed, and the index is as you found it — **with one exception,
which the tool says out loud.** If stderr carries `AND the cleanup reset also
failed`, this run's files may still be staged. Relay that message verbatim and
tell the user to check `git status` before doing anything else.

Then go to Step 6 either way, with `--choice "not committed"` and
`--note "<the error>"`. This is the same shape a failed push produces, and the
push path already routes there; sending this one nowhere left the files written,
the facts half-recorded, and the `mktemp` `facts.json` and message file behind
with nothing saying to remove them.
