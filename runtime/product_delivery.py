from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from direct_probe_core import ProbeError
from requirements_guard import requirements_context

POLICY_VERSION = "product-delivery-policy-v1"

KIND_VALUES = {"feature_delivery", "bugfix", "investigation", "documentation", "maintenance"}
TARGET_VALUES = {"artifact_ready", "source_ready", "owner_testable", "owner_accepted"}
SCOPE_VALUES = {"analysis", "document", "source", "unit", "integration", "live", "owner"}
RESULT_VALUES = {"pass", "fail", "unverified"}
VERIFICATION_VALUES = {"server_verified", "agent_attested"}
EVALUATOR_VALUES = {"agent", "test", "server", "owner"}
COMPLETION_OUTCOMES = {"delivered", "cancelled", "superseded", "failed"}
WAITING_KINDS = {"none", "external_job", "provider", "owner"}

POLICY_TEXT = """PRODUCT DELIVERY POLICY v1
- Define the nearest owner-relevant, verifiable result before multi-step work. Analysis is not implementation; green technical checks alone are not product completion.
- Every release blocker must come from an applicable requirement, mandatory check or authority boundary, a concrete defect, or material uncertainty. Optional audits, cleanup, hardening, model tournaments and polishing do not become gates by default.
- Reuse valid evidence for unchanged material inputs. Repeat a successful check only when the candidate/conditions changed or new evidence justifies it.
- Partial value is allowed with explicit limitations; it does not waive requirements, consent, safety, data integrity or other existing product constraints.
- Recovery continues the same saved work/task/job and preserves unknown-outcome protection. Never bypass an uncertain side effect with a new request key or wrapper.
- When the current target is actually ready, hand it off. Do not invent ETA, approval, incident, consultation or extra review steps on the normal path.
- This policy is additive: existing requirements, permissions, scope, consent, request IDs and recovery guards remain authoritative.
"""


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def requirement_snapshot(project_root: str | Path, refs: list[str]) -> dict[str, Any]:
    ctx = requirements_context(project_root)
    if not refs:
        return {
            "configured": bool(ctx.get("configured")),
            "manifest_digest": ctx.get("digest"),
            "requirements": [],
        }
    if not ctx.get("configured"):
        raise ProbeError(
            "delivery_requirement_missing",
            "delivery_contract references project requirements but no requirements contract is configured",
            status="denied",
        )
    rows = {row["id"]: row for row in ctx["requirements"]}
    missing = [ref for ref in refs if ref not in rows]
    if missing:
        raise ProbeError(
            "delivery_requirement_missing",
            "unknown delivery requirement refs: " + ", ".join(missing[:8]),
            status="denied",
        )
    selected = [
        {
            "id": rows[ref]["id"],
            "statement": rows[ref]["statement"],
            "critical": rows[ref]["critical"],
            "acceptance": rows[ref]["acceptance"],
        }
        for ref in refs
    ]
    return {
        "configured": True,
        "manifest_digest": ctx["digest"],
        "requirements": selected,
        "selected_digest": _digest(selected),
    }


def initial_state(project_root: str | Path, contract: dict[str, Any] | None) -> dict[str, Any]:
    if contract is None:
        return {}
    now = int(time.time())
    value = {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "contract_revision": 1,
        "contract": _copy(contract),
        "requirements_snapshot": requirement_snapshot(project_root, list(contract.get("requirement_refs") or [])),
        "contract_history": [{
            "revision": 1,
            "scope_ref": contract.get("scope_ref"),
            "target": contract.get("target"),
            "changed_at": now,
        }],
        "candidate": {},
        "criterion_results": [],
        "blockers": [],
        "waiting": {"kind": "none"},
        "handoff": {},
        "completion_outcome": None,
        "product_status": "unverified",
        "observations": [],
        "advisories": [],
    }
    return observe(value)


