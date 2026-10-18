---
name: devserver-agent-handoff
description: "Reliable handoffs of tasks, status, and results to another assistant, dot, human operator, or code agent over Slack or existing tools. Use when relaying a task and reporting delivery, acceptance, progress, or completion."
---

# Agent-to-agent handoff: delivery is not acceptance

Use the **already working destination** (Slack DM/thread, project thread, or connected task interface). Never add an MCP method, extra service, intermediary bot, or new public channel just to send an ordinary handoff.

1. **Resolve the recipient.** Verify the intended identity from a trusted current recipient/profile or existing project thread. For Slack, prefer an existing DM or the specified thread. Do not confuse a bot mention, a Slack channel, and the sender's own account.
2. **Deliver once.** Include the concrete action, source/result links, scope and authorizations, verification/definition of done, where to reply, and sensitive-data boundaries. Capture the returned `channel_id`, `message_ts` and permalink. If the send times out, reconcile that exact destination/thread before any resend.
3. **Check for acknowledgment before declaring the handoff finished.** Read the **exact parent thread** using its `message_ts`. If no response has appeared, perform at least one further bounded check with a short delay (roughly 15–30 seconds, when feasible within the active synchronous turn). Prefer two or three reads within about 60–90 seconds total, not hot polling or indefinite waiting. Do not merely read the DM channel's top-level history; responses may be threaded and hidden there.
4. **Validate the reply.** It must be authored by the intended recipient, posted after the handoff, and in the original thread/task. Ignore sender echoes, unrelated older replies, API delivery receipts and generic bot messages. A response can reject the task, require permission or identify a blocker; treat these distinctly.
5. **Classify the observed state:**
   - `sent_unconfirmed`: provider accepted the message, no verified recipient reply yet.
   - `acknowledged`: recipient explicitly accepted or refused; quote the actual decision.
   - `in_progress`: recipient reports substantive current findings.
   - `completed_unverified`: recipient claims completion without externally checked deliverables.
   - `verified_complete`: result artifacts and the relevant acceptance checks were actually verified.
6. **Report facts to the owner:** exact state, recipient, permalink, timestamp of the latest relevant answer, key findings/blockers, and next action. Never conflate "sent", "accepted" and "completed". If there is no reply within the bounded window, say "sent, not yet acknowledged" and offer an explicit follow-up/watch if useful. Do not claim you will continue checking after this response without a real scheduled automation.
7. **Preserve privacy and approvals.** No secrets or private document bytes in a Slack status. An acknowledgment does not grant new permissions or bypass release/publishing gates. Keep task/approval context and existing project conventions.

For continuing discussions, return to the **same** thread, not a new ping. Avoid repeated sends after any uncertain outcome. If a recipient replies before the first check, read that response; there is no reason to sleep merely to satisfy a fixed delay.

## Example acceptance

An owner asks to give dot the RKB archive fix and have it audit previously imported books. After sending via Slack, the sender reads the saved message's thread, sees dot answer "Принял", verifies that the reply belongs to dot, then reads any follow-up findings. The owner receives "dot accepted, investigation underway" with the reply permalink, not just "message sent" and not "audit completed".
