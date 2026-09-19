# Included by the canonical runtime builder, after the base task reader.
_BASE_READ_OPENCODE_TASK = _read_opencode_task
_EVIDENCE_WATCHERS: dict[str, asyncio.Task[Any]] = {}
_EVIDENCE_READ_LOCKS: dict[str, asyncio.Lock] = {}


async def _read_opencode_task(record: dict[str, Any], *, detail: str = 'summary') -> dict[str, Any]:
    lock = _EVIDENCE_READ_LOCKS.setdefault(record['taskId'], asyncio.Lock())
    async with lock:
        return await _read_opencode_task_locked(record, detail=detail)


async def _read_opencode_task_locked(record: dict[str, Any], *, detail: str = 'summary') -> dict[str, Any]:
    payload = dict(await _BASE_READ_OPENCODE_TASK(record, detail='full'))
    messages = payload.pop('history', [])
    payload = _redact_evidence(payload)
    status = payload['status']
    current = TASK_REGISTRY.get(record['taskId']) or record
    category = payload.get('latestTurn', {}).get('providerErrorCategory')
    if (current.get('routingReason') == 'adaptive_opencode_default'
        and category and (status == 'failed' or payload.get('latestTurn', {}).get('providerState', {}).get('type') == 'retry')):
        attempted = list(dict.fromkeys([*current.get('attemptedSelections', []), current['selection']]))
        catalog = await OPENCODE_BACKEND.catalog(directory=str(record['cwd']))
        eligible = [m for m in _routing_candidates(catalog['models'], attempted)
                    if not _known_model_exclusion(m['selection'])]
        if eligible and len(attempted) < 16:
            selected = eligible[0]
            if status == 'running':
                await OPENCODE_BACKEND.cancel(session_id=record['backendSessionId'], directory=str(record['cwd']))
                # Require the abort to settle before submitting another prompt.
                for _ in range(20):
                    states = await OPENCODE_BACKEND.request('GET', '/session/status', directory=str(record['cwd']))
                    if states.get(record['backendSessionId'], {}).get('type') not in {'busy', 'retry'}: break
                    await asyncio.sleep(0.25)
                else:
                    return payload
            await OPENCODE_BACKEND.prompt(session_id=record['backendSessionId'], directory=str(record['cwd']),
                prompt='Continue the existing user request after a provider availability failure. Preserve prior work and verify tool outcomes before repeating actions.',
                provider=selected['provider'], model_id=selected['nativeModelId'], access=record['access'])
            transition = {'from': current['selection'], 'to': selected['selection'], 'reason': category}
            updated = TASK_REGISTRY.update(record['taskId'], status='running', executionStatus='running',
                provider=selected['provider'], model=selected['nativeModelId'], selection=selected['selection'],
                attemptedSelections=attempted, routingTransitions=[*current.get('routingTransitions', []), transition],
                acceptanceStatus='not_ready', sessionEvidence={}, finishedAt=None, errorCategory=None)
            return {'status': 'running', 'task': _registry_public(updated), 'executionStatus': 'running',
                    'acceptanceStatus': 'not_ready', 'sessionEvidenceAvailable': False,
                    'routingTransition': transition, 'content': 'Continued the same session on the next available provider.'}
    if status in {'completed', 'failed', 'interrupted'} and messages:
        # A partial history must never be described or accepted as complete evidence.
        complete = await OPENCODE_BACKEND.request('GET',
            f"/session/{record['backendSessionId']}/message", directory=str(record['cwd']))
        if not isinstance(complete, list):
            raise ValueError('invalid complete session history')
        index = _capture_evidence(current, complete, status)
        old = current.get('sessionEvidence', {})
        acceptance = current.get('acceptanceStatus', 'pending_review') if old.get('evidenceSha256') == index['evidenceSha256'] else 'pending_review'
        current = TASK_REGISTRY.update(record['taskId'], executionStatus=status,
            acceptanceStatus=acceptance, sessionEvidence=index)
        payload.update(executionStatus=status, acceptanceStatus=acceptance,
            sessionEvidenceAvailable=True, evidenceSha256=index['evidenceSha256'])
        payload['task'] = {**_registry_public(current), 'executionStatus': status,
            'acceptanceStatus': acceptance, 'sessionEvidenceAvailable': True}
    else:
        payload.update(executionStatus=status, acceptanceStatus='not_ready', sessionEvidenceAvailable=False)
        if detail == 'full':
            payload['liveSessionSnapshot'] = _redact_evidence(messages)
    if detail == 'full' and status != 'running':
        payload['historyNote'] = 'Use get_task_evidence(scope=full_safe) for the complete redacted session.'
    return payload


