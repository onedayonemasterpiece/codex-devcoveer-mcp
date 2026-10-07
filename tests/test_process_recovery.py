from __future__ import annotations
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from jsonschema import Draft202012Validator
import process_recovery as recovery
import process_support as support
import direct_ops_gateway as gateway
import direct_actions as actions
import direct_probe_core as core


def run(coro):
    return asyncio.run(coro)


def work(tmp_path, key='test:work', incident=None):
    body={'goal':'Finish the existing product task','request_key':key,'next_step':'Verify the change'}
    if incident:
        body['incident_id']=incident
    return recovery.work_action(str(tmp_path), 'fixture', 'work_open', body)['work']


def test_lost_successful_response_is_recovered_after_storage_reopen(tmp_path):
    calls=[]
    async def mutate():
        calls.append('side-effect')
        return {'status':'ok','exit_code':0,'path':'result.txt'}
    async def scenario():
        token=recovery.CONTEXT.set({'request_key':'lost-response:one'})
        try:
            first=await recovery.recorded_call(str(tmp_path),'fixture','project_action',{'kind':'write_text'},mutate)
            # The first caller loses its response. A new call opens a new SQLite connection.
            second=await recovery.recorded_call(str(tmp_path),'fixture','project_action',{'kind':'write_text'},mutate)
            assert first['operation_id']==second['operation_id']
            assert second['replayed'] is True
            assert second['exit_code']==0
        finally:
            recovery.CONTEXT.reset(token)
    run(scenario())
    assert calls==['side-effect']


def test_request_key_conflict_does_not_execute_second_action(tmp_path):
    calls=[]
    async def mutate():
        calls.append(1); return {'status':'ok'}
    async def scenario():
        token=recovery.CONTEXT.set({'request_key':'one'})
        try:
            await recovery.recorded_call(str(tmp_path),'p','project_action',{'kind':'write_text','path':'a'},mutate)
            with pytest.raises(core.ProbeError, match='request_key'):
                await recovery.recorded_call(str(tmp_path),'p','project_action',{'kind':'write_text','path':'b'},mutate)
        finally: recovery.CONTEXT.reset(token)
    run(scenario()); assert len(calls)==1


def test_concurrent_retry_does_not_duplicate_mutation(tmp_path):
    calls=[]
    async def scenario():
        entered=asyncio.Event(); release=asyncio.Event()
        async def mutate():
            calls.append(1); entered.set(); await release.wait(); return {'status':'ok'}
        token=recovery.CONTEXT.set({'request_key':'concurrent:one'})
        try:
            first=asyncio.create_task(recovery.recorded_call(str(tmp_path),'p','project_action',{'kind':'write_text'},mutate))
            await entered.wait()
            second=await recovery.recorded_call(str(tmp_path),'p','project_action',{'kind':'write_text'},mutate)
            assert second['executed_again'] is False
            release.set(); await first
        finally: recovery.CONTEXT.reset(token)
    run(scenario()); assert len(calls)==1


def test_unknown_outcome_is_not_reexecuted(tmp_path):
    calls=[]
    async def mutate():
        calls.append(1); raise RuntimeError('simulated lost process response')
    async def scenario():
        token=recovery.CONTEXT.set({'request_key':'unknown:one'})
        try:
            with pytest.raises(RuntimeError):
                await recovery.recorded_call(str(tmp_path),'p','host_action',{'kind':'service_action'},mutate)
            replay=await recovery.recorded_call(str(tmp_path),'p','host_action',{'kind':'service_action'},mutate)
            assert replay['status']=='outcome_unknown' and replay['executed_again'] is False
        finally: recovery.CONTEXT.reset(token)
    run(scenario()); assert len(calls)==1


