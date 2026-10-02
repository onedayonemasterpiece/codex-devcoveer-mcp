from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME))

import direct_ops_gateway as gateway  # noqa: E402
import requirements_guard as rg  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def write_contract(root: Path) -> None:
    path = root / rg.MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "project": "demo",
        "requirements": [
            {
                "id": "CORE-001",
                "statement": "Automatically identify the object without shifting that work to the end user.",
                "critical": True,
                "acceptance": ["The primary flow does not require manual object identification."],
            },
            {
                "id": "CORE-002",
                "statement": "Keep publication creation conversational from facts through final output.",
                "critical": True,
                "acceptance": ["The user can review selected facts and publication text."],
            },
        ],
    }), encoding="utf-8")


def test_describe_automatically_attaches_compact_requirements_context(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    write_contract(root)

    out = run(gateway.run_direct_ops_v2(
        str(root), "p", "project_probe", "describe", {"operation": "requirements_context"},
    ))

    context = out["requirementsContext"]
    assert context["configured"] is True
    assert context["requirementCount"] == 2
    assert context["criticalRequirementIds"] == ["CORE-001", "CORE-002"]
    assert context["fullContextOperation"] == "requirements_context"
    assert all("acceptance" not in row for row in context["requirements"])
    assert "requirements_update" in out["operator_guidance"]["requirements"]
    assert "semantic typed operation" in out["operator_guidance"]["efficiency"]


def test_describe_has_zero_requirements_overhead_when_unconfigured(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    out = run(gateway.run_direct_ops_v2(
        str(root), "p", "project_probe", "describe", {"operation": "git_state"},
    ))

    assert "requirementsContext" not in out
    assert "efficiency" in out["operator_guidance"]
