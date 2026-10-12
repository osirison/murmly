---
title: Merging stacked pull requests when the repo squash-merges
description: Retarget each child to main before merging its parent, then move it with rebase --onto, or the rebase conflicts on commits that now exist on main as one squash commit
trigger: gh pr merge, gh pr edit --base, stacked, rebase --onto
depends_on: repository merge settings (squash, delete_branch_on_merge)
recorded: 2026-10-12
---

# Stacked PRs: retarget, then merge, then `rebase --onto`

**Setting:** the repo squash-merges and deletes the branch on merge
(`delete_branch_on_merge`). The squash message is the PR's commit messages.

**Symptom:** after merging a parent PR, a plain `git rebase origin/main` on the
child replays the parent's commits. They now exist on main as one squash commit
with different hashes, so the rebase conflicts. Deleting the parent branch can
also close a child that still targets it.

**Fix, in this order:**

1. Record the parent's tip: `git rev-parse <parent-branch>`.
2. Retarget the child: `gh pr edit <child> --base main`. Do this before merging
   the parent.
3. Merge the parent.
4. `git fetch origin`, then `git rebase --onto origin/main <old parent tip>` on
   the child, and `git push --force-with-lease`.