def test_checkpoint_resume_and_revision_conflict(tmp_path):
    w=work(tmp_path)
    updated=recovery.work_action(str(tmp_path),'p','work_checkpoint',{
        'work_id':w['work_id'],'expected_revision':1,'next_step':'Publish verified build',
        'artifact_refs':['artifact:verified-build'],'status':'waiting'})
    assert updated['work']['revision']==2
    with pytest.raises(core.ProbeError, match='changed'):
        recovery.work_action(str(tmp_path),'p','work_checkpoint',{
            'work_id':w['work_id'],'expected_revision':1,'next_step':'Stale change'})
    resumed=recovery.process_read(str(tmp_path),'work_resume',{'work_id':w['work_id']})
    assert resumed['work']['next_step']=='Publish verified build'
    assert resumed['work']['artifact_refs']==['artifact:verified-build']


def test_work_and_incident_context_do_not_mix(tmp_path):
    a=work(tmp_path,'work:a','inc_'+'a'*24)
    b=work(tmp_path,'work:b','inc_'+'b'*24)
    assert recovery.resolve_context(str(tmp_path),{'work_id':a['work_id']})['incident_id']=='inc_'+'a'*24
    with pytest.raises(core.ProbeError, match='differs'):
        recovery.resolve_context(str(tmp_path),{'work_id':a['work_id'],'incident_id':'inc_'+'b'*24})
    other=tmp_path/'other'; other.mkdir()
    with pytest.raises(core.ProbeError, match='different physical'):
        recovery.process_read(str(other),'work_resume',{'work_id':a['work_id']})
    assert b['work_id'] != a['work_id']


def test_resume_includes_saved_operation_without_extra_execution(tmp_path):
    w=work(tmp_path)
    async def scenario():
        token=recovery.CONTEXT.set({'work_id':w['work_id'],'request_key':'step:one'})
        try:
            return await recovery.recorded_call(str(tmp_path),'p','project_action',{'kind':'write_text'},
                lambda: asyncio.sleep(0,result={'status':'ok','job_id':'job_'+'a'*24}))
        finally: recovery.CONTEXT.reset(token)
    result=run(scenario())
    state=recovery.process_read(str(tmp_path),'work_resume',{'work_id':w['work_id']})
    assert state['operations'][0]['operation_id']==result['operation_id']
    assert state['operations'][0]['result_summary']['job_id']=='job_'+'a'*24


def test_results_are_paginated_without_printing_transcripts(tmp_path):
    async def scenario():
        return await recovery.recorded_call(str(tmp_path),'p','project_action',{'kind':'run_tool'},
            lambda: asyncio.sleep(0,result={'status':'ok','stdout':'x'*40000}))
    result=run(scenario())
    meta=recovery.process_read(str(tmp_path),'operation_get',{'operation_id':result['operation_id']})
    assert 'result_readback' in meta['operation']
    first=recovery.process_read(str(tmp_path),'operation_result',{'operation_id':result['operation_id']})
    assert first['next_offset'] is not None and len(first['text'])<=16000


def test_receipt_rotation_preserves_history_and_cursor(tmp_path):
    path=tmp_path/'receipts.jsonl'
    for n in range(30):
        support.append_receipt(path,{'cwd':str(tmp_path),'operation':'git_state',
            'observed_at':f'2026-09-30T10:00:{n:02d}+00:00'},300)
    assert list(tmp_path.glob('receipts.jsonl.*.archive'))
    found=[]; cursor=None
    while True:
        page=support.read_receipts(path,str(tmp_path),limit=7,before=cursor)
        found.extend(page['entries'])
        if not page['has_more']: break
        assert page['complete'] is False
        cursor=page['next_cursor']
    assert len(found)==30
    assert len({x['receipt_id'] for x in found})==30
    assert page['coverage_before_retained_since']=='unknown'


def test_field_validation_does_not_echo_secret_payload():
    schema={'oneOf':[{'type':'object','properties':{'kind':{'const':'job_status'},
        'tail_lines':{'type':'integer','maximum':200}},'required':['kind'],'additionalProperties':False}]}
    error=next(Draft202012Validator(schema).iter_errors({'kind':'job_status','tail_lines':999,'secret':'do-not-echo'}))
    message=support.validation_message(error)
    assert 'do-not-echo' not in message and len(message)<300