def _watch_opencode_evidence(task_id: str) -> None:
    existing = _EVIDENCE_WATCHERS.get(task_id)
    if existing is not None and not existing.done():
        return
    async def watch() -> None:
        while True:
            await asyncio.sleep(3)
            record = TASK_REGISTRY.get(task_id)
            if record is None: return
            try:
                payload = await _read_opencode_task(record)
                if payload.get('sessionEvidenceAvailable'): return
            except Exception:
                log.warning('Evidence capture deferred for %s', task_id)
            if record.get('status') not in {'running', 'cancelling', 'completed', 'failed', 'interrupted'}:
                return
    _EVIDENCE_WATCHERS[task_id] = asyncio.create_task(watch(), name=f'evidence-{task_id}')


async def _evidence_tool(name: str, arguments: dict, project: ProjectEntry | None) -> dict:
    record = _registry_task(arguments['task'], project)
    if record is None:
        candidates = TASK_REGISTRY.list(project=project.canonical if project else None, limit=5000)
        candidates = [r for r in candidates if r.get('backend') == 'opencode']
        hint = arguments['task'].casefold()
        if hint == 'latest':
            candidates = sorted(candidates, key=lambda r: r['createdAt'], reverse=True)[:1]
        else:
            candidates = [r for r in candidates if hint in r.get('title', '').casefold()]
        if len(candidates) != 1:
            return {'status': 'not_found' if not candidates else 'ambiguous', 'content': 'Use an exact OpenCode task ID.'}
        record = candidates[0]
    if record.get('backend') != 'opencode':
        return {'status': 'unsupported', 'content': 'Evidence is available for OpenCode tasks.'}
    payload = await _read_opencode_task(record)
    if not payload.get('sessionEvidenceAvailable'):
        return {'status': 'not_ready', 'content': 'Terminal session evidence is not available yet.'}
    record = TASK_REGISTRY.get(record['taskId'])
    index = record['sessionEvidence']
    document = _read_evidence(record['taskId'], index['evidenceSha256'])
    if name == 'record_task_review':
        if arguments['evidence_sha256'] != index['evidenceSha256']:
            return {'status': 'conflict', 'content': 'Evidence digest is stale; review the current evidence.'}
        review = {'verdict': arguments['verdict'], 'evidenceSha256': index['evidenceSha256'],
                  'findings': _redact_evidence(arguments.get('findings', [])), 'reviewedAt': int(time.time() * 1000)}
        TASK_REGISTRY.update(record['taskId'], acceptanceStatus=arguments['verdict'],
            review=review, reviews=[*record.get('reviews', []), review][-100:])
        return {'status': 'ok', 'taskId': record['taskId'], 'acceptanceStatus': arguments['verdict'],
                'executionStatus': record['executionStatus'], 'evidenceSha256': index['evidenceSha256']}
    result = {'status': 'ok', 'taskId': record['taskId'], **index,
              'executionStatus': record['executionStatus'], 'acceptanceStatus': record['acceptanceStatus']}
    if arguments.get('scope') == 'full_safe': result['session'] = document
    if arguments.get('scope') == 'suspicious':
        result['findings'] = ['Potential secrets were redacted.'] if index['redacted'] else []
    return result
