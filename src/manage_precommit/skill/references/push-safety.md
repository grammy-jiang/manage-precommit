# Force-push safety

Read this only when `push-plan` returned `action: "diverged"`. Every other
action is handled inline in Step 5.

Diverged means the branch and its upstream have each moved since they last
agreed. A plain push is refused by git. The only push that would land is a
force, and a force **deletes the remote commits that are not in your branch**.
That is irreversible for anyone who has not already fetched them.

## What the plan gives you

```text
behind        how many remote commits a force would DROP
ahead         how many local commits it would put there instead
would_drop    short hash, author and subject of each commit that would be lost
would_add     short hash, author and subject of each commit that would replace them
upstream_sha  the remote commit the comparison was made against
```

## What to show the user

All of it, in this order, before asking anything:

1. **The count, first and plainly**: "a force-push would drop N commit(s) from
   `<remote>/<branch>`, permanently."
2. **`would_drop`, every line, verbatim.** Never truncate or summarise this
   list. Each line carries the author as well as the subject, and the author is
   what makes "someone else's work is in it" a fact the operator can check
   rather than a warning they have to take on trust.
3. **`would_add`, every line, verbatim.** The same rule as `would_drop`, for
   the same reason: this is what the force actually puts on the remote, and the
   author on each line is how the operator notices a commit that should not be
   there — an unexpected one from a bad rebase, or someone else's work about to
   be attributed to this push. Never summarise it either.
4. **The destination and its URL**, from `guidance`. A remote's nickname says
   nothing about where the code goes.
5. If `suspicious_characters` is true, say that too: those subject lines contain
   characters that can misrepresent themselves, so what the terminal shows may
   not be what is recorded.
6. **That it cannot be undone** by this skill or by them, once pushed.

Then AskUserQuestion — exactly two options: **Force-push** / **Skip push**.

There is no third option, and no default. If AskUserQuestion is unavailable,
stop and say a force-push decision is needed.

## Only on "Force-push"

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" push \
  --confirm-force --expect-remote "<upstream_sha from the plan you just showed>" \
  --facts "<facts.json>"
```

`--expect-remote` is not optional and is not a formality. A bare
`--force-with-lease` leases against the remote-tracking ref, which `push-plan`
itself just refreshed with `git fetch` — so it would happily authorise dropping
commits that appeared *after* the user saw the list, which is exactly what the
lease exists to prevent. Passing the sha whose consequences were shown makes the
lease mean what the user agreed to.

Pass the `upstream_sha` **from the plan the user actually saw**. If you re-run
`push-plan` before pushing, you must re-show and re-ask with the new plan.

## If it refuses

- `error: "remote-moved"` (exit 4) — the remote changed between the approval and
  the push. The commits a force would drop are no longer the ones the user
  agreed to drop. Re-run `push-plan`, show the new list, and ask again. Do not
  retry with the old sha.
- `error: "missing-expect-remote"` (exit 6) — `--confirm-force` was passed
  without the sha. Supply it from the approved plan.
- exit 4 with no `error` — `--confirm-force` was not passed at all, which is the
  refusal working. Ask first.

## On "Skip push"

Nothing is pushed and nothing is lost — the commit stays local and the remote
keeps the commits a force would have dropped. Say that plainly, then go to
Step 6 and record why, so the summary does not simply read `commit only` with
no explanation:

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" facts --facts "<facts.json>" \
  --note "push declined: a force-push would have dropped <N> remote commit(s)"
```

Do not pass `--choice`; with a commit recorded and no push, the tool already
derives `commit only`. The `--note` is what says it was a decision rather than
a failure.

## Never

- Never pass `--confirm-force` on your own initiative, or because a previous
  push failed.
- Never re-run with a freshly fetched sha to get past `remote-moved`. That
  defeats the entire check.
- Never suggest `git push --force`, `-f`, or editing the refspec by hand.