def test_full_snapshot_is_bounded_and_omits_reasoning():
    messages=[{'info':{'id':str(i),'role':'assistant'},'parts':[
        {'type':'reasoning','text':'private'*10000},
        {'type':'tool','tool':'read','state':{'status':'completed','input':{'huge':'x'*100000},'output':'x'*100000}}
    ]} for i in range(30)]
    result,truncated=support.bounded_snapshot(messages)
    assert truncated and len(json.dumps(result).encode())<16000
    assert 'reasoning' not in json.dumps(result) and 'input' not in json.dumps(result)


def test_gateway_schema_exposes_recovery_without_new_top_level_tool():
    described=gateway.describe_plane('project_action','work_open')
    assert described['operation']=='work_open'
    assert 'work_open' in described['operations']
    assert {'git_fetch','github_workflow_dispatch','job_start','run_tool'} <= set(described['operations'])
    assert 'gateway_contract_digest' in described
    mutation=gateway.describe_plane('project_action','write_text')['payload_schema']['properties']
    assert {'work_id','incident_id','request_key'} <= set(mutation)
    assert 'expected_head' in mutation and 'expected_workspace_digest' in mutation


def test_gateway_keeps_native_validation_before_mutation(monkeypatch,tmp_path):
    calls=[]
    async def fake(*args,**kwargs):
        calls.append(1); return {'status':'ok'}
    monkeypatch.setattr(gateway,'run_project_action',fake)
    with pytest.raises(core.ProbeError):
        run(gateway.run_direct_ops_v2(str(tmp_path),'p','project_action','write_text',{
            'path':'a','content':'ok','unexpected':'not-authorized','request_key':'one'}))
    assert calls==[]


def test_long_gateway_command_uses_existing_durable_job(monkeypatch,tmp_path):
    seen=[]
    async def fake(root,project,action,**kw):
        seen.append(action); return {'status':'running','job_id':'job_'+'a'*24}
    monkeypatch.setattr(gateway,'run_project_action',fake)
    result=run(gateway.run_direct_ops_v2(str(tmp_path),'p','project_action','run_tool',{
        'tool':'pytest','args':['-q'],'timeout_seconds':300,'request_key':'long:one'}))
    assert seen[0]['kind']=='job_start' and seen[0]['request_key']=='long:one'
    assert result['status']=='running'
    run(gateway.run_direct_ops_v2(str(tmp_path),'p','project_action','run_tool',{
        'tool':'pytest','args':['--version'],'timeout_seconds':300,'execution_mode':'sync'}))
    assert seen[1]['kind']=='run_tool'


def test_legacy_capabilities_and_guards_remain():
    desc=gateway.describe_plane('project_action')
    assert {'run_tool','job_start','git_commit_push','git_push_existing','apply_source_patch','artifact_fetch'} <= set(desc['operations'])
    schema=gateway.describe_plane('project_action','run_tool')['payload_schema']['properties']
    assert {'bash_script','python_script'} <= set(schema['tool']['enum'])
    assert 'expected_head' in schema and 'expected_workspace_digest' in schema


def _delivery_contract(target='owner_testable', required_scope='integration'):
    return {
        'schema_version': 1,
        'kind': 'feature_delivery',
        'outcome': 'Owner can verify the current candidate',
        'target': target,
        'scope_ref': 'owner-task:test',
        'requirement_refs': [],
        'criteria': [{
            'id': 'D1',
            'statement': 'Current candidate satisfies the requested behavior',
            'required_scope': required_scope,
        }],
    }


