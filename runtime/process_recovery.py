"""Durable readback for direct operations; never retries an uncertain side effect."""
from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Awaitable, Callable

from direct_probe_core import ProbeError, _redact_text
from product_delivery import (
    apply_contract as delivery_apply_contract,
    apply_update as delivery_apply_update,
    assess as delivery_assess,
    observe as delivery_observe,
    refresh_requirements as delivery_refresh_requirements,
    render_delivery_context,
    validate_done as delivery_validate_done,
)

DB_PATH = Path.home() / '.local/state/openai-codex-mcp/process-recovery.sqlite3'
CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar('direct_operation_context', default={})
LOG = logging.getLogger('devcoveer.process')
MAX_RESULT_BYTES = 2 * 1024 * 1024
READBACK_BYTES = 16000
READ_ACTIONS = {'job_status', 'job_wait', 'job_list', 'github_run_wait', 'operation_get', 'operation_list', 'operation_result'}
WORK_ACTIONS = {'work_open', 'work_checkpoint'}
PROCESS_READS = {'work_list', 'work_resume', 'operation_get', 'operation_list', 'operation_result', 'safety_trace', 'recovery_snapshot'}


def _safe(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)[0]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe(v) for v in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(DB_PATH, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    db = sqlite3.connect(DB_PATH, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS works (
          work_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, project TEXT NOT NULL,
          request_key TEXT NOT NULL, goal TEXT NOT NULL, next_step TEXT NOT NULL,
          incident_id TEXT, revision INTEGER NOT NULL, status TEXT NOT NULL,
          artifacts TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
          UNIQUE(cwd, request_key));
        CREATE TABLE IF NOT EXISTS operations (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE,
          cwd TEXT NOT NULL, project TEXT NOT NULL, plane TEXT NOT NULL,
          operation TEXT NOT NULL, request_key TEXT NOT NULL, spec_sha TEXT NOT NULL,
          work_id TEXT, incident_id TEXT, started REAL NOT NULL, finished REAL,
          state TEXT NOT NULL, result TEXT, error_type TEXT, owner_pid INTEGER,
          UNIQUE(cwd, request_key));
        CREATE INDEX IF NOT EXISTS operations_work ON operations(work_id, seq);
        CREATE INDEX IF NOT EXISTS operations_cwd ON operations(cwd, seq);
        ''')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(works)').fetchall()}
        if 'delivery_state' not in columns:
            db.execute("ALTER TABLE works ADD COLUMN delivery_state TEXT NOT NULL DEFAULT '{}'")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _root(path: str) -> str:
    return str(Path(path).resolve(strict=True))


def _work(db, cwd: str, work_id: str):
    row = db.execute('SELECT * FROM works WHERE work_id=?', (work_id,)).fetchone()
    if row is None:
        raise ProbeError('not_found', 'work checkpoint not found', status='not_found')
    if row['cwd'] != cwd:
        raise ProbeError('workspace_drift', 'work belongs to a different physical checkout; select its project before execution', status='denied')
    return row


def _work_public(row) -> dict[str, Any]:
    value = dict(row)
    value['artifact_refs'] = json.loads(value.pop('artifacts'))
    raw_delivery = value.pop('delivery_state', '{}') or '{}'
    try:
        delivery = json.loads(raw_delivery)
    except (TypeError, ValueError):
        delivery = {}
    if isinstance(delivery, dict) and delivery:
        try:
            delivery, _changed = delivery_refresh_requirements(value['cwd'], delivery)
        except ProbeError:
            delivery = dict(delivery)
            delivery['product_status'] = 'unverified'
            delivery['requirements_readback_error'] = True
        value['delivery'] = delivery
        value['product_status'] = delivery.get('product_status', 'unverified')
    else:
        value['delivery'] = {}
        value['product_status'] = 'unverified' if value.get('status') == 'done' else 'not_configured'
    return value


def _delivery_evidence_exists(db, cwd: str, work_id: str, ref: str) -> bool:
    if not isinstance(ref, str):
        return False
    if ref.startswith('op_') and len(ref) == 27:
        row = db.execute(
            'SELECT state,work_id FROM operations WHERE operation_id=? AND cwd=?',
            (ref, cwd),
        ).fetchone()
        return bool(
            row is not None
            and row['state'] == 'completed'
            and row['work_id'] in (None, work_id)
        )
    return False


def task_work_context(cwd: str, work_id: str | None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    cwd = _root(cwd)
    if not work_id:
        return None, render_delivery_context(None)
    with _db() as db:
        work = _work_public(_work(db, cwd, work_id))
    return work, render_delivery_context(work)


def work_action(cwd: str, project: str, operation: str, body: dict[str, Any]) -> dict[str, Any]:
    cwd = _root(cwd)
    now = time.time()
    with _db() as db:
        db.execute('BEGIN IMMEDIATE')
        if operation == 'work_open':
            key = body['request_key']
            old = db.execute('SELECT * FROM works WHERE cwd=? AND request_key=?', (cwd, key)).fetchone()
            goal = _safe(body['goal'])
            if old is not None:
                if old['goal'] != goal or old['incident_id'] != body.get('incident_id'):
                    raise ProbeError('request_key_conflict', 'work request_key already identifies a different goal or incident', status='denied')
                if 'delivery_contract' in body:
                    public = _work_public(old)
                    if public.get('delivery', {}).get('contract') != body['delivery_contract']:
                        raise ProbeError(
                            'request_key_conflict',
                            'work request_key already identifies a work with a different delivery contract; update it through revision-checked work_checkpoint',
                            status='denied',
                        )
                return {
                    'status': 'ok',
                    'reused': True,
                    'work': _work_public(old),
                }
            wid = 'work_' + secrets.token_hex(12)
            from product_delivery import initial_state
            delivery = initial_state(cwd, body.get('delivery_contract'))
            db.execute(
                '''INSERT INTO works(
                   work_id,cwd,project,request_key,goal,next_step,incident_id,revision,status,artifacts,created,updated,delivery_state
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    wid, cwd, project, key, goal, _safe(body.get('next_step', '')),
                    body.get('incident_id'), 1, 'active', '[]', now, now, _json(delivery),
                ),
            )
            row = _work(db, cwd, wid)
            return {'status': 'ok', 'reused': False, 'work': _work_public(row)}

        row = _work(db, cwd, body['work_id'])
        if row['revision'] != body['expected_revision']:
            raise ProbeError('checkpoint_conflict', 'checkpoint changed; work_resume before updating it', status='denied')

        try:
            delivery = json.loads(row['delivery_state'] or '{}')
        except (TypeError, ValueError):
            delivery = {}
        material_changed = False
        if 'delivery_contract' in body:
            delivery, changed = delivery_apply_contract(cwd, delivery, body['delivery_contract'])
            material_changed = material_changed or changed
        if delivery:
            delivery, changed = delivery_refresh_requirements(cwd, delivery)
            material_changed = material_changed or changed
        if 'delivery_update' in body:
            if not delivery:
                raise ProbeError(
                    'delivery_contract_required',
                    'delivery_update requires an existing delivery_contract; bind the contract in this checkpoint first',
                    status='denied',
                )
            delivery, changed = delivery_apply_update(delivery, body.get('delivery_update'))
            material_changed = material_changed or changed

        requested_status = body.get('status', row['status'])
        if material_changed and row['status'] == 'done' and 'status' not in body:
            requested_status = 'active'

        if delivery:
            evidence_exists = lambda ref: _delivery_evidence_exists(db, cwd, row['work_id'], ref)
            delivery, _issues = delivery_assess(delivery, evidence_exists)
            if requested_status == 'done':
                delivery = delivery_validate_done(delivery, evidence_exists)
            else:
                delivery = delivery_observe(delivery)

        db.execute(
            'UPDATE works SET next_step=?,status=?,artifacts=?,delivery_state=?,revision=revision+1,updated=? WHERE work_id=?',
            (
                _safe(body['next_step']),
                requested_status,
                _json(_safe(body.get('artifact_refs', json.loads(row['artifacts'])))),
                _json(delivery),
                now,
                row['work_id'],
            ),
        )
        return {'status': 'ok', 'work': _work_public(_work(db, cwd, row['work_id']))}

def resolve_context(cwd: str, body: dict[str, Any]) -> dict[str, Any]:
    context = {key: body[key] for key in ('work_id', 'incident_id', 'request_key') if key in body}
    if context.get('work_id'):
        with _db() as db:
            work = _work(db, _root(cwd), context['work_id'])
            if context.get('incident_id') and context['incident_id'] != work['incident_id']:
                raise ProbeError('incident_conflict', 'explicit incident differs from the work checkpoint', status='denied')
            context['incident_id'] = work['incident_id']
    return context


def _claim(cwd: str, project: str, plane: str, operation: str, key: str, digest: str, context):
    with _db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM operations WHERE cwd=? AND request_key=?', (cwd, key)).fetchone()
        if row is not None:
            if row['spec_sha'] != digest:
                raise ProbeError('request_key_conflict', 'request_key already belongs to another operation or payload', status='denied')
            return dict(row), True
        oid = 'op_' + secrets.token_hex(12)
        db.execute('INSERT INTO operations(operation_id,cwd,project,plane,operation,request_key,spec_sha,work_id,incident_id,started,state,owner_pid) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (
            oid, cwd, project, plane, operation, key, digest, context.get('work_id'),
            context.get('incident_id'), time.time(), 'started', os.getpid()))
        return dict(db.execute('SELECT * FROM operations WHERE operation_id=?', (oid,)).fetchone()), False


