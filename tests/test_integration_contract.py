from __future__ import annotations
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime'))
# Load the source-controlled baseline backend used by the builder.
spec = importlib.util.spec_from_file_location('opencode_backend', ROOT/'historical/3.6.1/source/opencode_backend.py')
backend = importlib.util.module_from_spec(spec)
sys.modules['opencode_backend'] = backend
spec.loader.exec_module(backend)
from opencode_routing import candidates, classify, RoutingBackend
from model_health import ModelHealthStore
from session_evidence import capture, read, redact


def model(provider, name='model', **fields):
    return dict(provider=provider, nativeModelId=name, selection=provider+'/'+name,
                backend='opencode', capabilities={'toolcall': True}, free=True, **fields)


def test_priority_and_groq_pool_and_admission():
    items = [model('gigachat'), model('nvidia'), model('groq3','openai/gpt-oss-120b'),
             model('groq','openai/gpt-oss-120b'), model('opencode','nemotron-3-ultra-free'),
             model('groq2','openai/gpt-oss-120b'), model('groq','openai/gpt-oss-20b')]
    assert [m['provider'] for m in candidates(items)] == ['opencode','groq','groq2','groq3','nvidia','gigachat']
    items[4]['free'] = False
    items[3]['routingRecommended'] = False
    assert [m['provider'] for m in candidates(items)] == ['groq2','groq3','nvidia','gigachat']
    items[1]['capabilities']['toolcall'] = False
    assert 'nvidia' not in [m['provider'] for m in candidates(items)]


def test_provider_quota_and_endpoint_health(monkeypatch, tmp_path):
    b = RoutingBackend.__new__(RoutingBackend)
    b.health_store = ModelHealthStore(tmp_path/'health')
    items = [model('opencode','a'),model('opencode','b'),model('nvidia','a'),model('nvidia','b')]
    monkeypatch.setattr(backend.OpenCodeBackend, 'catalog', AsyncMock(return_value={'models':items, 'connected':['opencode','nvidia']}))
    b.health_store.record(provider='opencode',selection='opencode/a',outcome='failure',category='quota_exhausted')
    b.health_store.record(provider='nvidia',selection='nvidia/a',outcome='failure',category='rate_limited')
    result = asyncio.run(b.catalog())
    assert [m['selection'] for m in candidates(result['models'])] == ['nvidia/b']


def test_new_turn_does_not_reuse_old_success_or_error(monkeypatch, tmp_path):
    b = RoutingBackend.__new__(RoutingBackend); b.health_store = ModelHealthStore(tmp_path/'health')
    old = {'info':{'role':'assistant','error':{'message':'429'}}, 'parts':[{'type':'text','text':'old answer'}]}
    monkeypatch.setattr(backend.OpenCodeBackend, 'inspect', AsyncMock(return_value={'messages':[old,{'info':{'role':'user'},'parts':[]}]}))
    b.request = AsyncMock(return_value={})
    result = asyncio.run(b.inspect(session_id='ses_test',directory='/repo'))
    assert result['status'] == 'idle'
    assert result['finalResponse'] is None and result['error'] is None


def test_evidence_redaction_digest_and_tamper(tmp_path):
    task = {'taskId':'dvt_'+'a'*32,'backendSessionId':'ses_test'}
    messages=[{'info':{'role':'assistant'},'parts':[{'type':'text','text':'API_KEY=secret-123 Bearer abcdef123456789'}, {'type':'reasoning','text':'private reasoning'}]}]
    index = capture(task,messages,'completed',tmp_path/'evidence')
    doc = read(task['taskId'],index['evidenceSha256'],tmp_path/'evidence')
    encoded=json.dumps(doc)
    assert 'secret-123' not in encoded and 'abcdef123456789' not in encoded and 'private reasoning' not in encoded
    assert index == capture(task,messages,'completed',tmp_path/'evidence')
    path=tmp_path/'evidence'/task['taskId']/(index['evidenceSha256']+'.json')
    path.write_text('{}')
    with pytest.raises(ValueError,match='integrity'): read(task['taskId'],index['evidenceSha256'],tmp_path/'evidence')


