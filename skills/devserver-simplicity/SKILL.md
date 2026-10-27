---
name: devserver-simplicity
description: "Prevent over-complication in every code change on the devserver (RKB, VibePublish, Street Story, Wonderful Lections, DevCoveer and any other project). Trigger before planning or writing any fix, feature, refactor or PR, when writing a task prompt for an executor, and when reviewing a diff. Every added state, flag, mode, retry, check, gate, cache, queue, limit or recovery path must be justified by product value and a concrete failure that no existing mechanism already handles."
---

# Devserver: no complexity without product value

Distilled from repeated review returns on these projects. Most rejected PRs were not wrong; they were *bigger than the problem*. Read this before you plan, again before you open the PR.

## 1. Before writing code: three answers

Write these three lines into your plan (and later the PR description). If any is missing, stop and ask the task owner.

1. **Who notices, and how often.** A person and an action ("a reader of a book page waits 25 s on every page"). "More robust", "safer", "future-proof" are not answers.
2. **The concrete failure or decision.** An observed defect (log, failing test, measured time, wrong answer) or an explicit owner decision. A theoretical scenario does not count.
3. **The root, not the symptom.** Name the rule or component that produces the failure. If a fix adds information, a hint, a retry or a special case *around* the failure, you are usually treating a symptom. Example: a gate rejects pages that an instruction told the model to flag; the fix is to make the instruction and the gate agree, not to add +200 lines of hints explaining the rejection.

## 2. Trust what already works

Before adding a mechanism, find the existing one (file:line, or the platform documentation) and use it:

- **Stable platforms keep their own guarantees.** Database transactions and unique constraints, S3 conditional writes, systemd restarts, HTTP/MCP/Slack/Telegram SDK delivery and retries, OS file atomicity. Do not re-verify their outcome, re-implement their retry, or add a second integrity hash on top.
- **One authoritative source per fact.** Do not prove one fact several ways (status + log + re-read + hash). Read the single source of truth.
- **Reuse a working peer.** If another project on this host already solved it (a viewer, an upload path, a local service connection), copy its approach. Do not invent a new transport, gateway, queue or controller.
- **Do not validate your own just-produced output** when no untrusted boundary lies between producer and consumer.
- **Depend on internals explicitly or not at all.** If you rely on a library's internals, declare the dependency and its range; better, use its public API.

## 3. Forbidden by default

Each needs, in the PR description, the failure it catches *and* why no existing mechanism from §2 catches it. Without both, delete it.

- new state fields, phases, modes, flags, feature toggles, persisted contracts or protocol messages;
- retries, polling loops, timers, watchdogs, reconciliations, "repair"/"recovery"/"self-heal" paths, fallback branches;
- extra checks, gates, readiness conditions, SHA/fingerprint/version pins used as gates;
- hard-coded thresholds equal to today's count or chosen without measurement;
- special cases ("allow X only when Y") instead of fixing the general rule;
- a second implementation of something that already exists (a second answer path, a second receiver, a second capacity limiter);
- statuses with an unknown or "needs a human" outcome, and manual checks or approvals inside the normal flow (see §4).

**Repair paths are never the product.** If data can get into a bad state, fix the normal process that produced it and remove the bad state. Do not add a path that repairs it afterwards. Hiding a path (removing it from a schema while instructions or dead code still point to it) is not removal: delete the code, the instructions and the tests that describe it.

## 4. Liveness: the system must keep moving

- **Every scenario ends in a terminal outcome without a person.** Do not add statuses such as `unknown`, `needs_review`, `pending_manual`, `requires_operator`, `awaiting_confirmation` that stop work until someone looks. Do not design "an operator/owner will check it" steps, manual approval gates or "report and wait" branches into a normal path. Resolve the outcome in code (read the ledger, retry the same request, reject and move on) or keep the uncertainty *inside the data* (e.g. an unreadable word transcribed as `[?]`) so the flow continues.
- An error before commit means "not applied": the same request can be repeated. "Unknown outcome" exists only when the connection was lost at commit, and it is resolved by reading the ledger by key, never by waiting for a human.
- One failing item must not block unrelated items (other users, books, queues).
- A deterministic failure must not loop: after the bounded retry it is rejected and the queue moves on.
- Timers and leases extend on the fact of a request, not on its success.

## 5. Scope and size

- **Do exactly the task.** Do not change product behaviour outside it, even by one line of an instruction or tool description: a changed answer language, an added "you may request more results" hint, a widened limit. Those are product decisions; put them in a separate PR with the owner's decision.
- **Prefer subtraction.** When you touch an area, delete the dead branch, obsolete instruction and old test it leaves behind. A good fix often has a negative line count.
- **Respect the line budget** in the task. If the correct fix does not fit, report the exact deficit and why. Do not silently exceed the budget, and do not shrink readability or provenance checks to meet it.
- **Measure before optimising.** A performance change needs before/after numbers (query plan, timing, size) from the real path; a passing CI on a tiny fixture is not evidence of speed.
- **One distinguishing test.** It fails before the fix and passes after it. Do not add test suites for paths you just deleted.

## 6. Self-check before opening the PR

Add this table to the PR description, one row per added element (state, branch, check, retry, limit, message, dependency):

| added element | concrete failure it handles (observed) | existing mechanism checked (file:line) and why it is not enough | kept / deleted |

Then answer in the PR description:
- Net production lines (+/−), and the budget.
- What I deliberately did **not** add.
- What I deleted.
- How the owner or a user will see the result live (plan, merge and green CI do not mean deployed).

If a row has an empty "observed" column or names an existing mechanism that already handles it, delete that element before asking for review.

## 7. For agents who write executor prompts

Every development prompt to an executor (ChatGPT window, Codex task, other agent) should include:

- a link to this skill: `https://github.com/onedayonemasterpiece/codex-devcoveer-mcp/blob/main/skills/devserver-simplicity/SKILL.md`;
- the person-level goal (§1), the evidence (file:line, log), the line budget, the one distinguishing test, and what is explicitly forbidden for this task;
- the instruction to report the §6 table in the PR.

Executors tend to add states, flags, retries, gates and recovery modes "for safety". Name them as forbidden in the prompt.