def _finish(oid: str, result: Any, state: str, error_type: str | None = None):
    text = _json(_safe(result))
    if len(text.encode('utf-8')) > MAX_RESULT_BYTES:
        text = _json({'status': 'result_too_large', 'result_available': False,
                      'note': 'Operation finished; use its native job/artifact readback. Never repeat the mutation to recover output.'})
    with _db() as db:
        db.execute('UPDATE operations SET finished=?,state=?,result=?,error_type=? WHERE operation_id=?',
                   (time.time(), state, text, error_type, oid))


def _op_public(row, *, include_result=False):
    value = {key: row[key] for key in ('seq', 'operation_id', 'cwd', 'project', 'plane', 'operation',
             'request_key', 'work_id', 'incident_id', 'started', 'finished', 'state', 'error_type')}
    if row['result']:
        result = json.loads(row['result'])
        value['result_summary'] = {key: result[key] for key in (
            'status', 'exit_code', 'job_id', 'run_id', 'url', 'path', 'sha256', 'commit_sha', 'new_sha', 'action'
        ) if isinstance(result, dict) and key in result}
        if include_result:
            if len(row['result'].encode('utf-8')) <= READBACK_BYTES:
                value['result'] = result
            else:
                value['result_readback'] = {'operation': 'operation_result', 'operation_id': row['operation_id'], 'offset': 0}
    if row['state'] in {'started', 'outcome_unknown'}:
        value['next_action'] = 'Reconcile native job/service/Git state; this key will not re-execute the operation.'
    return value


