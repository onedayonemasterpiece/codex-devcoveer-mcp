# Codex Native deep links

DevCoveer supports **existing** Codex Native threads that were not launched via
its own `start_task` and therefore have no DevCoveer task-registry entry.

## Reading

Use `read_task(task="codex://threads/<UUID>", detail="full")` or the dedicated
`read_native_thread(thread="codex://threads/<UUID>", detail="full")`.
The latter also accepts a bare UUID.

The read is passive: no model invocation, no `thread/resume`, no file or session
mutation. It reads the verified rollout from fixed local Codex session profiles
(native DevCoveer profile first, then the standard Codex and bridge profiles).
Paths are never accepted from the caller. If `project` is supplied, the thread
must belong to that exact resolved physical project.

Native responses use a bounded, reproducible cursor:

- `detail=summary`: counts plus the latest visible messages.
- `detail=full`: `history` contains an ordered, secret-redacted slice.
  Start at `cursor=0`; continue with `nextCursor` until it is `null`.
  `limit` is 1–100 and defaults to 40; response bytes are separately bounded.
- `include_tools=false`: conversation messages only; this is faster for a
  long session. By default full detail includes tool calls and tool results.
- `index`, `part` and `parts` allow every visible message or tool output
  to be reconstructed, including oversized items, without silent truncation.
- `snapshotStable` indicates whether the source changed during the read.
  `complete` means both a stable snapshot and the final page.
  An ongoing thread may acquire additional appended history between pages.
- Developer/system messages, private reasoning, world-state internals and
  sensitive metadata are excluded. Secret-bearing content is redacted.

For bridge-owned threads not found in the filesystem, `read_task` keeps its
existing native app-server lookup. Existing DevCoveer task IDs, names, prefixes
and OpenCode task semantics are unchanged.

## Mutations

The URL form is normalized for `continue_task` and `cancel_task`.
For an externally owned native thread, their existing trusted bridge-metadata
guard **still applies**: possession of a URL does not permit altering or
interrupting a foreign native session. External threads can always be read
through the new reader. No access-control bypass or automatic native resume
was introduced.

## Verification

```text
pytest -q tests/test_native_thread_links.py tests/test_clean_host_canonical_runtime.py
python scripts/native-thread-link-acceptance.py
```

The live acceptance script uses the owner-provided regression UUID; it emits
only counts and timing, never the transcript. After updating `origin/main`,
run the registered `canonical_install` profile with deferred restart, restart
only the owned MCP tunnel, then verify `read_task` through the public connector.


## Stable gateway and consistent audit snapshots

Long-lived clients with an older top-level tool schema can use
`direct_ops_v2(project="codex-devcoveer-mcp", plane="host_probe",
operation="native_thread_read", payload={"thread":"codex://threads/<UUID>"})`.
Discover the current payload with `operation="describe"` and
`payload={"operation":"native_thread_read"}`. This reuses the same passive
reader and owner authentication. The gateway project is the operator context;
it does not assert that the native thread belongs to that project. No paths,
resume, model invocation, or session writes are accepted.

The gateway defaults to conversation-only history (`include_tools=false`).
Use `include_tools=true` explicitly when audit tool evidence is needed.
Only public `final`/`commentary` channels and legacy absent-channel events
are included; private-channel tool results are suppressed with their calls.

Every page returns a `snapshot` token identifying a fixed source-byte prefix,
its SHA-256, thread and tool-filter mode. Send the token unchanged with each
`nextCursor`. Appends do not alter this snapshot; replaced, changed or shortened
prefixes fail with `snapshot_mismatch`. A new snapshot starts at cursor zero.
A token is a consistency check, not an authentication credential.

`complete=true` means the last page of that pinned, stable, parseable snapshot.
Malformed/truncated JSON records are counted and prevent a completeness claim;
restart at zero without a token after the writer finishes the record. There is
no guarantee that an active session has stopped appending after the snapshot.

Reconstruct ordered `index`/`part`/`parts` events client-side. A Markdown export
can be assembled from these safe pages and delivered as an attachment without
publishing private history or relying on a remote local-file path.
