"""OAuth and full MCP transport integration, without provider calls."""
import base64
import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp import types
from mcp.server.lowlevel import Server
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
from public_mcp_oauth import create_oauth_app
from public_mcp_owner import OwnerStore

ISSUER = 'https://identity.example.test/devcoveer'
ORIGIN = 'https://identity.example.test'
RESOURCE = ISSUER + '/mcp/'
CALLBACK = 'https://grok.com/connectors/oauth/callback'
OWNER = 'dvc_owner_' + 'X' * 50


@pytest.fixture
def setup(tmp_path):
    tmp_path.chmod(0o700)
    key = tmp_path / 'owner-key'
    key.write_text(OWNER)
    key.chmod(0o600)
    async def tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name=name, description=name, input_schema={'type': 'object'})
                for name in ('read_code', 'write_code', 'start_task', 'host_action')])

    async def call(ctx, params):
        return types.CallToolResult(content=[types.TextContent(type='text', text=params.name)])

    server = Server('test-full-authority', on_list_tools=tools, on_call_tool=call)

    app = create_oauth_app(OwnerStore(key), auth_db=tmp_path / 'oauth.sqlite',
                           issuer=ISSUER, mcp_server=server)
    # nginx strips /devcoveer on upstream requests; advertised URLs retain it.
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        yield app, client, key


def grant(setup, *, callback=CALLBACK, resource=True, owner=OWNER, verifier='a'*43, auth_method='none'):
    app, client, _ = setup
    response = client.post('/register', json={
        'redirect_uris': [callback], 'token_endpoint_auth_method': auth_method,
        'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
    })
    assert response.status_code == 201, response.text
    cid = response.json()['client_id']
    client_secret = response.json().get('client_secret')
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    params = dict(client_id=cid, response_type='code', redirect_uri=callback, state='state',
                  code_challenge=challenge, code_challenge_method='S256')
    if resource:
        params['resource'] = RESOURCE
    response = client.get('/authorize', params=params)
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers['location'])
    page = client.get('/oauth/login?' + location.query)
    assert page.status_code == 200
    assert f'action="{ISSUER}/oauth/login"' in page.text
    assert OWNER not in page.text
    fields = dict(re.findall(r'name="(request|csrf)" value="([^"]+)"', page.text))
    consent = client.post('/oauth/login', data={**fields, 'service_token': owner, 'consent': 'yes'},
                          headers={'Origin': ORIGIN})
    if owner != OWNER:
        assert consent.status_code == 403
        return
    assert consent.status_code == 303, consent.text
    values = parse_qs(urlsplit(consent.headers['location']).query)
    assert values['iss'] == [ISSUER] and values['state'] == ['state']
    data = dict(client_id=cid, grant_type='authorization_code', code=values['code'][0],
                code_verifier=verifier, redirect_uri=callback)
    if client_secret:
        data['client_secret'] = client_secret
    if resource:
        data['resource'] = RESOURCE
    response = client.post('/token', data=data)
    assert response.status_code == 200, response.text
    return cid, response.json(), data


def rpc(client, token, method, params=None):
    return client.post('/mcp/', headers={'Authorization': 'Bearer ' + token,
                                        'Accept': 'application/json, text/event-stream',
                                        'MCP-Protocol-Version': '2025-03-26'},
                       json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})


def test_discovery_and_anonymous_denial(setup):
    _, client, _ = setup
    for path in ('/mcp', '/mcp/'):
        response = client.get(path)
        assert response.status_code == 401
        assert ORIGIN + '/.well-known/oauth-protected-resource/devcoveer/mcp/' in response.headers['www-authenticate']
    assert client.get('/.well-known/oauth-authorization-server').json()['issuer'] == ISSUER
    assert client.get('/.well-known/oauth-protected-resource/mcp/').json()['resource'] == RESOURCE


@pytest.mark.parametrize('resource', [True, False])
@pytest.mark.parametrize('callback', [CALLBACK, 'https://other.example/callback?connection=devcoveer',
                                     'http://127.0.0.1:49152/callback'])