def _receipt_time(row: dict[str, Any]) -> float:
    raw=row.get("observed_at")
    if not isinstance(raw,str): return 0.0
    try: return dt.datetime.fromisoformat(raw.replace("Z","+00:00")).timestamp()
    except (TypeError,ValueError): return 0.0
def _safety_trace(cwd: str, body: dict[str, Any]) -> dict[str, Any]:
    from direct_probe_core import DIRECT_RECEIPT_LOG
    history=read_receipts(DIRECT_RECEIPT_LOG,cwd,limit=int(body.get("limit",100))); cutoff=time.time()-int(body.get("window_seconds",3600))
    entries=[r for r in history.get("entries",[]) if _receipt_time(r)>=cutoff]; findings=[]
    def add(code,count,note): findings.append({"code":code,"count":count,"note":note})
    actions=[r for r in entries if str(r.get("plane") or "").endswith("_action")]
    if len(actions)>=10: add("mutation_burst",len(actions),"Many action calls occurred in one short window; prefer batch or one semantic operation.")
    failures=[r for r in entries if r.get("status") in {"denied","failed","error","outcome_unknown"}]
    if len(failures)>=3: add("failure_retry_pressure",len(failures),"Several denied/failed/unknown outcomes occurred; inspect recovery state before another side effect.")
    by={}
    for r in entries: by.setdefault(str(r.get("operation") or ""),[]).append(r)
    repeated=sum(1 for rows in by.values() if len(rows)>=2 and any(r.get("status")=="denied" for r in rows))
    if repeated: add("retry_after_denial",repeated,"One or more denied operation families were attempted again in the same window.")
    polling=sum(len(by.get(n,[])) for n in ("job_status","github_status","read_task"))
    if polling>=4: add("polling_burst",polling,"Repeated status polling can be replaced by job_wait/github_run_wait.")
    helpers=[r for r in entries if isinstance(r.get("subject"),str) and ("/.tmp" in r["subject"] or r["subject"].startswith(".tmp") or "/tmp_" in r["subject"])]
    if len(helpers)>=3: add("helper_script_churn",len(helpers),"Temporary helper-script churn is better expressed as text_edit or another semantic operation.")
    host=[r for r in actions if r.get("operation") in {"restart_service","service_action","owned_service_action","root_git_sync_exact"}]
    if len(host)>=3: add("host_mutation_burst",len(host),"Multiple host/runtime mutations occurred close together; prefer one verified rollout path.")
    return {"status":"ok","action":"safety_trace","advisory":True,"blocking":False,"window_seconds":int(body.get("window_seconds",3600)),"entries_considered":len(entries),"findings":findings,
        "recommended_next":"Use recovery_snapshot before another mutation after an interruption." if failures else "Prefer direct_ops_v2 semantic operations; capabilities and escape hatches remain unchanged.",
        "coverage":{"retained_since":history.get("retained_since"),"scan_complete":history.get("scan_complete"),"historical_before_retention":"unknown"}}
