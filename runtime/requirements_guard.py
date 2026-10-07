from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from direct_probe_core import ProbeError

MANIFEST_PATH = ".devcoveer/requirements.json"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_REQUIREMENTS = 50
AUTH_TTL_SECONDS = 4 * 60 * 60
REQUIREMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,63}$")
STATE_ROOT = Path.home() / ".local" / "state" / "openai-codex-mcp" / "requirements-authorizations"


def _root(project_root: str | Path) -> Path:
    return Path(project_root).resolve(strict=True)


def _manifest_file(project_root: str | Path) -> Path:
    return _root(project_root) / MANIFEST_PATH


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProbeError("requirements_invalid", "requirements manifest must be a JSON object", status="denied")
    if set(value) - {"schema_version", "project", "requirements"}:
        raise ProbeError("requirements_invalid", "requirements manifest has unsupported top-level fields", status="denied")
    if value.get("schema_version") != 1:
        raise ProbeError("requirements_invalid", "requirements schema_version must be 1", status="denied")
    project = value.get("project")
    if not isinstance(project, str) or not project.strip() or len(project) > 128:
        raise ProbeError("requirements_invalid", "requirements project must be a non-empty string", status="denied")
    raw_requirements = value.get("requirements")
    if not isinstance(raw_requirements, list) or not raw_requirements or len(raw_requirements) > MAX_REQUIREMENTS:
        raise ProbeError("requirements_invalid", "requirements must contain 1..50 entries", status="denied")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in raw_requirements:
        if not isinstance(raw, dict) or set(raw) - {"id", "statement", "critical", "acceptance"}:
            raise ProbeError("requirements_invalid", "requirement entry has unsupported fields", status="denied")
        ident = raw.get("id")
        statement = raw.get("statement")
        if not isinstance(ident, str) or REQUIREMENT_ID_RE.fullmatch(ident) is None:
            raise ProbeError("requirements_invalid", "requirement id is invalid", status="denied")
        if ident in seen:
            raise ProbeError("requirements_invalid", f"duplicate requirement id {ident}", status="denied")
        seen.add(ident)
        if not isinstance(statement, str) or not statement.strip() or len(statement) > 1200:
            raise ProbeError("requirements_invalid", f"requirement {ident} statement is invalid", status="denied")
        critical = raw.get("critical", True)
        if not isinstance(critical, bool):
            raise ProbeError("requirements_invalid", f"requirement {ident} critical must be boolean", status="denied")
        acceptance = raw.get("acceptance", [])
        if not isinstance(acceptance, list) or len(acceptance) > 10 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 500 for item in acceptance
        ):
            raise ProbeError("requirements_invalid", f"requirement {ident} acceptance is invalid", status="denied")
        normalized.append({
            "id": ident,
            "statement": statement.strip(),
            "critical": critical,
            "acceptance": [item.strip() for item in acceptance],
        })
    return {"schema_version": 1, "project": project.strip(), "requirements": normalized}


def _read_manifest(project_root: str | Path) -> tuple[dict[str, Any] | None, str | None]:
    path = _manifest_file(project_root)
    if not path.exists():
        return None, None
    if path.is_symlink() or not path.is_file():
        raise ProbeError("requirements_invalid", "requirements manifest must be a regular file", status="denied")
    data = path.read_bytes()
    if len(data) > MAX_MANIFEST_BYTES:
        raise ProbeError("requirements_invalid", "requirements manifest exceeds 64 KiB", status="denied")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ProbeError("requirements_invalid", "requirements manifest is not valid UTF-8 JSON", status="denied") from exc
    return _validate_manifest(value), _sha256(data)


