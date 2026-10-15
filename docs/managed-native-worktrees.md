# Parallel Git worktrees in Codex DevCoveer

Use a **managed worktree only when concurrent changes genuinely need isolation**.
Normal sequential work continues in the selected checkout. This replaces the
previous unconditional "do not create a worktree" instruction for native Codex
write tasks. Read-only tasks cannot create or integrate managed worktrees.

The existing `direct_ops_v2` gateway provides a typed action without a new
connector schema: project `street-story` (or the canonical repository project),
`plane=project_action`, `operation=managed_worktree`.

## Short path for the agent

1. **Create once** with `payload={"mode":"create","task_ref":"<exact native thread UUID>"}`.
   The response supplies an isolated worktree path and branch under
   `/home/dev/.codex/worktrees/devcoveer-managed/`; the original checkout and
   its uncommitted work remain untouched. Identical task ID reuses its worktree.
2. Work and test **inside that worktree**; do not edit another agent's checkout.
   Commit the intended, verified changes on the temporary branch.
3. **Integrate and retire** with `mode=finish`, the same task_ref and
   `expected_head=<exact feature SHA>`. The tool verifies the repository,
   branch and commit, a clean isolated checkout and target, and ancestry.
   It performs a fast-forward when possible or a standard conflict-free merge
   after concurrent target updates. It then removes only its own clean
   worktree and safely deletes the merged temporary branch.
4. If integration conflicts, the target is dirty, there are uncommitted files
   or a process is still using the worktree, the tool **does not force-delete**.
   It aborts any conflicted merge, leaves the worktree and commits intact,
   and reports the exact blocker. Reuse the same task and resolve the blocker;
   call `mode=status` for a readback. Do not start another worktree just to
   evade it. A successful answer must not call this work "merged/cleaned"
   while it remains blocked.

Do not use unmanaged clones, arbitrary worktree paths, blanket
`git worktree prune`, `git clean -fdx`, or `git worktree remove --force`.
Old unrelated worktrees and active agents are outside this lifecycle.
When the original checkout is actively modified by another agent, worktree
integration waits until that checkout is clean rather than overwriting it.

The lifecycle is scoped to an exact task UUID or dvt task ID, with an on-disk
receipt so interrupted tasks can resume idempotently. No new daemon, queue,
scheduled GC or mandatory per-task approval is introduced.
