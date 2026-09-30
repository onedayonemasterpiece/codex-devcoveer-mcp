"""Bounded diagnostics and retained operation receipts. No source or command execution."""
from __future__ import annotations
import base64
import datetime as dt
import fcntl
import json
import logging
import os
from pathlib import Path
import secrets
import time
from jsonschema import Draft202012Validator
from direct_probe_core import ProbeError, _redact_text


def validation_message(error):
    if error.validator == 'oneOf' and isinstance(error.instance, dict):
        kind = error.instance.get('kind')
        for variant in error.schema.get('oneOf', []):
            if variant.get('properties', {}).get('kind', {}).get('const') == kind:
                errors = list(Draft202012Validator(variant).iter_errors(error.instance))
                if errors:
                    child = errors[0]
                    return '.'.join(map(str, child.path)) + ': ' + validation_message(child)
    if error.validator == 'required':
        missing = [key for key in error.validator_value if key not in error.instance]
        return 'missing required field: ' + ', '.join(missing[:5])
    if error.validator == 'additionalProperties':
        return 'unexpected fields; use the live operation schema'
    descriptions = {'maximum':'number exceeds maximum', 'minimum':'number is below minimum',
        'maxLength':'text exceeds maximum length', 'minLength':'text is too short',
        'maxItems':'too many items', 'minItems':'too few items', 'pattern':'invalid identifier or format',
        'enum':'unsupported value', 'type':'incorrect value type', 'oneOf':'invalid operation variant'}
    return descriptions.get(error.validator, 'schema constraint not satisfied')


def bounded_snapshot(messages, limit=16000):
    rows = []
    for message in (messages or [])[-6:]:
        if not isinstance(message, dict):
            continue
        info = message.get('info') or {}
        row = {'info': {k: info[k] for k in ('id','role','time') if k in info}, 'parts': []}
        for part in (message.get('parts') or [])[-5:]:
            if not isinstance(part, dict):
                continue
            if part.get('type') == 'text':
                row['parts'].append({'type':'text','text':_redact_text(str(part.get('text',''))[:1000])[0]})
            elif part.get('type') == 'tool':
                state = part.get('state') or {}
                row['parts'].append({'type':'tool','tool':part.get('tool'),
                    'status':state.get('status'),
                    'output_tail':_redact_text(str(state.get('output',''))[-1000:])[0]})
        rows.append(row)
    while rows and len(json.dumps(rows, ensure_ascii=False).encode()) > limit:
        rows.pop(0)
    return rows, rows != (messages or [])


def append_receipt(path: Path, entry: dict, max_bytes: int):
    """Rotate without deleting history; retention cleanup is a separate explicit operation."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = dict(entry)
        payload.setdefault('receipt_id', secrets.token_hex(12))
        payload.setdefault('observed_at', dt.datetime.now(dt.timezone.utc).isoformat())
        line = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',',':'))+'\n').encode()
        fd = os.open(str(path)+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.is_symlink():
                return
            if path.exists() and path.stat().st_size + len(line) > max_bytes:
                os.replace(path, path.with_name(path.name+'.'+str(time.time_ns())+'.archive'))
            out = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out, 'ab') as handle:
                handle.write(line)
    except (OSError, ValueError, TypeError):
        logging.getLogger('devcoveer.process').warning('receipt_append_unavailable')


def _key(row):
    raw = row.get('observed_at', '')
    try:
        stamp = dt.datetime.fromisoformat(raw.replace('Z','+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        stamp = 0.0
    tie = row.get('receipt_id') or __import__('hashlib').sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    return stamp, tie


def read_receipts(path: Path, cwd: str, *, limit: int = 20, before: str | None = None):
    limit = max(1, min(int(limit), 100))
    cursor = None
    if before:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(before.encode()).decode())
            cursor = (float(decoded[0]), str(decoded[1]))
        except (ValueError, TypeError, IndexError):
            raise ProbeError('invalid_input', 'invalid receipt cursor', status='denied')
    paths = sorted(path.parent.glob(path.name+'.*.archive'), reverse=True)
    paths.insert(0, path)
    rows = []
    total = 0
    complete = True
    oldest = None
    for segment in paths:
        try:
            if segment.is_symlink():
                complete = False
                continue
            size = segment.stat().st_size
            if total + size > 64 * 1024 * 1024:
                complete = False
                break
            total += size
            with segment.open(encoding='utf-8') as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        complete = False
                        continue
                    if not isinstance(item, dict):
                        complete = False
                        continue
                    key = _key(item)
                    if oldest is None or key < oldest[0]:
                        oldest = (key, item.get('observed_at'))
                    if item.get('cwd') == cwd and (cursor is None or key < cursor):
                        rows.append(item)
        except FileNotFoundError:
            if segment != path:
                complete = False
        except (OSError, UnicodeError):
            complete = False
    rows.sort(key=_key, reverse=True)
    more = len(rows) > limit
    selected = rows[:limit]
    next_cursor = base64.urlsafe_b64encode(json.dumps(_key(selected[-1])).encode()).decode() if more else None
    return {'entries': selected, 'limit': limit, 'has_more': more,
        'complete': complete and not more, 'scan_complete': complete, 'next_cursor': next_cursor,
        'retained_since': oldest[1] if oldest else None,
        'coverage_before_retained_since': 'unknown', 'segments_scanned': len(paths),
        'note': 'Coverage describes retained records, not all historical activity. Older records may predate this release.'}
