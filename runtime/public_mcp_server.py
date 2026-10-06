"""Run the canonical DevCoveer bridge over a separate OAuth HTTP listener."""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

from public_mcp_oauth import create_oauth_app
from public_mcp_owner import OwnerStore


def create_app():
    runtime = Path(os.environ.get('DEVCOVEER_BRIDGE_RUNTIME', '/home/dev/.local/libexec/openai-codex-mcp'))
    sys.path.insert(0, str(runtime))
    spec = importlib.util.spec_from_file_location('devcoveer_public_bridge', runtime / 'codex-chatgpt-bridge.py')
    bridge = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bridge
    spec.loader.exec_module(bridge)
    # OAuth URLs contain transaction identifiers; do not log HTTP client URLs.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    issuer = os.environ['DEVCOVEER_PUBLIC_ISSUER'].rstrip('/')
    state = Path(os.environ['DEVCOVEER_PUBLIC_STATE'])
    owner = OwnerStore(os.environ['DEVCOVEER_PUBLIC_OWNER_KEY_FILE'])
    history = bridge.NativeHistoryClient()
    server = bridge.build_server(history)
    return create_oauth_app(owner, auth_db=state / 'oauth.sqlite', issuer=issuer,
                            mcp_server=server, history=history)


if __name__ == '__main__':
    import uvicorn
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    # Provider and tunnel credentials never enter the new HTTP service.
    for name in ('CONTROL_PLANE_API_KEY', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_ADMIN_KEY'):
        os.environ.pop(name, None)
    uvicorn.run(create_app(), host='127.0.0.1', port=int(os.environ.get('DEVCOVEER_PUBLIC_PORT', '7320')),
                access_log=False, proxy_headers=False)