def requirements_context(project_root: str | Path) -> dict[str, Any]:
    manifest, digest = _read_manifest(project_root)
    if manifest is None:
        return {
            "status": "ok",
            "configured": False,
            "manifestPath": MANIFEST_PATH,
            "changePolicy": "Create/update only through requirements_update after explicit user requirement-change intent.",
        }
    rows = manifest["requirements"]
    return {
        "status": "ok",
        "configured": True,
        "manifestPath": MANIFEST_PATH,
        "digest": digest,
        "project": manifest["project"],
        "requirements": rows,
        "criticalRequirementIds": [row["id"] for row in rows if row["critical"]],
        "changePolicy": (
            "Implementation may change; requirement statements do not. If the current task conflicts with this "
            "contract, surface the conflict. Change the contract only through requirements_update after explicit "
            "user requirement-change intent."
        ),
    }


def requirements_summary_context(project_root: str | Path) -> dict[str, Any]:
    context = requirements_context(project_root)
    if not context["configured"]:
        return context
    rows = context["requirements"]
    return {
        "status": "ok",
        "configured": True,
        "manifestPath": MANIFEST_PATH,
        "digest": context["digest"],
        "project": context["project"],
        "requirementCount": len(rows),
        "criticalRequirementIds": context["criticalRequirementIds"],
        "requirements": [
            {"id": row["id"], "critical": row["critical"], "statement": row["statement"][:700]}
            for row in rows
        ],
        "fullContextOperation": "requirements_context",
        "changePolicy": context["changePolicy"],
    }


def requirements_prompt(project_root: str | Path) -> str:
    context = requirements_context(project_root)
    if not context["configured"]:
        return ""
    lines = [
        "PROJECT REQUIREMENTS CONTRACT (must be preserved unless the current user explicitly requested a requirement change).",
        f"Manifest: {MANIFEST_PATH}; digest: {context['digest']}.",
        "Do not silently weaken, remove, reinterpret, or trade away these requirements as an implementation detail.",
        "If the requested task conflicts with the contract, report the conflict instead of mutating the requirement.",
    ]
    for row in context["requirements"]:
        marker = "CRITICAL" if row["critical"] else "CORE"
        lines.append(f"- [{row['id']}] [{marker}] {row['statement']}")
        for acceptance in row["acceptance"]:
            lines.append(f"  acceptance: {acceptance}")
    return "\n".join(lines) + "\n\n"


def patch_touches_requirements(patch: str | bytes) -> bool:
    data = patch.encode("utf-8") if isinstance(patch, str) else bytes(patch)
    needle = MANIFEST_PATH.encode("utf-8")
    for line in data.splitlines():
        if line.startswith((b"diff --git ", b"--- ", b"+++ ", b"rename from ", b"rename to ")) and needle in line:
            return True
    return False


def guard_requirement_mutation(project_root: str | Path, action: dict[str, Any]) -> None:
    kind = str(action.get("kind") or "")
    if kind == "requirements_update":
        return
    protected = False
    if kind in {"write_text", "text_edit", "delete_file", "chmod_executable"}:
        protected = action.get("path") == MANIFEST_PATH
    elif kind == "move_path":
        protected = action.get("source") == MANIFEST_PATH or action.get("target") == MANIFEST_PATH
    elif kind == "apply_source_patch":
        protected = patch_touches_requirements(action.get("patch", ""))
    if protected:
        raise ProbeError(
            "requirements_change_required",
            f"{MANIFEST_PATH} is a protected requirements contract; use requirements_update only after explicit user requirement-change intent",
            status="denied",
        )


def _authorization_path(project_root: str | Path) -> Path:
    key = hashlib.sha256(str(_root(project_root)).encode("utf-8")).hexdigest()
    return STATE_ROOT / f"{key}.json"


