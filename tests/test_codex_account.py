import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from codex_account import normalize_limits, read_status, resolve_profile

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def snapshot(primary=25, secondary=50):
    return {'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': primary},
                           'secondary': {'usedPercent': secondary}}}


@pytest.mark.parametrize('usage,eligible', [(0, True), (89.9, True), (90, False), (100, False)])
def test_reserve_is_strictly_greater_than_ten(usage, eligible):
    result = normalize_limits(snapshot(usage, 0), now=NOW)
    assert result['admission']['eligible'] is eligible
    assert result['admission']['effective_remaining_percent'] == pytest.approx(100-usage)


@pytest.mark.parametrize('bad', [None, True, -1, 101, '25', float('nan'), float('inf')])
def test_bad_percentage_is_unknown_and_denied(bad):
    result = normalize_limits(snapshot(bad), now=NOW)
    assert not result['admission']['eligible']
    assert result['status'] == 'unknown'


def test_short_and_long_windows_are_not_averaged():
    result = normalize_limits(snapshot(10, 95), now=NOW)
    assert result['admission']['effective_remaining_percent'] == 5
    assert not result['admission']['eligible']


def test_map_wins_over_legacy_snapshot():
    result = normalize_limits({**snapshot(0, 0), 'rateLimitsByLimitId': {
        'codex': snapshot(96, 0)['rateLimits'],
        'another': {'limitId': 'another', 'primary': {'usedPercent': 0}},
    }}, now=NOW)
    assert result['admission']['effective_remaining_percent'] == 4
    assert not result['admission']['eligible']


def test_no_codex_bucket_does_not_borrow_another_quota():
    result = normalize_limits({'rateLimitsByLimitId': {'other': snapshot()['rateLimits'] | {'limitId': 'other'}}}, now=NOW)
    assert not result['admission']['eligible']
    assert result['admission']['reason'] == 'codex_quota_bucket_unknown'


def test_expired_reset_and_limit_reached_are_not_eligible():
    value = snapshot()
    value['rateLimits']['primary']['resetsAt'] = NOW.timestamp()-1
    assert not normalize_limits(value, now=NOW)['admission']['eligible']
    value = snapshot()
    value['rateLimits']['rateLimitReachedType'] = 'rate_limit'
    assert not normalize_limits(value, now=NOW)['admission']['eligible']


def catalog(model='gpt-6.1-sol'):
    return [{'id':model, 'model':model, 'supportedReasoningEfforts':[{'reasoningEffort':'medium'}]}]


def test_profile_maps_only_to_actual_native_model():
    assert resolve_profile('gpt-6.1-medium', None, catalog()) == ('gpt-6.1-sol', 'medium')
    assert resolve_profile('gpt-6.1-medium', 'medium', catalog('gpt-6.1')) == ('gpt-6.1', 'medium')
    assert resolve_profile('gpt-6-astra', 'high', []) == ('gpt-6-astra', 'high')
    assert resolve_profile(None, None, []) == (None, None)


@pytest.mark.parametrize('rows,effort', [([], None), (catalog()+catalog('gpt-6.1'), None), (catalog(), 'high')])
def test_missing_ambiguous_conflicting_profile_rejected(rows, effort):
    with pytest.raises(ValueError):
        resolve_profile('gpt-6.1-medium', effort, rows)


def test_status_uses_only_documented_read_methods_no_private_fields():
    class History:
        def __init__(self): self.calls=[]
        async def request(self, method, params, timeout):
            self.calls.append((method, params))
            return (snapshot() | {'private': 'must-not-leak', 'accessToken': 'not-logged'}) if method.startswith('account/') else {'data':catalog()}
    history=History()
    result=asyncio.run(read_status(history))
    assert [c[0] for c in history.calls] == ['account/rateLimits/read', 'model/list']
    assert result['inference_performed'] is False
    assert result['default_profile']['model'] == 'gpt-6.1-sol'
    assert result['default_profile']['inference_verified'] is False
    assert 'must-not-leak' not in str(result)


def test_status_failure_does_not_claim_quota_or_model():
    class History:
        async def request(self, method, params, timeout): raise RuntimeError('private-error')
    result=asyncio.run(read_status(History()))
    assert result['status']=='unknown'
    assert not result['admission']['eligible']
    assert not result['default_profile']['catalog_available']
    assert 'private-error' not in str(result)


def test_canonical_builder_wires_status_and_alias(tmp_path):
    import subprocess
    root = Path(__file__).resolve().parents[1]
    builder = root / 'scripts' / 'build-clean-host-runtime.py'
    if not builder.exists():
        pytest.skip('Full repository integration test runs in the source handoff')
    result = subprocess.run([sys.executable, str(builder), str(tmp_path / 'runtime')],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    source = (tmp_path / 'runtime' / 'codex-chatgpt-bridge.py').read_text()
    compile(source, 'generated-bridge', 'exec')
    assert 'name="codex_status"' in source
    assert '"codex_status": Draft202012Validator(STATUS_SCHEMA)' in source
    assert 'return _tool_result(await _codex_read_status(history))' in source
    assert '_codex_resolve_profile(' in source
    assert (tmp_path / 'runtime' / 'codex_account.py').is_file()