def test_old_process_recovery_database_is_migrated_in_place(tmp_path, monkeypatch):
    import sqlite3
    db_path = tmp_path / 'legacy.sqlite3'
    monkeypatch.setattr(recovery, 'DB_PATH', db_path)
    cwd = tmp_path / 'project'
    cwd.mkdir()
    now = 1.0
    db = sqlite3.connect(db_path)
    db.execute('''CREATE TABLE works (
      work_id TEXT PRIMARY KEY, cwd TEXT NOT NULL, project TEXT NOT NULL,
      request_key TEXT NOT NULL, goal TEXT NOT NULL, next_step TEXT NOT NULL,
      incident_id TEXT, revision INTEGER NOT NULL, status TEXT NOT NULL,
      artifacts TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
      UNIQUE(cwd, request_key))''')
    wid = 'work_' + 'a' * 24
    db.execute(
        'INSERT INTO works VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (wid, str(cwd.resolve()), 'fixture', 'legacy:key', 'Old task', 'Finished', None, 1, 'done', '[]', now, now),
    )
    db.commit()
    db.close()

    resumed = recovery.process_read(str(cwd), 'work_resume', {'work_id': wid})
    assert resumed['work']['status'] == 'done'
    assert resumed['work']['delivery'] == {}
    assert resumed['work']['product_status'] == 'unverified'

    opened = recovery.work_action(str(cwd), 'fixture', 'work_open', {
        'goal': 'New task', 'request_key': 'new:key',
    })
    assert opened['work']['revision'] == 1
    with sqlite3.connect(db_path) as reopened:
        columns = {row[1] for row in reopened.execute('PRAGMA table_info(works)')}
        assert 'delivery_state' in columns


def test_delivery_done_is_atomic_and_requires_current_evidence_and_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, 'DB_PATH', tmp_path / 'delivery.sqlite3')
    opened = recovery.work_action(str(tmp_path), 'fixture', 'work_open', {
        'goal': 'Ship the feature',
        'request_key': 'delivery:atomic',
        'delivery_contract': _delivery_contract(),
    })['work']
    assert opened['delivery']['policy_version'] == 'product-delivery-policy-v1'

    with pytest.raises(core.ProbeError, match='Cannot mark delivery work done'):
        recovery.work_action(str(tmp_path), 'fixture', 'work_checkpoint', {
            'work_id': opened['work_id'],
            'expected_revision': 1,
            'next_step': 'Hand off',
            'status': 'done',
            'delivery_update': {
                'candidate': {'deployment_ref': 'deploy:test'},
                'criterion_results': [{
                    'criterion_id': 'D1',
                    'status': 'pass',
                    'evidence_refs': ['external:integration-pass'],
                    'actual_scope': 'integration',
                    'verification': 'agent_attested',
                    'evaluated_by': 'agent',
                    'candidate_ref': 'deploy:test',
                }],
            },
        })
    still = recovery.process_read(str(tmp_path), 'work_resume', {'work_id': opened['work_id']})
    assert still['work']['revision'] == 1
    assert still['work']['status'] == 'active'

    completed = recovery.work_action(str(tmp_path), 'fixture', 'work_checkpoint', {
        'work_id': opened['work_id'],
        'expected_revision': 1,
        'next_step': 'Delivered',
        'status': 'done',
        'delivery_update': {
            'candidate': {'deployment_ref': 'deploy:test'},
            'criterion_results': [{
                'criterion_id': 'D1',
                'status': 'pass',
                'evidence_refs': ['external:integration-pass'],
                'actual_scope': 'integration',
                'verification': 'agent_attested',
                'evaluated_by': 'agent',
                'candidate_ref': 'deploy:test',
            }],
            'handoff': {
                'artifact_refs': ['deploy:test'],
                'verify': 'Open the deployed owner path and exercise the requested behavior.',
                'limitations': ['Owner acceptance is not inferred from delivery.'],
            },
        },
    })['work']
    assert completed['status'] == 'done'
    assert completed['delivery']['product_status'] == 'ready'
    assert completed['delivery']['completion_outcome'] == 'delivered'


