from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from direct_probe_core import ProbeError, _redact_text

INCIDENT_STATE_PATH = Path.home() / ".local" / "state" / "openai-codex-mcp" / "incidents.json"
INCIDENT_LOCK_PATH = INCIDENT_STATE_PATH.with_suffix(".lock")
INCIDENT_ID_RE = re.compile(r"^inc_[0-9a-f]{24}$")
STATUS_VALUES = {"open", "mitigated", "closed"}
SEVERITY_VALUES = {"sev1", "sev2", "sev3", "sev4", "unknown"}
NOTE_KINDS = {"observation", "hypothesis", "mitigation", "verification", "decision", "followup"}
MAX_INCIDENTS = 5000
MAX_TIMELINE = 1000
MAX_TEXT = 12000
TOKEN_RE = re.compile(r"[^\W_]{3,}", re.UNICODE)


def _now() -> int:
    return int(time.time())


def _clean_text(value: str, *, field: str, max_len: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise ProbeError("invalid_input", f"{field} must be text")
    value = value.strip()
    if not value:
        raise ProbeError("invalid_input", f"{field} must not be empty")
    if len(value) > max_len:
        raise ProbeError("invalid_input", f"{field} is too long")
    redacted, _ = _redact_text(value)
    return redacted


def _clean_list(values: list[str] | None, *, field: str, max_items: int = 32, max_len: int = 200) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > max_items:
        raise ProbeError("invalid_input", f"{field} must be a bounded list")
    out: list[str] = []
    for raw in values:
        value = _clean_text(raw, field=field, max_len=max_len)
        if value not in out:
            out.append(value)
    return out


def _ensure_state_dir() -> None:
    INCIDENT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(INCIDENT_STATE_PATH.parent, 0o700)
    except OSError:
        pass


def _load_unlocked() -> dict[str, Any]:
    if not INCIDENT_STATE_PATH.exists():
        return {"version": 1, "incidents": []}
    if INCIDENT_STATE_PATH.is_symlink():
        raise ProbeError("incident_store", "incident registry may not be a symlink")
    try:
        raw = json.loads(INCIDENT_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProbeError("incident_store", "incident registry is unreadable") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("incidents"), list):
        raise ProbeError("incident_store", "incident registry has an unsupported shape")
    return raw


def _save_unlocked(state: dict[str, Any]) -> None:
    incidents = state.get("incidents")
    if not isinstance(incidents, list) or len(incidents) > MAX_INCIDENTS:
        raise ProbeError("incident_store", "incident registry exceeds bounded capacity")
    _ensure_state_dir()
    fd, tmp = tempfile.mkstemp(prefix=".incidents-", dir=INCIDENT_STATE_PATH.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, INCIDENT_STATE_PATH)
        os.chmod(INCIDENT_STATE_PATH, 0o600)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


class _LockedState:
    def __enter__(self) -> dict[str, Any]:
        _ensure_state_dir()
        fd = os.open(INCIDENT_LOCK_PATH, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._handle = os.fdopen(fd, "r+")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        self.state = _load_unlocked()
        return self.state

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                _save_unlocked(self.state)
        finally:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()


def _tokens(*values: str) -> set[str]:
    out: set[str] = set()
    for value in values:
        out.update(token.casefold() for token in TOKEN_RE.findall(value or ""))
    stop = {
        "incident", "production", "service", "error", "failure", "problem",
        "инцидент", "проблема", "ошибка", "сервис", "production",
    }
    return {token for token in out if token not in stop}


def _incident_summary(incident: dict[str, Any], *, relation_score: int | None = None) -> dict[str, Any]:
    out = {
        "incident_id": incident["incident_id"],
        "project": incident["project"],
        "title": incident["title"],
        "severity": incident["severity"],
        "status": incident["status"],
        "services": list(incident.get("services") or []),
        "tags": list(incident.get("tags") or []),
        "created_at": incident["created_at"],
        "updated_at": incident["updated_at"],
        "closed_at": incident.get("closed_at"),
        "related_incidents": list(incident.get("related_incidents") or []),
    }
    if relation_score is not None:
        out["relation_score"] = relation_score
    return out


def _relation_score(candidate: dict[str, Any], project: str, title: str, summary: str,
                    services: list[str], tags: list[str]) -> int:
    if candidate.get("project") != project:
        return 0
    score = 1
    candidate_tokens = _tokens(
        str(candidate.get("title") or ""),
        str(candidate.get("summary") or ""),
        " ".join(candidate.get("services") or []),
        " ".join(candidate.get("tags") or []),
    )
    current_tokens = _tokens(title, summary, " ".join(services), " ".join(tags))
    score += 2 * len(candidate_tokens & current_tokens)
    score += 4 * len(set(candidate.get("services") or []) & set(services))
    score += 3 * len(set(candidate.get("tags") or []) & set(tags))
    return score


def related_incidents(state: dict[str, Any], *, project: str, title: str, summary: str,
                      services: list[str], tags: list[str], limit: int = 5,
                      exclude_id: str | None = None) -> list[dict[str, Any]]:
    ranked: list[tuple[int, dict[str, Any]]] = []
    for incident in state["incidents"]:
        if incident.get("incident_id") == exclude_id:
            continue
        score = _relation_score(incident, project, title, summary, services, tags)
        if score >= 3:
            ranked.append((score, incident))
    ranked.sort(key=lambda row: (row[0], int(row[1].get("updated_at") or 0)), reverse=True)
    return [_incident_summary(item, relation_score=score) for score, item in ranked[:limit]]


def search_incidents(project: str, *, query: str | None = None, status: str | None = None,
                     severity: str | None = None, limit: int = 20) -> dict[str, Any]:
    if status is not None and status not in STATUS_VALUES:
        raise ProbeError("invalid_input", "unknown incident status")
    if severity is not None and severity not in SEVERITY_VALUES:
        raise ProbeError("invalid_input", "unknown incident severity")
    limit = max(1, min(int(limit), 100))
    query_tokens = _tokens(query or "")
    with _LockedState() as state:
        rows: list[tuple[int, dict[str, Any]]] = []
        for incident in state["incidents"]:
            if incident.get("project") != project:
                continue
            if status is not None and incident.get("status") != status:
                continue
            if severity is not None and incident.get("severity") != severity:
                continue
            hay = _tokens(
                str(incident.get("title") or ""),
                str(incident.get("summary") or ""),
                " ".join(incident.get("services") or []),
                " ".join(incident.get("tags") or []),
                str(incident.get("root_cause") or ""),
                str(incident.get("resolution") or ""),
            )
            if query_tokens and not (query_tokens & hay):
                continue
            score = len(query_tokens & hay) if query_tokens else 0
            rows.append((score, incident))
        rows.sort(key=lambda row: (row[0], int(row[1].get("updated_at") or 0)), reverse=True)
        return {
            "status": "ok",
            "project": project,
            "incidents": [_incident_summary(item) for _, item in rows[:limit]],
            "count": min(len(rows), limit),
            "complete": len(rows) <= limit,
        }


def get_incident(project: str, incident_id: str) -> dict[str, Any]:
    if INCIDENT_ID_RE.fullmatch(incident_id) is None:
        raise ProbeError("invalid_input", "invalid incident id")
    with _LockedState() as state:
        for incident in state["incidents"]:
            if incident.get("incident_id") == incident_id:
                if incident.get("project") != project:
                    raise ProbeError("incident_denied", "incident belongs to another project", status="denied")
                related = [
                    _incident_summary(other)
                    for other in state["incidents"]
                    if other.get("incident_id") in set(incident.get("related_incidents") or [])
                ]
                return {"status": "ok", "incident": dict(incident), "related": related}
    raise ProbeError("not_found", "incident not found", status="not_found")


def open_incident(project: str, action: dict[str, Any]) -> dict[str, Any]:
    title = _clean_text(action["title"], field="title", max_len=300)
    summary = _clean_text(action["summary"], field="summary")
    severity = str(action.get("severity") or "unknown")
    if severity not in SEVERITY_VALUES:
        raise ProbeError("invalid_input", "unknown incident severity")
    services = _clean_list(action.get("services"), field="services", max_items=32, max_len=100)
    tags = _clean_list(action.get("tags"), field="tags", max_items=32, max_len=100)
    explicit_related = _clean_list(action.get("related_incidents"), field="related_incidents", max_items=32, max_len=64)
    for incident_id in explicit_related:
        if INCIDENT_ID_RE.fullmatch(incident_id) is None:
            raise ProbeError("invalid_input", "related_incidents contains an invalid incident id")
    now = _now()
    with _LockedState() as state:
        for existing in state["incidents"]:
            if existing.get("project") == project and existing.get("status") != "closed" and existing.get("title") == title:
                rel = related_incidents(
                    state, project=project, title=title, summary=summary, services=services,
                    tags=tags, exclude_id=existing["incident_id"],
                )
                return {"status": "ok", "reused": True, "incident": _incident_summary(existing), "related": rel}
        automatic = related_incidents(
            state, project=project, title=title, summary=summary, services=services, tags=tags,
        )
        related_ids = list(dict.fromkeys([*explicit_related, *(row["incident_id"] for row in automatic)]))
        incident = {
            "incident_id": "inc_" + secrets.token_hex(12),
            "project": project,
            "title": title,
            "summary": summary,
            "severity": severity,
            "status": "open",
            "services": services,
            "tags": tags,
            "related_incidents": related_ids,
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
            "root_cause": None,
            "resolution": None,
            "verification": None,
            "action_items": [],
            "timeline": [{
                "at": now,
                "kind": "opened",
                "note": "Incident opened; related incident history must be reviewed before remediation.",
                "evidence_refs": [],
            }],
        }
        state["incidents"].append(incident)
        return {"status": "ok", "reused": False, "incident": _incident_summary(incident), "related": automatic}


def note_incident(project: str, action: dict[str, Any]) -> dict[str, Any]:
    incident_id = str(action["incident_id"])
    if INCIDENT_ID_RE.fullmatch(incident_id) is None:
        raise ProbeError("invalid_input", "invalid incident id")
    note = _clean_text(action["note"], field="note")
    kind = str(action.get("note_kind") or "observation")
    if kind not in NOTE_KINDS:
        raise ProbeError("invalid_input", "unknown incident note kind")
    evidence_refs = _clean_list(action.get("evidence_refs"), field="evidence_refs", max_items=32, max_len=500)
    now = _now()
    with _LockedState() as state:
        for incident in state["incidents"]:
            if incident.get("incident_id") != incident_id:
                continue
            if incident.get("project") != project:
                raise ProbeError("incident_denied", "incident belongs to another project", status="denied")
            if incident.get("status") == "closed":
                raise ProbeError("incident_closed", "closed incident cannot receive timeline mutations", status="denied")
            timeline = incident.setdefault("timeline", [])
            if len(timeline) >= MAX_TIMELINE:
                raise ProbeError("incident_capacity", "incident timeline is full", status="denied")
            timeline.append({"at": now, "kind": kind, "note": note, "evidence_refs": evidence_refs})
            incident["updated_at"] = now
            return {"status": "ok", "incident": _incident_summary(incident), "timeline_count": len(timeline)}
    raise ProbeError("not_found", "incident not found", status="not_found")


def close_incident(project: str, action: dict[str, Any]) -> dict[str, Any]:
    incident_id = str(action["incident_id"])
    if INCIDENT_ID_RE.fullmatch(incident_id) is None:
        raise ProbeError("invalid_input", "invalid incident id")
    root_cause = _clean_text(action["root_cause"], field="root_cause")
    resolution = _clean_text(action["resolution"], field="resolution")
    verification = _clean_text(action["verification"], field="verification")
    action_items = _clean_list(action.get("action_items"), field="action_items", max_items=100, max_len=1000)
    now = _now()
    with _LockedState() as state:
        for incident in state["incidents"]:
            if incident.get("incident_id") != incident_id:
                continue
            if incident.get("project") != project:
                raise ProbeError("incident_denied", "incident belongs to another project", status="denied")
            incident["root_cause"] = root_cause
            incident["resolution"] = resolution
            incident["verification"] = verification
            incident["action_items"] = action_items
            incident["status"] = "closed"
            incident["updated_at"] = now
            incident["closed_at"] = now
            timeline = incident.setdefault("timeline", [])
            timeline.append({
                "at": now,
                "kind": "closed",
                "note": "Incident closed after live verification.",
                "evidence_refs": [],
            })
            return {"status": "ok", "incident": _incident_summary(incident)}
    raise ProbeError("not_found", "incident not found", status="not_found")
