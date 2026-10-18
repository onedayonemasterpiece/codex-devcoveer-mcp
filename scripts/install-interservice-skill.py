#!/usr/bin/env python3
"""Install the versioned intra-service skill and a managed global AGENTS.md rule."""
from __future__ import annotations
import argparse
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills/devserver-interservice/SKILL.md"
AGENTS = Path("/home/dev/AGENTS.md")
TARGET = Path("/home/dev/.local/share/devcoveer/auth/codex/skills/devserver-interservice/SKILL.md")
START = "<!-- DEVCOVEER_INTERSERVICE_POLICY_START -->"
END = "<!-- DEVCOVEER_INTERSERVICE_POLICY_END -->"
BLOCK = """<!-- DEVCOVEER_INTERSERVICE_POLICY_START -->
## Reliable same-host interservice integration (mandatory)

- For every intra-server integration use the `devserver-interservice` skill at
  `/home/dev/.local/share/devcoveer/auth/codex/skills/devserver-interservice/SKILL.md`.
  Canonical policy: `/home/dev/projects/codex-devcoveer-mcp/docs/operations/local-service-connections.md`.
- Same host: receiver's loopback HTTP or existing Unix socket, dedicated least-privileged
  **service** identity, explicit target/method/topic scope. No public HTTPS hop, browser
  OAuth, owner token reuse, proxy/tunnel or newly invented central gateway.
- Persist idempotency keys and receipts before effects, recover lost/unknown outcomes
  without duplicates, keep original data until verified provider receipt and SHA-256.
  Degraded downstream leaves durable pending state and does not block canonical ingest.
- External hosts use authenticated TLS; human-facing remote MCP retains user OAuth.
  Preserve independent service restarts, privacy and per-resource ACL.
- A plan, changed files or green unit tests do NOT mean a product is deployed.
  Report deployment, real downstream E2E and remaining blockers explicitly.
<!-- DEVCOVEER_INTERSERVICE_POLICY_END -->"""


def transformed(current: str) -> str:
    if START in current or END in current:
        if current.count(START) != 1 or current.count(END) != 1:
            raise ValueError("inconsistent managed interservice block markers")
        before, tail = current.split(START, 1)
        _, after = tail.split(END, 1)
        return before.rstrip() + "\n\n" + BLOCK + after
    return current.rstrip() + "\n\n" + BLOCK + "\n"


def atomic(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError("refuse to overwrite symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content if content.endswith("\n") else content + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--agents", type=Path, default=AGENTS, help="for isolated tests")
    p.add_argument("--target", type=Path, default=TARGET, help="for isolated tests")
    args = p.parse_args()
    if not SOURCE.is_file():
        raise SystemExit("missing canonical skill")
    if args.agents.is_symlink() or args.target.is_symlink():
        raise SystemExit("unsafe target symlink")
    old = args.agents.read_text(encoding="utf-8") if args.agents.exists() else ""
    desired = transformed(old)
    skill = SOURCE.read_text(encoding="utf-8")
    current_skill = args.target.read_text(encoding="utf-8") if args.target.is_file() else ""
    changed = (desired != old or skill != current_skill)
    if args.apply and changed:
        atomic(args.target, skill)
        atomic(args.agents, desired)
    print({"status": "installed" if args.apply else "dry_run",
           "changed": changed,
           "agentsRulePresent": START in (desired if args.apply else old),
           "skillPresent": args.target.is_file() if args.apply else bool(current_skill)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())