def test_server_verified_evidence_must_exist_on_same_work(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, 'DB_PATH', tmp_path / 'server-evidence.sqlite3')
    opened = recovery.work_action(str(tmp_path), 'fixture', 'work_open', {
        'goal': 'Ship',
        'request_key': 'delivery:server',
        'delivery_contract': _delivery_contract(target='source_ready', required_scope='unit'),
    })['work']
    missing = 'op_' + 'f' * 24
    with pytest.raises(core.ProbeError, match='unavailable server evidence'):
        recovery.work_action(str(tmp_path), 'fixture', 'work_checkpoint', {
            'work_id': opened['work_id'], 'expected_revision': 1,
            'next_step': 'Done', 'status': 'done',
            'delivery_update': {
                'candidate': {'source_ref': 'commit:test'},
                'criterion_results': [{
                    'criterion_id': 'D1', 'status': 'pass',
                    'evidence_refs': [missing], 'actual_scope': 'unit',
                    'verification': 'server_verified', 'evaluated_by': 'test',
                    'candidate_ref': 'commit:test',
                }],
            },
        })


def test_contract_replay_conflict_and_legacy_checkpoint_cannot_erase_it(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, 'DB_PATH', tmp_path / 'replay.sqlite3')
    body = {
        'goal': 'Ship',
        'request_key': 'delivery:replay',
        'delivery_contract': _delivery_contract(target='source_ready', required_scope='source'),
    }
    opened = recovery.work_action(str(tmp_path), 'fixture', 'work_open', body)['work']
    replay = recovery.work_action(str(tmp_path), 'fixture', 'work_open', body)
    assert replay['reused'] is True
    changed = dict(body)
    changed['delivery_contract'] = _delivery_contract(target='owner_testable', required_scope='integration')
    with pytest.raises(core.ProbeError, match='different delivery contract'):
        recovery.work_action(str(tmp_path), 'fixture', 'work_open', changed)

    checkpoint = recovery.work_action(str(tmp_path), 'fixture', 'work_checkpoint', {
        'work_id': opened['work_id'], 'expected_revision': 1,
        'next_step': 'Keep working',
    })['work']
    assert checkpoint['delivery']['contract']['target'] == 'source_ready'


def test_delivery_stall_signal_is_nonblocking(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, 'DB_PATH', tmp_path / 'stall.sqlite3')
    current = recovery.work_action(str(tmp_path), 'fixture', 'work_open', {
        'goal': 'Ship',
        'request_key': 'delivery:stall',
        'delivery_contract': _delivery_contract(target='source_ready', required_scope='unit'),
    })['work']
    for _ in range(3):
        current = recovery.work_action(str(tmp_path), 'fixture', 'work_checkpoint', {
            'work_id': current['work_id'],
            'expected_revision': current['revision'],
            'next_step': 'Run the one required check',
        })['work']
    signals = current['delivery']['advisories']
    assert any(row['code'] == 'delivery_stalled' and row['blocking'] is False for row in signals)
    assert current['status'] == 'active'


def test_gateway_returns_compact_delivery_context(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, 'DB_PATH', tmp_path / 'gateway-delivery.sqlite3')
    result = run(gateway.run_direct_ops_v2(str(tmp_path), 'fixture', 'project_action', 'work_open', {
        'goal': 'Ship',
        'request_key': 'delivery:gateway',
        'delivery_contract': _delivery_contract(target='source_ready', required_scope='unit'),
    }))
    assert result['deliveryPolicyVersion'] == 'product-delivery-policy-v1'
    assert result['deliveryContext']['configured'] is True
    assert result['deliveryContext']['target'] == 'source_ready'
    described = gateway.describe_plane('project_action', 'work_checkpoint')
    assert described['deliveryPolicy']['version'] == 'product-delivery-policy-v1'
    assert described['deliveryPolicy']['blockingAdvisories'] is False