def fragment_env(tmp_path):
    record={'taskId':'dvt_'+'a'*32,'backendSessionId':'ses_test','cwd':'/repo','backend':'opencode','project':'repo','status':'completed', 'provider':'opencode', 'model':'a','selection':'opencode/a','access':'read','routingReason':'adaptive_opencode_default'}
    registry=SimpleNamespace(get=lambda _:record, update=lambda _,**changes: (record.update(changes) or record))
    fake=SimpleNamespace(request=AsyncMock(return_value=[{'info':{'role':'assistant'},'parts':[{'type':'text','text':'OK'}]}]),catalog=AsyncMock(return_value={'models':[model('groq','openai/gpt-oss-120b'),model('nvidia'),model('gigachat')]}),prompt=AsyncMock(),cancel=AsyncMock())
    base=AsyncMock(return_value={'status':'completed','history':[{}],'latestTurn':{},'task':record.copy()})
    env={'Any':object,'asyncio':asyncio,'time':__import__('time'),'_read_opencode_task':base,
         'TASK_REGISTRY':registry,'OPENCODE_BACKEND':fake,'_registry_public':lambda r:r.copy(),
         '_redact_evidence':redact,'_capture_evidence':lambda t,m,s:capture(t,m,s,tmp_path/'evidence'),
         '_read_evidence':lambda t,s:read(t,s,tmp_path/'evidence'),'_routing_candidates':candidates,
         '_known_model_exclusion':lambda s:None,'_registry_task':lambda t,p:record,'ProjectEntry':object}
    exec(compile((ROOT/'runtime/bridge_fragments/session_evidence.py').read_text(),'<fragment>','exec'),env)
    return env,record,base,fake


def test_acceptance_digest_and_continuation_invalidation(tmp_path):
    env,record,base,fake=fragment_env(tmp_path)
    async def run():
        result=await env['_read_opencode_task'](record)
        assert result['acceptanceStatus']=='pending_review'
        args={'task':record['taskId'],'evidence_sha256':'0'*64,'verdict':'accepted'}
        assert (await env['_evidence_tool']('record_task_review',args,None))['status']=='conflict'
        args['evidence_sha256']=result['evidenceSha256']
        assert (await env['_evidence_tool']('record_task_review',args,None))['acceptanceStatus']=='accepted'
        assert (await env['_read_opencode_task'](record))['acceptanceStatus']=='accepted'
        fake.request.return_value=[{'info':{'role':'assistant'},'parts':[{'type':'text','text':'NEW'}]}]
        assert (await env['_evidence_tool']('record_task_review',args,None))['status']=='conflict'
        assert record['acceptanceStatus']=='pending_review'
    asyncio.run(run())


def test_automatic_fallback_preserves_session_and_explicit_choice(tmp_path):
    env,record,base,fake=fragment_env(tmp_path)
    base.return_value={'status':'failed','history':[{}],'latestTurn':{'providerErrorCategory':'rate_limited'}}
    async def run():
        for expected in ['groq','nvidia','gigachat']:
            result=await env['_read_opencode_task'](record)
            assert result['status']=='running' and record['provider']==expected
            assert fake.prompt.call_args.kwargs['session_id']=='ses_test'
        assert len(record['routingTransitions'])==3
        record['routingReason']='explicit_opencode_provider'
        fake.prompt.reset_mock()
        await env['_read_opencode_task'](record)
        fake.prompt.assert_not_called()
    asyncio.run(run())

@pytest.mark.parametrize('error,category', [('HTTP 429','rate_limited'),('free usage exceeded','quota_exhausted'),('HTTP 410','eol'),('MessageAbortedError',None)])
def test_failure_classification(error,category): assert classify(error)==category


def test_nvidia_rotation_leaves_room_for_reserve():
    items=[model('nvidia',str(i)) for i in range(8)]+[model('gigachat')]
    attempted=['nvidia/0','nvidia/1','nvidia/2']
    assert [m['provider'] for m in candidates(items,attempted)]==['gigachat']


def test_busy_task_is_never_cancelled_for_elapsed_time(tmp_path):
    env,record,base,fake=fragment_env(tmp_path)
    base.return_value={'status':'running','history':[{}],'latestTurn':{'providerErrorCategory':None,'providerState':{'type':'busy'}}}
    asyncio.run(env['_read_opencode_task'](record))
    fake.cancel.assert_not_called(); fake.prompt.assert_not_called()
