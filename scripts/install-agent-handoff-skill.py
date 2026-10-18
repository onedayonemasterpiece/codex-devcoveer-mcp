#!/usr/bin/env python3
"""Idempotently install host-wide agent-handoff skill and a managed instruction block."""
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills/devserver-agent-handoff/SKILL.md"
HOME_AGENTS = Path("/home/dev/AGENTS.md")
TARGET = Path("/home/dev/.local/share/devcoveer/auth/codex/skills/devserver-agent-handoff/SKILL.md")
START = "<!-- DEVCOVEER_AGENT_HANDOFF_START -->"
END = "<!-- DEVCOVEER_AGENT_HANDOFF_END -->"

BLOCK = """<!-- DEVCOVEER_AGENT_HANDOFF_START -->
## Agent handoff acknowledgment (mandatory)

- For tasks relayed to dot, another bot/agent, or a colleague, read
  `/home/dev/.local/share/devcoveer/auth/codex/skills/devserver-agent-handoff/SKILL.md`.
- Use the existing Slack DM/thread or existing task channel; no new MCP,
  relay service, chat or credential is required merely to hand off a task.
- Sending is **not** acknowledgment. Save the exact target and Slack
  `thread_ts`, read the same thread for a response from the actual recipient.
  If initially unanswered, perform a bounded later recheck; avoid hot polling,
  duplicates, and indefinite waits.
- Report separately: sent, recipient-acknowledged, in progress, and verified
  complete. If unacknowledged, say so. Do not promise background monitoring
  without an actual scheduled task, or infer authorization from an ack.
<!-- DEVCOVEER_AGENT_HANDOFF_END -->"""


def transformed(current: str) -> str:
    if START in current or END in current:
        if current.count(START) != 1 or current.count(END) != 1:
            raise ValueError("inconsistent managed handoff instruction markers")
        before, tail = current.split(START, 1)
        _, after = tail.split(END, 1)
        return before.rstrip() + "\n\n" + BLOCK + after
    return current.rstrip() + "\n\n" + BLOCK + "\n"


def atomic(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError("refuse to overwrite symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content if content.endswith("\n") else content + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--agents", type=Path, default=HOME_AGENTS)
    parser.add_argument("--target", type=Path, default=TARGET)
    args = parser.parse_args()
    if not SOURCE.is_file():
        raise SystemExit("agent handoff skill source missing")
    if args.agents.is_symlink() or args.target.is_symlink():
        raise SystemExit("unsafe target symlink")
    current = args.agents.read_text(encoding="utf-8") if args.agents.is_file() else ""
    required = transformed(current)
    skill = SOURCE.read_text(encoding="utf-8")
    installed = args.target.read_text(encoding="utf-8") if args.target.is_file() else None
    changed = required != current or installed != skill
    if args.apply and changed:
        atomic(args.target, skill)
        atomic(args.agents, required)
    print({"state": "installed" if args.apply else "dry_run",
           "changed": changed,
           "rule_present": START in (required if args.apply else current),
           "skill_present": args.target.is_file() if args.apply else installed == skill})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