def _recovery_snapshot(cwd: str, body: dict[str, Any]) -> dict[str, Any]:
    limit=int(body.get("limit",10)); work_id=body.get("work_id")
    with _db() as db:
        args=[cwd]; clause="cwd=?"
        if work_id: _work(db,cwd,work_id); clause+=" AND work_id=?"; args.append(work_id)
        rows=db.execute("SELECT * FROM operations WHERE "+clause+" ORDER BY seq DESC LIMIT ?",(*args,limit)).fetchall()
        work_rows=db.execute("SELECT * FROM works WHERE cwd=? AND status IN ('active','waiting') ORDER BY updated DESC LIMIT 5",(cwd,)).fetchall()
    operations=[_op_public(r) for r in rows]; uncertain=[r for r in operations if r.get("state") in {"started","outcome_unknown"}]
    from direct_jobs import list_jobs
    jobs=list_jobs(Path(cwd),limit=min(limit,20)).get("jobs",[]); next_actions=[{"operation":"operation_get","operation_id":r["operation_id"]} for r in uncertain[:5]]
    if work_id: next_actions.append({"operation":"work_resume","work_id":work_id})
    elif work_rows: next_actions.append({"operation":"work_resume","work_id":work_rows[0]["work_id"]})
    for j in jobs[:3]:
        if j.get("status") in {"not_polled","running","starting"}: next_actions.append({"operation":"job_wait","job_id":j["job_id"]})
    return {"status":"ok","action":"recovery_snapshot","uncertain_operations":uncertain,"recent_operations":operations,"active_works":[_work_public(r) for r in work_rows],
        "candidate_jobs":jobs,"next_actions":next_actions,"safe_to_start_new_side_effect":not bool(uncertain),"note":"Read-only recovery guidance; no side effect is retried or reconciled automatically."}

