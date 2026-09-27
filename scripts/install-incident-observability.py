#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME_AGENTS = Path("/home/dev/AGENTS.md")
SKILL_SOURCE = ROOT / "skills" / "devcoveer-incident-investigation" / "SKILL.md"
SKILL_TARGET = Path("/home/dev/.local/share/devcoveer/auth/codex/skills/devcoveer-incident-investigation/SKILL.md")
START = "<!-- DEVCOVEER_INCIDENT_OBSERVABILITY_START -->"
END = "<!-- DEVCOVEER_INCIDENT_OBSERVABILITY_END -->"
BLOCK = """<!-- DEVCOVEER_INCIDENT_OBSERVABILITY_START -->
## Incident engineering and observability

- For production/live outages, material regressions, failed recoveries/deployments, repeated live failures, data-integrity/security/safety incidents, or unreproduced production bugs, use the `devcoveer-incident-investigation` skill before remediation. Ordinary local/unit-test failures do not require an incident.
- Incident work is evidence-first: search/read related past incidents, inspect registered live logs/health, open or resume the incident, record observations/hypotheses/mitigation, then verify against the live system before closing. Do not declare runtime root cause from source alone when logs or health evidence exist.
- Use `host_probe log_search` for targeted time-bounded log investigation; use `inventory` to discover aliases. Tool-addressable worktrees inherit observability from their GitHub repository.
- When changing live failure-prone behavior, add or improve concise structured logging at external boundaries, decisions, retries, state transitions, failures and successful outcomes. Include useful correlation/release/component context; never log credentials or private payloads.
- Typed operations are preferred when they exactly express the action because they leave clearer evidence. They are not a capability boundary: full-power agent/shell remains available for legitimate operations absent from the typed surface.
- Detailed workflow: `/home/dev/.local/share/devcoveer/auth/codex/skills/devcoveer-incident-investigation/SKILL.md`.
<!-- DEVCOVEER_INCIDENT_OBSERVABILITY_END -->"""


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="."+path.name+".", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def transformed_agents(current: str) -> str:
    if START in current or END in current:
        if current.count(START) != 1 or current.count(END) != 1:
            raise SystemExit("managed incident block markers are inconsistent")
        before, rest = current.split(START, 1)
        _old, after = rest.split(END, 1)
        return before.rstrip() + "\n\n" + BLOCK + after
    return current.rstrip() + "\n\n" + BLOCK + "\n"


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args=parser.parse_args()
    if not SKILL_SOURCE.is_file():
        raise SystemExit("source incident skill is missing")
    current = HOME_AGENTS.read_text(encoding="utf-8") if HOME_AGENTS.exists() else ""
    agents = transformed_agents(current)
    skill = SKILL_SOURCE.read_text(encoding="utf-8")
    print({"status":"ok","applyRequested":args.apply,
           "agentsChanged":agents != current,
           "skillChanged":not SKILL_TARGET.exists() or SKILL_TARGET.read_text(encoding="utf-8") != skill})
    if not args.apply:
        return 0
    atomic_write(HOME_AGENTS, agents, 0o644)
    atomic_write(SKILL_TARGET, skill, 0o644)
    print({"status":"installed","agents":str(HOME_AGENTS),"skill":str(SKILL_TARGET)})
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
