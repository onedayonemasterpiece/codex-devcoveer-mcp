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
    assert described['operations']==['work_open']
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
    assert {'run_tool','job_start','git_commit_push','apply_source_patch','artifact_fetch'} <= set(desc['operations'])
    schema=gateway.describe_plane('project_action','run_tool')['payload_schema']['properties']
    assert {'bash_script','python_script'} <= set(schema['tool']['enum'])
    assert 'expected_head' in schema and 'expected_workspace_digest' in schema
