"""Ordered live routing and bounded operational health, without provider secrets."""
from __future__ import annotations
import json
import time
from typing import Any
from model_health import ModelHealthStore, resource_priority, resource_class
from opencode_backend import OpenCodeBackend, DEFAULT_OPENCODE_MODEL


def classify(error: Any) -> str | None:
    text = json.dumps(error, ensure_ascii=False).lower()
    if 'messageabortederror' in text: return None
    if any(s in text for s in ('free usage exceeded', 'quota exceeded', 'quota_exhausted', 'usage limit exceeded', 'insufficient_quota')): return 'quota_exhausted'
    if any(s in text for s in ('429', 'rate limit', 'rate_limit', 'too many requests')): return 'rate_limited'
    if any(s in text for s in ('410', 'end of life')): return 'eol'
    if any(s in text for s in ('timed out', 'timeout')): return 'timeout'
    if any(s in text for s in ('503', '502', '500', 'overloaded', 'unavailable')): return 'provider_unavailable'
    if any(s in text for s in ('401', '403', 'invalid api key', 'authentication')): return 'provider_unavailable'
    return None


def candidates(models: list[dict], attempted: list[str] = ()) -> list[dict]:
    admitted = []
    for item in models:
        if item.get('backend') == 'codex': continue
        p = item['provider']; native = item['nativeModelId']
        capabilities = item.get('capabilities', {})
        if capabilities.get('toolcall') is not True: continue
        modalities = capabilities.get('modalities', {})
        if any(modalities.get(k) and 'text' not in modalities[k] for k in ('input', 'output')): continue
        if p == 'opencode' and not item.get('free'): continue
        if p.startswith('groq') and (p not in ('groq', 'groq2', 'groq3') or native != 'openai/gpt-oss-120b'): continue
        if p not in ('opencode', 'groq', 'groq2', 'groq3', 'atria', 'nvidia') and not p.startswith('giga'): continue
        # Bound expensive endpoint rotation and leave room for the reserve tier.
        if p == 'nvidia' and sum(s.startswith('nvidia/') for s in attempted) >= 3: continue
        if item.get('routingRecommended') is False or item['selection'] in attempted: continue
        admitted.append(item)
    def rank(item):
        p = item['provider']; health = item.get('runtimeHealth', {})
        hour = health.get('windows', {}).get('1h', {})
        return (resource_priority(p), {'groq': 0, 'groq2': 1, 'groq3': 2}.get(p, 0),
                0 if item['selection'] == DEFAULT_OPENCODE_MODEL else 1,
                -(hour.get('successRate') or 0), hour.get('p95LatencyMs') or 10**9, item['selection'])
    return sorted(admitted, key=rank)


class RoutingBackend(OpenCodeBackend):
    def __init__(self):
        super().__init__()
        self.health_store = ModelHealthStore()

    async def catalog(self, *, directory=None):
        result = await super().catalog(directory=directory)
        for item in result['models']:
            p = item['provider']; selection = item['selection']
            # Custom credentialed providers are not part of the renewable-free tier,
            # even when their catalog omits pricing data.
            if p == 'atria': item['free'] = False
            h = self.health_store.summary(selection)
            ph = self.health_store.provider_summary(p)
            blocked = h['currentRuntimeStatus'] in ('eol', 'temporarily_unavailable')
            if p == 'opencode' and ph.get('lastCategory') == 'quota_exhausted' and ph.get('currentRuntimeStatus') == 'temporarily_unavailable': blocked = True
            item.update(runtimeHealth=h, resourceClass=resource_class(p), resourcePriority=resource_priority(p), routingRecommended=not blocked)
        result['providerHealth'] = {p:self.health_store.provider_summary(p) for p in result['connected']}
        return result

    async def inspect(self, *, session_id, directory, include_messages=True):
        result = await super().inspect(session_id=session_id, directory=directory, include_messages=include_messages)
        messages = result.get('messages', [])
        # Only the latest user turn can determine execution status after continuation.
        last_user = max((i for i, m in enumerate(messages) if m.get('info', {}).get('role') == 'user'), default=-1)
        assistants = [m for m in messages[last_user+1:] if m.get('info', {}).get('role') == 'assistant']
        latest = assistants[-1] if assistants else {}
        info = latest.get('info', {})
        result['error'] = info.get('error')
        text = '\n'.join(p['text'] for m in assistants for p in m.get('parts', []) if p.get('type') == 'text' and isinstance(p.get('text'), str))
        result['finalResponse'] = text or None
        result['messageId'] = info.get('id')
        states = await self.request('GET', '/session/status', directory=directory)
        state = states.get(session_id, {'type': 'idle'})
        result['sessionStatus'] = state.get('type', 'idle')
        if result['sessionStatus'] in ('busy', 'retry'): result['status'] = 'running'
        elif result['error']: result['status'] = 'failed'
        # A terminal assistant turn may finish with tool calls and no text part.
        # Completion comes from the turn metadata; finalResponse is evidence,
        # not a liveness signal.
        elif info.get('time', {}).get('completed'): result['status'] = 'completed'
        else:
            user_time = messages[last_user].get('info', {}).get('time', {}).get('created', 0) if last_user >= 0 else 0
            result['status'] = 'running' if user_time and time.time() * 1000 - user_time < 900_000 else 'idle'
        category = classify([result['error'], state])
        if category is None and state.get('type') == 'retry': category = 'upstream_error'
        result['providerErrorCategory'] = category
        result['providerState'] = state
        if info.get('providerID') and info.get('modelID'):
            p = info['providerID']; selection = p + '/' + info['modelID']
            if category or result['status'] == 'completed':
                self.health_store.record(provider=p, selection=selection,
                    outcome='success' if result['status'] == 'completed' else 'retry' if result['status'] == 'running' else 'failure',
                    category=category or 'ok', event_key=f"{session_id}:{info.get('id')}:{category or 'ok'}")
        return result
