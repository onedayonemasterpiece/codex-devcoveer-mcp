#!/usr/bin/env python3
"""Read only the native Codex catalogue and account quota; never starts inference."""
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

RUNTIME = Path('/home/dev/.local/libexec/openai-codex-mcp')
sys.path.insert(0, str(RUNTIME))
spec = importlib.util.spec_from_file_location('devcoveer_account_probe', RUNTIME / 'codex-chatgpt-bridge.py')
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)

async def main():
    history = bridge.NativeHistoryClient()
    result = {'inference_performed': False}
    try:
        result['cli_version'] = subprocess.run([bridge.CODEX_BINARY, '--version'], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        for hidden in (False, True):
            payload = await history.request('model/list', {'cursor': None, 'limit': 100, 'includeHidden': hidden}, timeout=15)
            result['models_all' if hidden else 'models_visible'] = [{k: row.get(k) for k in ('id', 'model', 'displayName', 'supportedReasoningEfforts', 'defaultReasoningEffort', 'hidden')} for row in payload.get('data', []) if isinstance(row, dict)]
        try:
            quota = await history.request('account/rateLimits/read', {}, timeout=15)
            result['rate_limits'] = {k: quota.get(k) for k in ('rateLimits', 'rateLimitsByLimitId')}
        except Exception as exc:
            result['quota_error'] = type(exc).__name__
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await asyncio.to_thread(history._stop_sync)

asyncio.run(main())