def test_full_authority_and_public_client_compatibility(setup, callback, resource):
    _, client, _ = setup
    _, tokens, _ = grant(setup, callback=callback, resource=resource)
    token = tokens['access_token']
    init = rpc(client, token, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                           'clientInfo': {'name': 'grok-test', 'version': '1'}})
    assert init.status_code == 200, init.text
    tools = rpc(client, token, 'tools/list').json()['result']['tools']
    assert {t['name'] for t in tools} == {'read_code', 'write_code', 'start_task', 'host_action'}
    response = rpc(client, token, 'tools/call', {'name': 'write_code', 'arguments': {}})
    assert response.status_code == 200 and response.json()['result']['content'][0]['text'] == 'write_code'


def test_bad_owner_does_not_grant(setup):
    grant(setup, owner='wrong-owner-key')


def test_pkce_code_reuse_refresh_reuse_and_rotation(setup):
    app, client, key = setup
    cid, tokens, data = grant(setup)
    assert client.post('/token', data=data).status_code == 400
    refresh = dict(client_id=cid, grant_type='refresh_token', refresh_token=tokens['refresh_token'], resource=RESOURCE)
    rotated = client.post('/token', data=refresh)
    assert rotated.status_code == 200
    assert client.post('/token', data=refresh).status_code == 400
    assert rpc(client, rotated.json()['access_token'], 'tools/list').status_code == 401
    _, tokens, _ = grant(setup)
    key.write_text('dvc_owner_' + 'Y' * 50)
    assert rpc(client, tokens['access_token'], 'tools/list').status_code == 401
    for secret in (OWNER, data['code'], tokens['access_token'], tokens['refresh_token']):
        assert secret.encode() not in app.provider.path.read_bytes()


def test_bounds_and_origin_host_checks(setup):
    _, client, _ = setup
    assert client.get('/healthz', headers={'Host': 'evil.example'}).status_code == 403
    assert client.get('/healthz', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/register', content=b'{' + b'x'*17000).status_code == 413
    assert client.get('/authorize?token=secret').status_code == 400
    assert client.get('/authorize?client_id=a&client_id=b').status_code == 400


@pytest.mark.parametrize('callback', ['http://evil.example/callback', 'file:///callback',
                                     CALLBACK+'#fragment', CALLBACK+'?code=reserved'])
def test_unsafe_callback_registration_rejected(setup, callback):
    _, client, _ = setup
    response = client.post('/register', json={'redirect_uris': [callback],
                           'token_endpoint_auth_method': 'none'})
    assert response.status_code == 400


def test_wrong_resource_and_wrong_pkce_cannot_exchange(setup):
    _, client, _ = setup
    _, _, data = grant(setup)
    assert client.post('/token', data={**data, 'resource': 'https://evil.example/mcp/'}).status_code == 400
    assert client.post('/token', data={**data, 'code_verifier': 'b'*43}).status_code == 400


def test_owner_key_is_not_a_bearer_access_token(setup):
    assert rpc(setup[1], OWNER, 'tools/list').status_code == 401


def test_confidential_client_supported_without_weakening_pkce(setup):
    cid, tokens, data = grant(setup, auth_method='client_secret_post')
    assert 'client_secret' in data
    assert rpc(setup[1], tokens['access_token'], 'tools/list').status_code == 200


def test_additive_edge_render_preserves_all_existing_routes():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / 'scripts/install-public-mcp.py'
    spec = importlib.util.spec_from_file_location('public_mcp_installer', path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    original = '''http {
    server {
        server_name identity.kenigevents.ru;
        location / { proxy_pass http://127.0.0.1:8780; }
    }
    server { server_name another.example; }
}
'''
    candidate = installer.render_edge(original)
    assert candidate.replace(installer.LOCATIONS, '') == original
    assert installer.render_edge(candidate) == candidate
    assert 'location ^~ /devcoveer/' in candidate
    assert '/.well-known/oauth-authorization-server/devcoveer' in candidate


def test_bound_edge_write_preserves_inode(tmp_path):
    import importlib.util
    path = Path(__file__).resolve().parents[1] / 'scripts/install-public-mcp.py'
    spec = importlib.util.spec_from_file_location('public_mcp_installer_write', path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    config = tmp_path / 'nginx.conf'
    config.write_text('before')
    inode = config.stat().st_ino
    installer.write_bound_file(config, 'after')
    assert config.read_text() == 'after' and config.stat().st_ino == inode