def _git_head(project_root: str | Path) -> str:
    completed = subprocess.run(
        ["/usr/bin/git", "-C", str(_root(project_root)), "rev-parse", "HEAD"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10,
    )
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", completed.stdout.strip()) is None:
        raise ProbeError("requirements_git", "could not resolve project HEAD", status="denied")
    return completed.stdout.strip()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".requirements-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def update_requirements(project_root: str | Path, action: dict[str, Any]) -> dict[str, Any]:
    root = _root(project_root)
    _current, current_digest = _read_manifest(root)
    expected = action.get("expected_digest")
    if current_digest is None:
        if expected is not None:
            raise ProbeError("precondition_failed", "requirements manifest does not exist", status="denied")
    elif expected != current_digest:
        raise ProbeError("precondition_failed", "requirements digest does not match expected_digest", status="denied")
    raw = action["content"].encode("utf-8")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ProbeError("requirements_invalid", "requirements manifest exceeds 64 KiB", status="denied")
    try:
        manifest = _validate_manifest(json.loads(raw.decode("utf-8")))
    except (UnicodeError, ValueError) as exc:
        raise ProbeError("requirements_invalid", "requirements content is not valid UTF-8 JSON", status="denied") from exc
    canonical = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path = root / MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ProbeError("requirements_invalid", "requirements directory may not be a symlink", status="denied")
    fd, temp_name = tempfile.mkstemp(prefix=".requirements-", dir=path.parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    new_digest = _sha256(canonical)
    authorization = {
        "version": 1,
        "projectRoot": str(root),
        "requirementsDigest": new_digest,
        "authorizationRef": action["authorization_ref"].strip(),
        "reason": action["reason"].strip(),
        "createdAt": int(time.time()),
        "headAtAuthorization": _git_head(root),
    }
    _atomic_json(_authorization_path(root), authorization)
    return {
        "status": "ok",
        "action": "requirements_update",
        "manifestPath": MANIFEST_PATH,
        "oldDigest": current_digest,
        "newDigest": new_digest,
        "requirementCount": len(manifest["requirements"]),
        "authorizationRef": authorization["authorizationRef"],
        "note": "Requirement changes are explicit and audit-scoped; stage and commit normally after review.",
    }


def _staged_manifest(project_root: str | Path) -> bytes | None:
    root = _root(project_root)
    completed = subprocess.run(
        ["/usr/bin/git", "-C", str(root), "diff", "--cached", "--name-only", "-z", "--", MANIFEST_PATH],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
    )
    if completed.returncode != 0 or MANIFEST_PATH.encode("utf-8") not in completed.stdout.split(b"\0"):
        return None
    staged = subprocess.run(
        ["/usr/bin/git", "-C", str(root), "show", f":{MANIFEST_PATH}"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
    )
    if staged.returncode != 0:
        raise ProbeError(
            "requirements_change_required",
            "staged requirements deletion is not authorized; replace the contract through requirements_update",
            status="denied",
        )
    return staged.stdout


def requirements_commit_guard(project_root: str | Path) -> dict[str, Any] | None:
    root = _root(project_root)
    staged = _staged_manifest(root)
    if staged is None:
        return None
    try:
        _validate_manifest(json.loads(staged.decode("utf-8")))
    except (UnicodeError, ValueError) as exc:
        raise ProbeError("requirements_invalid", "staged requirements contract is invalid", status="denied") from exc
    staged_digest = _sha256(staged)
    path = _authorization_path(root)
    try:
        authorization = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProbeError(
            "requirements_change_required",
            "requirements manifest is staged without a requirements_update authorization",
            status="denied",
        ) from exc
    if not isinstance(authorization, dict):
        raise ProbeError("requirements_change_required", "requirements authorization is invalid", status="denied")
    age = int(time.time()) - int(authorization.get("createdAt") or 0)
    if (
        authorization.get("version") != 1
        or authorization.get("projectRoot") != str(root)
        or authorization.get("requirementsDigest") != staged_digest
        or not isinstance(authorization.get("authorizationRef"), str)
        or not authorization["authorizationRef"].strip()
        or age < 0
        or age > AUTH_TTL_SECONDS
    ):
        raise ProbeError("requirements_change_required", "requirements authorization is stale or does not match staged contract", status="denied")
    return {
        "requirementsDigest": staged_digest,
        "authorizationRef": authorization["authorizationRef"],
        "reason": authorization.get("reason"),
    }


def consume_requirements_authorization(project_root: str | Path) -> None:
    path = _authorization_path(project_root)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