def process_read(cwd: str, operation: str, body: dict[str, Any]) -> dict[str, Any]:
    cwd = _root(cwd)
    if operation == 'receipt_history':
        from direct_probe_core import DIRECT_RECEIPT_LOG
        return read_receipts(DIRECT_RECEIPT_LOG, cwd, limit=body.get('limit', 20), before=body.get('before'))
    if operation == 'safety_trace': return _safety_trace(cwd, body)
    if operation == 'recovery_snapshot': return _recovery_snapshot(cwd, body)
    with _db() as db:
        if operation == 'work_list':
            rows = db.execute('SELECT * FROM works WHERE cwd=? ORDER BY updated DESC LIMIT ?',
                              (cwd, body.get('limit', 10) + 1)).fetchall()
            n = body.get('limit', 10)
            return {'status': 'ok', 'works': [_work_public(r) for r in rows[:n]], 'has_more': len(rows) > n}
        if operation == 'work_resume':
            work = _work(db, cwd, body['work_id'])
            rows = db.execute('SELECT * FROM operations WHERE cwd=? AND work_id=? ORDER BY seq DESC LIMIT 21',
                              (cwd, body['work_id'])).fetchall()
            return {'status': 'ok', 'work': _work_public(work),
                    'operations': [_op_public(r) for r in rows[:20]], 'has_more': len(rows) > 20,
                    'next_cursor': rows[19]['seq'] if len(rows) > 20 else None,
                    'note': 'Read saved results before retrying. A transport failure is not proof the operation failed.'}
        if operation == 'operation_list':
            n = body.get('limit', 20)
            args: list[Any] = [cwd, body.get('before', 9223372036854775807)]
            clause = 'cwd=? AND seq<?'
            if body.get('work_id'):
                _work(db, cwd, body['work_id'])
                clause += ' AND work_id=?'
                args.append(body['work_id'])
            rows = db.execute('SELECT * FROM operations WHERE ' + clause + ' ORDER BY seq DESC LIMIT ?', (*args, n+1)).fetchall()
            return {'status': 'ok', 'operations': [_op_public(r) for r in rows[:n]],
                    'has_more': len(rows) > n, 'complete': len(rows) <= n,
                    'next_cursor': rows[n-1]['seq'] if len(rows) > n else None}
        row = db.execute('SELECT * FROM operations WHERE operation_id=? AND cwd=?', (body['operation_id'], cwd)).fetchone()
        if row is None:
            raise ProbeError('not_found', 'operation not found in this physical checkout', status='not_found')
        if operation == 'operation_get':
            return {'status': 'ok', 'operation': _op_public(row, include_result=True)}
        text = row['result'] or ''
        offset = body.get('offset', 0)
        end = min(len(text), offset + READBACK_BYTES)
        return {'status': 'ok', 'operation_id': row['operation_id'], 'text': text[offset:end],
                'next_offset': end if end < len(text) else None, 'complete': end >= len(text)}


