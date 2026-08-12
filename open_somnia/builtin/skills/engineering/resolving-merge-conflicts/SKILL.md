---
name: resolving-merge-conflicts
description: Resolve an in-progress git merge or rebase conflict hunk by hunk - by intent traced to each side's primary source, never by guessing or --abort. Use when mid-merge/rebase with conflicts, or on 'resolve these conflicts', 'help with the merge'.
---

# Resolving Merge Conflicts

Work an in-progress `git merge` or `git rebase` conflict **hunk by hunk**,
resolving by **intent** traced to each side's primary source — not by picking
lines or guessing. Use the shell tool (`bash`) for all git operations. **Never
`--abort`**: a conflict you abandon is a conflict you'll hit again.

## Process

1. **Understand both sides.** For each side of the merge, find the primary
   source of the change — the commit, PR, decision, spec, task, or ADR behind it
   (`git log`, `gh pr view`, the task store, the ADR dir). You are resolving
   *why each side changed this spot*, not which lines look nicer.

2. **Hunk by hunk.** List the conflicted files (`git status`), then resolve one
   conflict hunk at a time. For each hunk: read both sides, state each side's
   intent in one sentence, then write the merged result that preserves both
   intents. If the intents genuinely conflict (not just the text), that's a real
   decision — see "When to stop and ask".

3. **Verify as you go.** After resolving a file, run the relevant check (its
   tests, a typecheck, a build) before moving on. Don't accumulate half-resolved
   files and discover breakage only at the end.

4. **Finish the operation.** Once every hunk is resolved and the suite is green,
   complete the merge or continue the rebase (`git merge --continue` /
   `git rebase --continue`). If a test fails because of how you merged, fix the
   merge — don't back out.

## When to stop and ask

If a hunk's two sides reflect **incompatible product decisions** (not a textual
collision), stop and put the decision to the user with both intents and your
recommendation. Merging code is mechanical; choosing between intents is not.
