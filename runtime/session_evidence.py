"""Durable redacted OpenCode evidence. Raw transcripts are never persisted here."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

TASK_RE = re.compile(r"dvt_[0-9a-f]{32}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
ROOT = Path('/home/dev/.local/share/openai-codex-mcp/session-evidence')
SECRET_KEY = re.compile(r'(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|private[_-]?key|credential)', re.I)
PATTERNS = (
    re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----'),
    re.compile(r'\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+', re.I),
    re.compile(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]+|AKIA[A-Z0-9]{16})\b'),
    re.compile(r'''(?im)(?:[\w.-]*(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|credential)[\w.-]*)["']?\s*[:=]\s*(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)'''),
    re.compile(r'(https?://)[^\s/@:]+:[^\s/@]+@', re.I),
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: '[REDACTED]' if SECRET_KEY.search(k) and k != 'tokens' else redact(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pattern in PATTERNS:
            value = pattern.sub('[REDACTED]', value)
    return value


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def capture(task: dict, messages: list, execution_status: str, root: Path = ROOT) -> dict:
    task_id = task['taskId']
    if not TASK_RE.fullmatch(task_id) or execution_status not in {'completed', 'failed', 'interrupted'}:
        raise ValueError('invalid terminal evidence request')
    # Hidden reasoning is unnecessary for external acceptance and is not exposed.
    safe_messages = []
    for message in messages:
        safe_messages.append({**message, 'parts': [p for p in message.get('parts', [])
                              if p.get('type') != 'reasoning']})
    document = redact({'schema': 'devcoveer-session-evidence.v1', 'taskId': task_id,
        'sessionId': task['backendSessionId'], 'executionStatus': execution_status,
        'messages': safe_messages})
    data = encode(document)
    digest = hashlib.sha256(data).hexdigest()
    folder = root / task_id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    os.chmod(folder, 0o700)
    target = folder / (digest + '.json')
    if target.exists():
        if target.is_symlink() or target.read_bytes() != data:
            raise ValueError('evidence integrity failure')
    else:
        fd, tmp = tempfile.mkstemp(prefix='.evidence-', dir=folder)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    return {'evidenceSha256': digest, 'messageCount': len(messages),
            'redacted': encode(safe_messages) != encode(document['messages']),
            'bytes': len(data), 'complete': True}


def read(task_id: str, digest: str, root: Path = ROOT) -> dict:
    if not TASK_RE.fullmatch(task_id) or not SHA_RE.fullmatch(digest):
        raise ValueError('invalid evidence identifier')
    path = root / task_id / (digest + '.json')
    if path.is_symlink(): raise ValueError('invalid evidence file')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('evidence integrity failure')
    return json.loads(data)
