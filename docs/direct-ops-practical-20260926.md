# Practical Direct Ops follow-up — 2026-09-26

Status: implementation candidate. ChatGPT authored the changes and 21 focused
regressions. Source handoff application and Git whitespace validation succeeded.
The regression suite, clean-host build, installed-runtime behavior and external
MCP acceptance have NOT been run for this candidate. Native Codex will perform
review, tests, minimal final fixes and rollout. Do not treat this document as an
acceptance receipt.

## Scope

Improve the existing direct execution path; do not create another executor,
environment manager, deployment framework, telemetry service or database.
No production service or main checkout was changed while preparing this branch.

- `run_tool` / `job_start`: select existing project Python environments and permit
  a small documented set of nonsecret environment flags.
- `job_start` / `job_status` / `job_cancel` / `job_cleanup`: retain useful output,
  avoid duplicate keyed launches, expose progress and recover missing results.
- `job_list`: discover existing jobs in the same project/worktree.
- `git_fetch`: fetch one branch without touching checkout/index/untracked files.
- `github_workflow_dispatch` / `github_run_cancel`: routine Actions control without
  an LLM executor. Existing status/log/PR operations remain in place.
- `direct_ops_v2` operation `describe`: discover current operation names and exact
  payload schemas. Manifest/capabilities now include full direct-schema digests.
- Tool-description guidance: long deterministic work uses direct jobs, not an
  agent solely to execute a known command.

Generic deployment parameterization and reusable dependency provisioning are
intentionally outside this patch. Do not turn final debugging into their design.

## Discover and call

The `direct_ops_v2` top-level envelope is unchanged:

```json
{"project":"codex-devcoveer-mcp","plane":"project_action","operation":"describe","payload":{}}
```

Ask for one operation's actual parameters:

```json
{"project":"codex-devcoveer-mcp","plane":"project_action","operation":"describe","payload":{"operation":"job_start"}}
```

`describe` is read-only on all four planes. Its payload schema excludes the
reserved `kind` / `id` fields; do not put those fields in gateway payloads.
`schema_digest` fingerprints the full current plane schema, not Python symbol
names. `mcp_manifest.direct_schema_digests` is explicitly limited to the four
direct planes; the old catalogue-name digest remains for compatibility.

When a frozen client cannot call the top-level gateway, the existing carrier is:

```json
{"project":"codex-devcoveer-mcp","action":{"kind":"run_tool","tool":"python_script","path":"@direct_ops_v2","args":["project_action","describe","{\"operation\":\"job_start\"}"]}}
```

This reserved path invokes the same typed gateway, not a Python file. Its old
argument size limits still apply. Updating server code alone cannot repair an
externally published or cached connector catalogue. Compare actual server
`tools/list`, its descriptions and this gateway with what a fresh client exposes.
Do not repeatedly restart healthy production to solve a client-side catalogue gap.

## Python execution and jobs

`python_env` accepts `auto`, `system`, or an existing project-relative venv
directory. Auto checks `cwd/.venv`, then project-root `.venv`, then system Python.
An existing invalid venv is an environment error, not silent fallback. Standard
venv interpreter symlinks are supported; symlinked venv directories and traversal
remain rejected. No packages or extra venvs are created automatically.