async def recorded_call(project_root: str, project_id: str, plane: str, action: dict[str, Any],
                        call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Claim before effects; cache after completion; no lock is held across execution."""
    context = dict(CONTEXT.get())
    operation = action.get('kind', '')
    if operation in READ_ACTIONS or operation in {'incident_open', 'incident_note', 'incident_close'}:
        return await call()
    cwd = _root(project_root)
    key = context.get('request_key') or action.get('request_key') or ('auto:' + secrets.token_hex(16))
    if operation in {'job_start', 'github_workflow_dispatch'}:
        key = 'native:' + secrets.token_hex(16)  # Native operation owns idempotent reconciliation.
    digest = hashlib.sha256(_json({'plane': plane, 'action': action, 'work_id': context.get('work_id'),
                                  'incident_id': context.get('incident_id')}).encode()).hexdigest()
    try:
        row, reused = await asyncio.to_thread(_claim, cwd, project_id, plane, operation, key, digest, context)
    except (OSError, sqlite3.Error) as exc:
        if context.get('request_key') or context.get('work_id') or action.get('request_key'):
            raise ProbeError('recovery_storage', 'Cannot persist operation before execution; no action was started. Check disk/storage health.') from exc
        # Legacy operations keep their existing capabilities if optional observation is unavailable.
        result = await call()
        return {**result, 'recovery_warning': 'operation receipt storage unavailable'}
    if reused:
        if row['state'] == 'completed' and row['result']:
            return {**json.loads(row['result']), 'operation_id': row['operation_id'], 'replayed': True}
        return {'status': 'outcome_unknown' if row['state'] != 'failed' else 'failed',
                'operation_id': row['operation_id'], 'replayed': True, 'executed_again': False,
                'error_category': row['error_type'],
                'next_action': 'operation_get; reconcile native state before an intentional new request'}
    context['operation_id'] = row['operation_id']
    token = CONTEXT.set(context)
    LOG.info('operation_started operation_id=%s operation=%s project=%s', row['operation_id'], operation, project_id)
    try:
        result = await call()
        await asyncio.to_thread(_finish, row['operation_id'], result, 'completed')
        LOG.info('operation_finished operation_id=%s status=%s', row['operation_id'], result.get('status'))
        return {**result, 'operation_id': row['operation_id'], 'replayed': False}
    except BaseException as exc:
        state = 'failed' if isinstance(exc, ProbeError) and exc.status == 'denied' else 'outcome_unknown'
        category = exc.category if isinstance(exc, ProbeError) else type(exc).__name__
        try:
            await asyncio.shield(asyncio.to_thread(_finish, row['operation_id'],
                {'status': state, 'error_category': category}, state, category))
        except (OSError, sqlite3.Error, asyncio.CancelledError):
            pass
        if isinstance(exc, ProbeError):
            raise ProbeError(exc.category, f'{exc} [operation_id={row["operation_id"]}]',
                             status=exc.status, exit_code=exc.exit_code) from exc
        raise
    finally:
        CONTEXT.reset(token)


def context_fields() -> dict[str, Any]:
    return {key: value for key, value in CONTEXT.get().items()
            if key in {'work_id', 'incident_id', 'operation_id'} and value is not None}


def _schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


KEY = {'type': 'string', 'minLength': 1, 'maxLength': 128, 'pattern': r'^[A-Za-z0-9][A-Za-z0-9._:-]*$'}
WORK_ID = {'type': 'string', 'pattern': r'^work_[0-9a-f]{24}$'}
OP_ID = {'type': 'string', 'pattern': r'^op_[0-9a-f]{24}$'}
INCIDENT_ID = {'type': 'string', 'pattern': r'^inc_[0-9a-f]{24}$'}
TEXT = {'type': 'string', 'maxLength': 4000}
CRITERION_ID = {'type': 'string', 'minLength': 1, 'maxLength': 64, 'pattern': r'^[A-Za-z0-9][A-Za-z0-9._:-]*$'}
SCOPE = {'enum': ['analysis', 'document', 'source', 'unit', 'integration', 'live', 'owner']}
DELIVERY_CONTRACT_SCHEMA = _schema({
    'schema_version': {'const': 1},
    'kind': {'enum': ['feature_delivery', 'bugfix', 'investigation', 'documentation', 'maintenance']},
    'outcome': {**TEXT, 'minLength': 1},
    'target': {'enum': ['artifact_ready', 'source_ready', 'owner_testable', 'owner_accepted']},
    'scope_ref': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
    'requirement_refs': {'type': 'array', 'maxItems': 32, 'uniqueItems': True, 'items': CRITERION_ID},
    'criteria': {
        'type': 'array', 'minItems': 1, 'maxItems': 32,
        'items': _schema({
            'id': CRITERION_ID,
            'statement': {'type': 'string', 'minLength': 1, 'maxLength': 1200},
            'required_scope': SCOPE,
            'allow_agent_attested': {'type': 'boolean'},
        }, ('id', 'statement', 'required_scope')),
    },
}, ('schema_version', 'kind', 'outcome', 'target', 'scope_ref', 'criteria'))
DELIVERY_UPDATE_SCHEMA = _schema({
    'candidate': _schema({
        'source_ref': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        'build_ref': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        'artifact_ref': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        'deployment_ref': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        'fingerprint': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        'notes': {'type': 'string', 'maxLength': 2000},
    }),
    'criterion_results': {
        'type': 'array', 'maxItems': 64,
        'items': _schema({
            'criterion_id': CRITERION_ID,
            'status': {'enum': ['pass', 'fail', 'unverified']},
            'evidence_refs': {'type': 'array', 'maxItems': 16, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 2000}},
            'actual_scope': SCOPE,
            'verification': {'enum': ['server_verified', 'agent_attested']},
            'evaluated_by': {'enum': ['agent', 'test', 'server', 'owner']},
            'candidate_ref': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
            'note': {'type': 'string', 'maxLength': 2000},
        }, ('criterion_id', 'status')),
    },
    'blockers': {
        'type': 'array', 'maxItems': 32,
        'items': _schema({
            'criterion_id': CRITERION_ID,
            'reason': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
            'source': {'enum': ['requirement', 'required_check', 'authority', 'defect', 'uncertainty', 'advisory']},
            'blocking_scope': {'enum': ['criterion', 'target', 'all', 'none']},
            'evidence_ref': {'type': 'string', 'maxLength': 2000},
            'next_action': {'type': 'string', 'maxLength': 2000},
        }, ('reason', 'source', 'blocking_scope')),
    },
    'waiting': _schema({
        'kind': {'enum': ['none', 'external_job', 'provider', 'owner']},
        'ref': {'type': 'string', 'maxLength': 2000},
    }, ('kind',)),
    'handoff': _schema({
        'artifact_refs': {'type': 'array', 'maxItems': 32, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 2000}},
        'verify': {'type': 'string', 'maxLength': 2000},
        'limitations': {'type': 'array', 'maxItems': 16, 'items': {'type': 'string', 'maxLength': 1000}},
    }),
    'completion_outcome': {'enum': ['delivered', 'cancelled', 'superseded', 'failed']},
})
SCHEMAS = {
 'work_open': _schema({'goal': {**TEXT, 'minLength': 1}, 'request_key': KEY, 'next_step': TEXT,
                       'incident_id': INCIDENT_ID, 'delivery_contract': DELIVERY_CONTRACT_SCHEMA}, ('goal', 'request_key')),
 'work_checkpoint': _schema({'work_id': WORK_ID, 'expected_revision': {'type': 'integer', 'minimum': 1},
      'next_step': TEXT, 'status': {'enum': ['active', 'waiting', 'done']},
      'artifact_refs': {'type': 'array', 'maxItems': 32, 'items': {'type': 'string', 'maxLength': 1000}},
      'delivery_contract': DELIVERY_CONTRACT_SCHEMA, 'delivery_update': DELIVERY_UPDATE_SCHEMA},
      ('work_id', 'expected_revision', 'next_step')),
 'work_list': _schema({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}),
 'work_resume': _schema({'work_id': WORK_ID}, ('work_id',)),
 'operation_get': _schema({'operation_id': OP_ID}, ('operation_id',)),
 'operation_result': _schema({'operation_id': OP_ID, 'offset': {'type': 'integer', 'minimum': 0}}, ('operation_id',)),
 'operation_list': _schema({'work_id': WORK_ID, 'before': {'type': 'integer', 'minimum': 1},
                           'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}),
}

from process_support import validation_message, bounded_snapshot, append_receipt, read_receipts

PROCESS_READS.add('receipt_history')
SCHEMAS['receipt_history'] = _schema({'limit': {'type':'integer','minimum':1,'maximum':100}, 'before':{'type':'string','maxLength':256}})
SCHEMAS['safety_trace'] = _schema({'limit': {'type':'integer','minimum':10,'maximum':100}, 'window_seconds': {'type':'integer','minimum':60,'maximum':86400}})
SCHEMAS['recovery_snapshot'] = _schema({'work_id': WORK_ID, 'limit': {'type':'integer','minimum':1,'maximum':20}})
