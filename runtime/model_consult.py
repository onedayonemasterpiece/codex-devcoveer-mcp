"""Read-only single-model consultation helpers for DevCoveer."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from session_evidence import redact

CONSULT_ROOT = Path("/home/dev/.local/share/openai-codex-mcp/model-consultations")
TASK_RE = re.compile(r"^dvt_[0-9a-f]{32}$")

CONSULT_MODELS: dict[str, dict[str, str]] = {
    "kimi_k3": {
        "provider": "nvidia",
        "nativeModelId": "moonshotai/kimi-k3",
        "selection": "nvidia/moonshotai/kimi-k3",
        "displayName": "Kimi K3",
    },
    "deepseek": {
        "provider": "nvidia",
        "nativeModelId": "deepseek-ai/deepseek-v4.1-flash",
        "selection": "nvidia/deepseek-ai/deepseek-v4.1-flash",
        "displayName": "DeepSeek V4.1 Flash",
    },
}
CONSULT_PURPOSES = ("requirements", "edge_cases", "architecture", "code_review", "ideas")

CONSULT_MODEL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {
            "type": "string", "minLength": 1, "maxLength": 128,
            "description": "Project whose read-only context the consultant may inspect.",
        },
        "model": {
            "type": "string", "enum": sorted(CONSULT_MODELS),
            "description": "Consultant alias. No automatic fallback to another model.",
        },
        "purpose": {
            "type": "string", "enum": list(CONSULT_PURPOSES),
            "description": "Why a second opinion is being requested.",
        },
        "question": {
            "type": "string", "minLength": 1, "maxLength": 20000,
            "description": "Focused question for the consultant.",
        },
    },
    "required": ["project", "model", "purpose", "question"],
    "additionalProperties": False,
}


def model_spec(alias: str) -> dict[str, str]:
    try:
        return dict(CONSULT_MODELS[alias])
    except KeyError as exc:
        raise ValueError(f"unsupported consultant model: {alias}") from exc


def consultation_prompt(*, purpose: str, question: str) -> str:
    if purpose not in CONSULT_PURPOSES:
        raise ValueError(f"unsupported consultation purpose: {purpose}")
    question = question.strip()
    if not question:
        raise ValueError("consultation question is empty")
    return (
        "You are an independent advisory model consultant inside DevCoveer. "
        "This consultation is READ ONLY and advisory: do not edit files, commit, deploy, "
        "change requirements, mutate services, or perform external writes. Inspect relevant "
        "read-only project context when useful. The calling ChatGPT remains the decision owner; "
        "disagree explicitly when warranted. Do not expose hidden chain-of-thought or secrets. "
        "Return a concise Markdown consultation with sections: Findings, Edge cases, Recommendation, "
        "and Confidence. Focus on materially useful additions rather than restating the question.\n\n"
        f"Purpose: {purpose}\n\nQuestion:\n{question}"
    )


def consultation_metadata(*, alias: str, purpose: str, question: str) -> dict[str, Any]:
    spec = model_spec(alias)
    return {
        "kind": "model_consultation",
        "modelAlias": alias,
        "displayName": spec["displayName"],
        "purpose": purpose,
        "questionSha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "markdownOnCompletion": True,
    }


def _safe_field(value: Any) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def persist_consultation(
    task: dict[str, Any],
    execution_status: str,
    final_response: str | None,
    *,
    root: Path = CONSULT_ROOT,
) -> dict[str, Any]:
    task_id = str(task.get("taskId") or "")
    if TASK_RE.fullmatch(task_id) is None:
        raise ValueError("invalid consultation task id")
    if execution_status not in {"completed", "failed", "interrupted"}:
        raise ValueError("consultation is not terminal")
    consultation = task.get("consultation")
    if not isinstance(consultation, dict) or consultation.get("kind") != "model_consultation":
        raise ValueError("task is not a model consultation")

    started = int(task.get("createdAt") or 0)
    finished = int(task.get("finishedAt") or task.get("updatedAt") or int(time.time() * 1000))
    latency_ms = max(0, finished - started) if started else None
    response = redact(final_response or f"Consultation ended with status: {execution_status}.")
    if not isinstance(response, str):
        response = str(response)

    lines = [
        "# Model consultation",
        "",
        f"- Task: `{task_id}`",
        f"- Model: {_safe_field(task.get('selection') or consultation.get('displayName') or 'unknown')}",
        f"- Purpose: {_safe_field(consultation.get('purpose') or 'unknown')}",
        f"- Execution status: {_safe_field(execution_status)}",
        f"- Latency ms: {latency_ms if latency_ms is not None else 'unknown'}",
        f"- Question SHA-256: `{_safe_field(consultation.get('questionSha256') or 'unknown')}`",
        "- Authority: advisory only; ChatGPT remains the decision owner",
        "",
        "## Consultation result",
        "",
        response.strip(),
        "",
    ]
    data = ("\n".join(lines)).encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    target = root / f"{task_id}.md"
    if target.exists():
        if target.is_symlink():
            raise ValueError("consultation artifact path is a symlink")
        existing = target.read_bytes()
        if existing == data:
            return {
                "status": "ok", "path": str(target), "sha256": digest,
                "bytes": len(data), "format": "markdown", "reused": True,
            }
    fd, tmp = tempfile.mkstemp(prefix=".consult-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {
        "status": "ok", "path": str(target), "sha256": digest,
        "bytes": len(data), "format": "markdown", "reused": False,
    }