Permitted `env` keys are `CI`, `NODE_ENV`, `PYTHONPATH`,
`PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `TZ`, `LANG`, and `LC_ALL`.
Unbuffered output and disabled bytecode writing are defaults. Execution receipts
include the selected executable and working directory. Python-dependent shell
scripts can explicitly select `python_env` to place its bin directory on PATH.

A typical long run:

```json
{"project":"example-project","plane":"project_action","operation":"job_start","payload":{"tool":"pytest","cwd":"backend","args":["-vv","-x"],"python_env":"auto","timeout_seconds":900,"request_key":"owner-chosen-test-attempt-1"}}
```

Then call `job_status` with the returned `job_id` and optional `tail_lines`
(0–200; default 40). It returns bounded stdout/stderr tails and `last_output_at`.
Logs are private, redacted before tail truncation, and rotated in place at about
1 MiB per stream. They are diagnostic tails, not an unlimited complete log
archive. Newline-free progress may appear only at line completion or process exit;
a quiet log alone does not prove that a process is stuck.

The execution runner keeps the end of output; existing probe prefix semantics
remain unchanged. Timeout bounds include pipe EOF after parent exit, and a
`ProbeError` carries partial output. The durable job runner persists that partial
output with `timed_out`; synchronous callers still must handle timeout errors.

`request_key` is optional. Reusing the same key in the same resolved worktree
reuses the job; changing its command is a conflict. A lost start response must not
trigger an automatic second launch. To deliberately rerun tests, choose a new
key. Cleanup of keyed jobs removes logs but retains a small receipt to preserve
this behavior; unkeyed cleanup removes the job directory as before.

`job_list` returns metadata with a cursor, without polling every systemd unit.
Completed results survive MCP restart. An exited/missing unit without a result
becomes `interrupted`, not permanent `pending`. A failed status transport remains
`unknown` and retryable. This is not automatic job resumption after host reboot.
Cancellation targets only that job's systemd control group and preserves a result
that completed concurrently.

Source receipts record HEAD, dirty state and a bounded tracked-diff fingerprint.
They do not freeze a worktree. With untracked/truncated/unavailable evidence,
`complete=false` and `source_changed` can be null: never claim exact immutable
source coverage from that receipt. Use the existing isolated handoff when needed.
A successful process is not automatically product acceptance.

## Git and Actions

`git_fetch` accepts a remote alias (default `origin`) and one branch. It validates
the real GitHub remote and fetches objects/tracking ref without checkout, reset,
prune or force. Dirty files need not be discarded.

`github_workflow_dispatch` accepts workflow ID/filename, branch/tag ref, and
optional scalar inputs. It uses GitHub's versioned dispatch response
(`X-GitHub-Api-Version: 2026-03-10`) and returns `workflow_run_id` as `run_id` when
provided by GitHub. Empty/legacy receipts are explicitly incomplete; no guessed
latest run and no automatic redispatch. A submitted workflow is not a passing CI
run. String inputs use raw fields, including values starting with `@`.

`github_run_cancel` requires one `run_id` and its expected head SHA. Completed runs
are a no-op; accepted cancellation is `cancelling`, not claimed completion. Read
back through existing GitHub status operations. Never test against unrelated runs.

## Native Codex review, tests and rollout

1. Fresh-read the PR head and preserve the dirty canonical checkout and installed
   recovery overlay. Use an isolated worktree for testing. No reset/clean/stash of
   unrelated work, no OpenCode, no ADB or Android runtime on DevCoveer.
2. Review the actual diff and run `tests/test_direct_ops_practical.py`, the existing
   direct jobs/actions/probes/gateway/host/reminder regressions, then the complete
   checked-in verifier (`bash scripts/verify-source.sh`). Reuse its declared test
   environment. Fix real defects in this same narrow scope; do not merely report
   a failing test or relax tests to hide one.
3. Exercise the candidate against real user-systemd with disposable fixtures:
   success, nonzero exit, timeout with output, child-held pipes, cancellation,
   concurrent identical keys, lost response/replay, long logs, missing unit,
   environment selection, result/list/cleanup. Check legacy operations and source
   handoff still work. Preserve honest redaction/truncation/completeness metadata.
4. Verify the clean-host build enumerates the stable gateway, updated concrete
   schemas, source-derived descriptions and all previous tools. Exercise describe
   and the frozen-client carrier; ensure new variants survive rebuild.
5. Validate GitHub dispatch/cancel with a dedicated harmless workflow only when
   available. Never launch a production deployment for smoke and never cancel
   unrelated runs. Otherwise report mocked contract validation separately from
   live provider acceptance.
6. After successful source verification, merge the exact verified candidate into
   main, read the resulting SHA, and use the canonical installer/build/readback
   process with an actual rollback copy. Reconcile independent installed recovery
   changes explicitly rather than silently losing them. Restart only the needed
   MCP/tunnel component, after the installer RPC has returned.
7. Verify installed operations over real MCP; check job results across the MCP
   restart, old probes and no unrelated service disruption. Record exact tested,
   merged and installed SHAs and any remaining client-catalogue issue in the PR.

Do not claim ChatGPT-side acceptance from local tools/list alone. If the published
connector still exposes the old catalogue, state the precise refresh/republication
step and leave production alone. A fresh chat is for final client acceptance after
that refresh, not a substitute for completing the server-side rollout.