def _criterion_map(contract: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(contract, dict):
        return {}
    return {
        str(item["id"]): item
        for item in contract.get("criteria", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def apply_contract(project_root: str | Path, state: dict[str, Any], contract: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if not state:
        return initial_state(project_root, contract), True
    old_contract = state.get("contract")
    if old_contract == contract:
        refreshed, changed = refresh_requirements(project_root, state)
        return refreshed, changed
    old_target = old_contract.get("target") if isinstance(old_contract, dict) else None
    new_target = contract.get("target")
    if old_target in TARGET_VALUES and new_target in TARGET_VALUES:
        rank = {"source_ready": 0, "artifact_ready": 1, "owner_testable": 2, "owner_accepted": 3}
        if rank[new_target] < rank[old_target]:
            raise ProbeError(
                "delivery_contract_downgrade",
                "delivery target cannot be downgraded only to make completion easier; record a superseded/cancelled outcome or use a new owner scope",
                status="denied",
            )
    updated = _copy(state)
    revision = int(updated.get("contract_revision") or 0) + 1
    old_criteria = _criterion_map(old_contract)
    new_criteria = _criterion_map(contract)
    changed_ids = {
        ident for ident in set(old_criteria) | set(new_criteria)
        if old_criteria.get(ident) != new_criteria.get(ident)
    }
    if changed_ids:
        for row in updated.get("criterion_results", []):
            if row.get("criterion_id") in changed_ids:
                row["status"] = "unverified"
                row["invalidated_reason"] = "criterion_changed"
    updated["contract_revision"] = revision
    updated["contract"] = _copy(contract)
    updated["requirements_snapshot"] = requirement_snapshot(
        project_root, list(contract.get("requirement_refs") or [])
    )
    history = list(updated.get("contract_history") or [])
    history.append({
        "revision": revision,
        "scope_ref": contract.get("scope_ref"),
        "target": contract.get("target"),
        "changed_at": int(time.time()),
    })
    updated["contract_history"] = history[-12:]
    updated["completion_outcome"] = None
    updated["product_status"] = "unverified"
    return updated, True


def refresh_requirements(project_root: str | Path, state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    contract = state.get("contract")
    if not isinstance(contract, dict):
        return _copy(state), False
    current = requirement_snapshot(project_root, list(contract.get("requirement_refs") or []))
    saved = state.get("requirements_snapshot") or {}
    # Ignore unrelated manifest churn when the selected requirement rows are unchanged.
    if current.get("selected_digest") == saved.get("selected_digest") and current.get("requirements") == saved.get("requirements"):
        return _copy(state), False
    updated = _copy(state)
    updated["requirements_snapshot"] = current
    for row in updated.get("criterion_results", []):
        if row.get("status") == "pass":
            row["status"] = "unverified"
            row["invalidated_reason"] = "linked_requirement_changed"
    updated["completion_outcome"] = None
    updated["product_status"] = "unverified"
    return updated, True


def _upsert_results(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(row.get("criterion_id")): _copy(row) for row in existing if isinstance(row, dict)}
    order = [str(row.get("criterion_id")) for row in existing if isinstance(row, dict)]
    for row in incoming:
        ident = str(row["criterion_id"])
        by_id[ident] = _copy(row)
        if ident not in order:
            order.append(ident)
    return [by_id[ident] for ident in order if ident in by_id]


def apply_update(state: dict[str, Any], update: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    if not update:
        return _copy(state), False
    updated = _copy(state)
    before = _material_digest(updated)
    if "candidate" in update:
        candidate = dict(updated.get("candidate") or {})
        candidate.update(_copy(update["candidate"]))
        updated["candidate"] = candidate
    if "criterion_results" in update:
        updated["criterion_results"] = _upsert_results(
            list(updated.get("criterion_results") or []),
            list(update["criterion_results"]),
        )
    if "blockers" in update:
        updated["blockers"] = _copy(update["blockers"])
    if "waiting" in update:
        updated["waiting"] = _copy(update["waiting"])
    if "handoff" in update:
        handoff = dict(updated.get("handoff") or {})
        handoff.update(_copy(update["handoff"]))
        updated["handoff"] = handoff
    if "completion_outcome" in update:
        updated["completion_outcome"] = update["completion_outcome"]
    after = _material_digest(updated)
    if before != after:
        updated["product_status"] = "unverified"
        if updated.get("completion_outcome") == "delivered":
            updated["completion_outcome"] = None
    return updated, before != after


def _material_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: state.get(key)
        for key in ("contract", "requirements_snapshot", "candidate", "criterion_results", "blockers", "waiting", "handoff", "completion_outcome")
    }


def _material_digest(state: dict[str, Any]) -> str:
    return _digest(_material_payload(state))


def observe(state: dict[str, Any]) -> dict[str, Any]:
    if not state:
        return {}
    updated = _copy(state)
    digest = _material_digest(updated)
    observations = list(updated.get("observations") or [])
    observations.append(digest)
    updated["observations"] = observations[-8:]
    advisories: list[dict[str, Any]] = []
    waiting = updated.get("waiting") if isinstance(updated.get("waiting"), dict) else {}
    if (
        len(updated["observations"]) >= 3
        and len(set(updated["observations"][-3:])) == 1
        and waiting.get("kind", "none") == "none"
        and updated.get("product_status") != "ready"
    ):
        open_ids = open_criteria(updated)
        advisories.append({
            "code": "delivery_stalled",
            "blocking": False,
            "criterion_id": open_ids[0] if open_ids else None,
            "basis_refs": ["same material delivery state across three observed checkpoints"],
            "next_action": _next_action(updated),
            "coverage": "partial",
        })
    if updated.get("product_status") == "ready":
        advisories.append({
            "code": "handoff_ready",
            "blocking": False,
            "basis_refs": ["all current completion criteria are satisfied"],
            "next_action": "hand off the verified result; further polishing is optional",
            "coverage": "current_work",
        })
    updated["advisories"] = advisories
    return updated


def candidate_ref(state: dict[str, Any]) -> str | None:
    candidate = state.get("candidate")
    if not isinstance(candidate, dict):
        return None
    for key in ("fingerprint", "deployment_ref", "artifact_ref", "build_ref", "source_ref"):
        value = candidate.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _scope_satisfies(required: str, actual: str) -> bool:
    if required == actual:
        return True
    if required == "analysis":
        return actual in SCOPE_VALUES
    if required == "document":
        return actual in {"document", "owner"}
    rank = {"source": 0, "unit": 1, "integration": 2, "live": 3, "owner": 4}
    return required in rank and actual in rank and rank[actual] >= rank[required]


def _result_for(state: dict[str, Any], criterion_id: str) -> dict[str, Any] | None:
    for row in reversed(list(state.get("criterion_results") or [])):
        if isinstance(row, dict) and row.get("criterion_id") == criterion_id:
            return row
    return None


def open_criteria(state: dict[str, Any]) -> list[str]:
    contract = state.get("contract")
    if not isinstance(contract, dict):
        return []
    result = []
    current = candidate_ref(state)
    for criterion in contract.get("criteria", []):
        row = _result_for(state, criterion["id"])
        if row is None or row.get("status") != "pass":
            result.append(criterion["id"])
            continue
        if not _scope_satisfies(str(criterion["required_scope"]), str(row.get("actual_scope") or "")):
            result.append(criterion["id"])
            continue
        refs = row.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            result.append(criterion["id"])
            continue
        row_candidate = row.get("candidate_ref")
        if current and row_candidate != current:
            result.append(criterion["id"])
    return result


def completion_issues(
    state: dict[str, Any],
    evidence_exists: Callable[[str], bool],
) -> list[dict[str, Any]]:
    contract = state.get("contract")
    if not isinstance(contract, dict):
        return []
    issues: list[dict[str, Any]] = []
    current_candidate = candidate_ref(state)
    for criterion in contract.get("criteria", []):
        ident = criterion["id"]
        row = _result_for(state, ident)
        if row is None or row.get("status") != "pass":
            issues.append({"code": "criterion_open", "criterion_id": ident, "message": f"{ident} is not passed"})
            continue
        actual_scope = str(row.get("actual_scope") or "")
        if not _scope_satisfies(str(criterion["required_scope"]), actual_scope):
            issues.append({
                "code": "scope_insufficient",
                "criterion_id": ident,
                "message": f"{ident} requires {criterion['required_scope']} evidence, got {actual_scope or 'none'}",
            })
            continue
        refs = row.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            issues.append({"code": "evidence_missing", "criterion_id": ident, "message": f"{ident} has no evidence"})
            continue
        if current_candidate and row.get("candidate_ref") != current_candidate:
            issues.append({
                "code": "candidate_mismatch",
                "criterion_id": ident,
                "message": f"{ident} evidence belongs to a different candidate",
            })
            continue
        verification = row.get("verification")
        if verification == "server_verified":
            missing = [ref for ref in refs if not evidence_exists(str(ref))]
            if missing:
                issues.append({
                    "code": "evidence_unavailable",
                    "criterion_id": ident,
                    "message": f"{ident} references unavailable server evidence: {missing[0]}",
                })
                continue
        elif verification == "agent_attested":
            if criterion.get("allow_agent_attested", True) is False:
                issues.append({
                    "code": "attestation_not_allowed",
                    "criterion_id": ident,
                    "message": f"{ident} requires server/native evidence",
                })
                continue
            if row.get("evaluated_by") not in EVALUATOR_VALUES:
                issues.append({
                    "code": "attestation_incomplete",
                    "criterion_id": ident,
                    "message": f"{ident} attestation has no valid evaluator",
                })
                continue
        else:
            issues.append({
                "code": "verification_missing",
                "criterion_id": ident,
                "message": f"{ident} has no supported verification mode",
            })
            continue
        if actual_scope == "owner" and row.get("evaluated_by") != "owner":
            issues.append({
                "code": "owner_feedback_missing",
                "criterion_id": ident,
                "message": f"{ident} owner scope requires explicit owner evaluation",
            })
    blockers = [
        row for row in state.get("blockers", [])
        if isinstance(row, dict) and row.get("blocking_scope", "target") != "none"
    ]
    if blockers:
        first = blockers[0]
        issues.append({
            "code": "blocking_issue",
            "criterion_id": first.get("criterion_id"),
            "message": str(first.get("reason") or "a current blocker remains"),
        })
    waiting = state.get("waiting") if isinstance(state.get("waiting"), dict) else {}
    if waiting.get("kind", "none") != "none":
        issues.append({
            "code": "still_waiting",
            "message": f"work is still waiting for {waiting.get('kind')}",
        })
    target = contract.get("target")
    handoff = state.get("handoff") if isinstance(state.get("handoff"), dict) else {}
    candidate = state.get("candidate") if isinstance(state.get("candidate"), dict) else {}
    artifact_refs = handoff.get("artifact_refs") if isinstance(handoff.get("artifact_refs"), list) else []
    if target == "source_ready" and not candidate.get("source_ref"):
        issues.append({"code": "source_missing", "message": "source_ready requires a source_ref candidate"})
    if target in {"artifact_ready", "owner_testable", "owner_accepted"}:
        if not artifact_refs and not candidate.get("artifact_ref") and not candidate.get("deployment_ref"):
            issues.append({
                "code": "handoff_missing",
                "message": f"{target} requires an accessible artifact/deployment handoff",
            })
        if not handoff.get("verify"):
            issues.append({
                "code": "verification_instructions_missing",
                "message": f"{target} requires a short handoff verification instruction",
            })
    if target == "owner_accepted":
        owner_pass = any(
            isinstance(row, dict)
            and row.get("status") == "pass"
            and row.get("actual_scope") == "owner"
            and row.get("evaluated_by") == "owner"
            for row in state.get("criterion_results", [])
        )
        if not owner_pass:
            issues.append({
                "code": "owner_acceptance_missing",
                "message": "owner_accepted cannot be inferred without explicit owner feedback evidence",
            })
    return issues


def assess(
    state: dict[str, Any],
    evidence_exists: Callable[[str], bool],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not state or not isinstance(state.get("contract"), dict):
        value = _copy(state)
        if value:
            value["product_status"] = "unverified"
        return value, []
    value = _copy(state)
    outcome = value.get("completion_outcome")
    if outcome in {"cancelled", "superseded", "failed"}:
        value["product_status"] = "not_delivered"
        return value, []
    issues = completion_issues(value, evidence_exists)
    value["product_status"] = "ready" if not issues else "unverified"
    return value, issues


def validate_done(
    state: dict[str, Any],
    evidence_exists: Callable[[str], bool],
) -> dict[str, Any]:
    if not state or not isinstance(state.get("contract"), dict):
        return _copy(state)
    value, issues = assess(state, evidence_exists)
    outcome = value.get("completion_outcome")
    if outcome in {"cancelled", "superseded", "failed"}:
        return observe(value)
    if issues:
        first = issues[0]
        raise ProbeError(
            "product_readiness",
            "Cannot mark delivery work done: " + first["message"],
            status="denied",
        )
    value["completion_outcome"] = "delivered"
    value["product_status"] = "ready"
    value.setdefault("ready_at", int(time.time()))
    return observe(value)


def _next_action(state: dict[str, Any]) -> str:
    for blocker in state.get("blockers", []):
        if isinstance(blocker, dict) and blocker.get("next_action"):
            return str(blocker["next_action"])
    open_ids = open_criteria(state)
    if open_ids:
        return f"close {open_ids[0]} with applicable evidence for the current candidate"
    handoff = state.get("handoff") if isinstance(state.get("handoff"), dict) else {}
    if handoff.get("verify"):
        return "hand off the verified result using the saved verification instructions"
    return "continue the nearest result-producing step; do not add an unrelated gate"


def render_delivery_context(work: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(work, dict):
        return {
            "policyVersion": POLICY_VERSION,
            "bound": False,
            "coverage": "unbound_task",
            "complete": True,
            "guidance": "Use the nearest verifiable owner result; optional process work is not a release gate.",
        }
    state = work.get("delivery")
    if not isinstance(state, dict) or not state.get("contract"):
        return {
            "policyVersion": POLICY_VERSION,
            "workId": work.get("work_id"),
            "bound": True,
            "configured": False,
            "productStatus": "unverified" if work.get("status") == "done" else "not_configured",
            "coverage": "legacy_work",
            "complete": True,
        }
    contract = state["contract"]
    context = {
        "policyVersion": POLICY_VERSION,
        "workId": work.get("work_id"),
        "bound": True,
        "configured": True,
        "kind": contract.get("kind"),
        "target": contract.get("target"),
        "productStatus": state.get("product_status", "unverified"),
        "candidate": candidate_ref(state),
        "openCriteria": open_criteria(state)[:8],
        "blockers": [
            {
                "criterion_id": row.get("criterion_id"),
                "reason": row.get("reason"),
                "next_action": row.get("next_action"),
            }
            for row in state.get("blockers", [])[:4]
            if isinstance(row, dict)
        ],
        "waiting": (state.get("waiting") or {}).get("kind", "none"),
        "handoffAvailable": bool((state.get("handoff") or {}).get("artifact_refs") or (state.get("handoff") or {}).get("verify")),
        "nextAction": _next_action(state),
        "advisories": list(state.get("advisories") or [])[:2],
        "coverage": "current_work",
        "complete": True,
    }
    encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 1400:
        context["blockers"] = context["blockers"][:1]
        context["advisories"] = context["advisories"][:1]
        context["complete"] = False
        context["readback"] = f"work_resume:{work.get('work_id')}"
    return context


def policy_prompt(work: dict[str, Any] | None = None) -> str:
    context = render_delivery_context(work)
    return (
        POLICY_TEXT
        + "\nCurrent deliveryContext (server state; do not fabricate completion):\n"
        + json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n\n"
    )
