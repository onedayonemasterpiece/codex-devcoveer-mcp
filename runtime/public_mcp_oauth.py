"""DevCoveer OAuth boundary, adapted from the verified DevCoveer OAuth flow.

Independent grants and owner credential; no tool filtering or provider credentials.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizeError,
    RefreshToken, RegistrationError, TokenError)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

from public_mcp_owner import Actor, DomainError
from public_mcp_transport import create_transport_app, event

SCOPE = 'devcoveer'
COOKIE = '__Host-devcoveer-consent'
ACCESS_TTL, REFRESH_TTL, PENDING_TTL, CODE_TTL = 600, 30*86400, 300, 120
MAX_REDIRECT_URIS = 8
OAUTH_RESPONSE_KEYS = frozenset({'code','state','error','error_description','error_uri','iss'})


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def actor_data(actor):
    data = asdict(actor)
    data['scopes'] = sorted(actor.scopes)
    return data


def valid_redirect_uri(value):
    """Accept browser HTTPS and local native-client callbacks, never open HTTP."""
    if not isinstance(value,str) or not value or len(value)>2048:
        return False
    parsed = urlsplit(value)
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        return False
    if any(key in OAUTH_RESPONSE_KEYS for key,_ in parse_qsl(parsed.query,keep_blank_values=True)):
        return False
    if parsed.scheme=='https':
        return True
    if parsed.scheme!='http':
        return False
    host = parsed.hostname.lower().rstrip('.')
    if host=='localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def redirect_source(value):
    parsed = urlsplit(value)
    return f'{parsed.scheme}://{parsed.netloc}'


class OAuthProvider:
    """MCP SDK provider with durable, hashed grants and atomic one-use exchanges."""
    def __init__(self, store, auth_db, issuer, resource):
        self.store, self.path = store, Path(auth_db).absolute()
        self.issuer, self.resource = issuer, resource
        p = urlsplit(issuer)
        if (p.scheme != 'https' or not p.hostname or p.username or p.password or
                p.path not in ("", "/devcoveer") or p.query or p.fragment or p.port not in (None, 443) or
                resource != issuer + '/mcp/'):
            raise ValueError('Exact HTTPS origin issuer and issuer/mcp/ resource required')
        if self.path == store.path or self.path.is_symlink():
            raise ValueError('OAuth state must use a separate private regular file')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.stat().st_mode & 0o077:
            raise ValueError('OAuth state parent must be private (0700)')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY, data TEXT NOT NULL,
                csrf TEXT, expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS families(id TEXT PRIMARY KEY, actor TEXT NOT NULL,
                client TEXT NOT NULL, expires INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS credentials(hash TEXT PRIMARY KEY, kind TEXT NOT NULL,
                data TEXT NOT NULL, family TEXT, expires INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS rates(bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires INTEGER NOT NULL);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def current(self, data):
        actor = Actor(**{**data, 'scopes': frozenset(data['scopes'])})
        current = self.store.current(actor)
        if current.scopes != actor.scopes or current.owner != actor.owner:
            raise DomainError('access_revoked', next_action='reauthorize')
        return current

    def cleanup(self, db):
        now = int(time.time())
        db.execute('DELETE FROM pending WHERE expires < ?', (now,))
        db.execute('DELETE FROM rates WHERE expires < ?', (now,))
        # Keep consumed refresh hashes until family expiry for reuse detection.
        db.execute('DELETE FROM credentials WHERE expires < ?', (now,))
        db.execute('DELETE FROM families WHERE expires < ?', (now,))

    def rate(self, remote, path):
        now = int(time.time())
        with self.db() as db:
            self.cleanup(db)
            keys = [(f'global:{now//60}', 120), (f'{digest(remote)}:{path}:{now//60}', 20)]
            for key, limit in keys:
                row = db.execute('SELECT count FROM rates WHERE bucket=?', (key,)).fetchone()
                if row and row[0] >= limit:
                    return False
            for key, _ in keys:
                db.execute('INSERT INTO rates VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1', (key,now+120))
        return True

    async def get_client(self, client_id):
        with self.db() as db:
            row = db.execute('SELECT data FROM clients WHERE id=?', (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info):
        redirects = [str(u) for u in client_info.redirect_uris or []]
        if (not redirects or len(redirects)>MAX_REDIRECT_URIS or len(set(redirects))!=len(redirects) or
            not all(valid_redirect_uri(uri) for uri in redirects) or
            client_info.token_endpoint_auth_method not in {'none', 'client_secret_post', 'client_secret_basic'} or
            ('authorization_code' not in client_info.grant_types or not set(client_info.grant_types) <= {'authorization_code', 'refresh_token'}) or
            client_info.response_types != ['code'] or client_info.scope != SCOPE):
            raise RegistrationError('invalid_client_metadata', 'Clients require bounded HTTPS or loopback callbacks, PKCE code grants and the devcoveer scope')
        with self.db() as db:
            if db.execute('SELECT count(*) FROM clients').fetchone()[0] >= 1000:
                raise RegistrationError('invalid_client_metadata', 'Registration capacity reached; contact owner')
            db.execute('INSERT INTO clients VALUES(?,?)', (client_info.client_id,client_info.model_dump_json()))
        event('oauth_client_registered', client_id=client_info.client_id)

    async def authorize(self, client, params):
        registered = {str(uri) for uri in client.redirect_uris or []}
        if (params.resource not in (None, self.resource) or str(params.redirect_uri) not in registered or
                not re.fullmatch(r'[A-Za-z0-9_-]{43}', params.code_challenge) or
                params.scopes not in (None, [], [SCOPE]) or not params.state or len(params.state)>1024):
            raise AuthorizeError('invalid_request', 'Exact resource, scope, state and S256 PKCE required')
        params.resource = self.resource
        params.scopes = [SCOPE]
        request_id = secrets.token_urlsafe(32)
        data = {'client_id':client.client_id, 'params':params.model_dump(mode='json')}
        with self.db() as db:
            self.cleanup(db)
            if db.execute('SELECT count(*) FROM pending').fetchone()[0] >= 1000:
                raise AuthorizeError('temporarily_unavailable')
            db.execute('INSERT INTO pending VALUES(?,?,NULL,?)', (digest(request_id),json.dumps(data),int(time.time())+PENDING_TTL))
        event('oauth_authorization_started', client_id=client.client_id)
        return self.issuer + '/oauth/login?' + urlencode({'request':request_id})

    def consent_page(self, request_id):
        csrf = secrets.token_urlsafe(32)
        with self.db() as db:
            row = db.execute('SELECT data FROM pending WHERE id=? AND expires>=?', (digest(request_id),time.time())).fetchone()
            if not row:
                raise DomainError('oauth_request_expired')
            pending = json.loads(row['data'])
            client_row = db.execute('SELECT data FROM clients WHERE id=?',(pending['client_id'],)).fetchone()
            if not client_row:
                raise DomainError('oauth_invalid_consent')
            client = json.loads(client_row['data'])
            callback = pending['params']['redirect_uri']
            if callback not in client.get('redirect_uris',[]):
                raise DomainError('oauth_invalid_consent')
            db.execute('UPDATE pending SET csrf=? WHERE id=?', (digest(csrf),digest(request_id)))
        return csrf, str(client.get('client_name') or 'MCP-клиент')[:160], callback

    def consent(self, request_id, csrf, cookie, service_token):
        if not csrf or not cookie or not hmac.compare_digest(csrf.encode(),cookie.encode()):
            raise DomainError('oauth_invalid_consent')
        # Service token remains local and is never persisted by this bridge.
        actor = self.store.authenticate(service_token)
        if not actor.owner:
            raise DomainError('oauth_owner_required')
        data_actor = actor_data(actor)
        with self.db() as db:
            row = db.execute('SELECT * FROM pending WHERE id=?', (digest(request_id),)).fetchone()
            if not row or row['expires'] < time.time() or not row['csrf'] or not hmac.compare_digest(row['csrf'],digest(csrf)):
                raise DomainError('oauth_invalid_consent')
            self.current(data_actor)
            data = json.loads(row['data'])
            params = data['params']
            client_row = db.execute('SELECT data FROM clients WHERE id=?',(data['client_id'],)).fetchone()
            if not client_row or params['redirect_uri'] not in json.loads(client_row['data']).get('redirect_uris',[]):
                raise DomainError('oauth_invalid_consent')
            code = secrets.token_urlsafe(32)
            auth = {'code':'', 'client_id':data['client_id'], 'scopes':[SCOPE],
                'expires_at':int(time.time())+CODE_TTL, 'code_challenge':params['code_challenge'],
                'redirect_uri':params['redirect_uri'], 'redirect_uri_provided_explicitly':params['redirect_uri_provided_explicitly'],
                'resource':self.resource, 'subject':actor.principal_id, 'actor':data_actor}
            db.execute('INSERT INTO credentials VALUES(?,?,?,NULL,?,0)', (digest(code),'code',json.dumps(auth),auth['expires_at']))
            db.execute('DELETE FROM pending WHERE id=?', (digest(request_id),))
        event('oauth_consent_accepted', client_id=data['client_id'])
        separator = '&' if urlsplit(params['redirect_uri']).query else '?'
        return params['redirect_uri'] + separator + urlencode({'code':code,'state':params['state'],'iss':self.issuer})

    def credential(self, value, kind, client_id=None):
        if not isinstance(value,str) or not 20 <= len(value) <= 128:
            return None
        with self.db() as db:
            row = db.execute('SELECT * FROM credentials WHERE hash=? AND kind=?', (digest(value),kind)).fetchone()
            if not row or row['expires'] < time.time():
                return None
            data = json.loads(row['data'])
            if data.get('resource') != self.resource:
                return None
            if client_id and data['client_id'] != client_id:
                return None
            if row['used']:
                if kind == 'refresh':
                    db.execute('UPDATE families SET revoked=1 WHERE id=?',(row['family'],))
                return None
            if row['family']:
                family = db.execute('SELECT * FROM families WHERE id=?',(row['family'],)).fetchone()
                if not family or family['revoked'] or family['expires'] < time.time():
                    return None
                data['actor'] = json.loads(family['actor'])
            try:
                self.current(data['actor'])
            except DomainError:
                return None
            return data

    async def load_authorization_code(self, client, authorization_code):
        data = self.credential(authorization_code,'code',client.client_id)
        return AuthorizationCode(**{**data,'code':authorization_code}) if data else None

    def issue(self, db, family, client_id, actor, expires):
        self.cleanup(db)
        if db.execute('SELECT count(*) FROM credentials').fetchone()[0] >= 20000:
            raise TokenError('invalid_grant', 'Authorization capacity reached; contact owner')
        access, refresh = 'dvoa_' + secrets.token_urlsafe(32), 'dvor_' + secrets.token_urlsafe(32)
        now = int(time.time())
        for value, kind, expiry in ((access,'access',min(now+ACCESS_TTL,expires)), (refresh,'refresh',expires)):
            data = {'client_id':client_id,'scopes':[SCOPE],'expires_at':expiry,'resource':self.resource,'subject':actor['principal_id']}
            db.execute('INSERT INTO credentials VALUES(?,?,?,?,?,0)', (digest(value),kind,json.dumps(data),family,expiry))
        return OAuthToken(access_token=access,token_type='Bearer',expires_in=min(ACCESS_TTL,expires-now),refresh_token=refresh,scope=SCOPE)

    async def exchange_authorization_code(self, client, authorization_code):
        with self.db() as db:
            row = db.execute('SELECT * FROM credentials WHERE hash=? AND kind="code"', (digest(authorization_code.code),)).fetchone()
            if not row or row['used'] or row['expires'] < time.time():
                raise TokenError('invalid_grant')
            data = json.loads(row['data'])
            if data['client_id'] != client.client_id or data['resource'] != self.resource:
                raise TokenError('invalid_grant')
            try:
                self.current(data['actor'])
            except DomainError:
                raise TokenError('invalid_grant') from None
            db.execute('UPDATE credentials SET used=1 WHERE hash=?',(digest(authorization_code.code),))
            if db.execute('SELECT count(*) FROM families').fetchone()[0] >= 1000:
                raise TokenError('invalid_grant', 'Authorization capacity reached; contact owner')
            family, expiry = secrets.token_hex(32), int(time.time())+REFRESH_TTL
            db.execute('INSERT INTO families VALUES(?,?,?,?,0)',(family,json.dumps(data['actor']),client.client_id,expiry))
            result = self.issue(db,family,client.client_id,data['actor'],expiry)
        event('oauth_grant_issued', client_id=client.client_id)
        return result

    async def load_refresh_token(self, client, refresh_token):
        data = self.credential(refresh_token,'refresh',client.client_id)
        return RefreshToken(**{**data,'token':refresh_token}) if data else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        # Keep revocation committed even when a concurrent rotation loses the race.
        result = None
        with self.db() as db:
            row = db.execute('SELECT * FROM credentials WHERE hash=? AND kind="refresh"',(digest(refresh_token.token),)).fetchone()
            if row:
                family = db.execute('SELECT * FROM families WHERE id=?',(row['family'],)).fetchone()
                if family and family['client'] == client.client_id:
                    if row['used']:
                        db.execute('UPDATE families SET revoked=1 WHERE id=?',(family['id'],))
                    elif not family['revoked'] and min(row['expires'],family['expires']) >= time.time() and scopes == [SCOPE]:
                        actor = json.loads(family['actor'])
                        try:
                            self.current(actor)
                        except DomainError:
                            pass
                        else:
                            db.execute('UPDATE credentials SET used=1 WHERE hash=?',(digest(refresh_token.token),))
                            result = self.issue(db,family['id'],client.client_id,actor,family['expires'])
        if result is None:
            raise TokenError('invalid_grant')
        return result

    async def load_access_token(self, token):
        data = self.credential(token,'access')
        return AccessToken(**{**data,'token':token}) if data else None

    async def revoke_token(self, token):
        with self.db() as db:
            row = db.execute('SELECT family FROM credentials WHERE hash=?',(digest(token.token),)).fetchone()
            if row:
                db.execute('UPDATE families SET revoked=1 WHERE id=?',(row['family'],))

    def authenticate(self, token):
        if token.startswith(('dvoa_','dvor_')):
            data = self.credential(token,'access')
            if not data:
                raise DomainError('unauthorized',next_action='reauthorize')
            return self.current(data['actor'])
        return self.store.authenticate(token)


class OAuthBoundary:
    """Public auth endpoints use strict bounds; protected traffic retains core auth."""
    def __init__(self, app, provider):
        self.app, self.provider = app, provider
        parsed = urlsplit(provider.issuer)
        self.host = parsed.netloc
        self.origin = f'{parsed.scheme}://{parsed.netloc}'
        self.challenge = f'Bearer resource_metadata="{self.origin}/.well-known/oauth-protected-resource{urlsplit(provider.resource).path}", scope="{SCOPE}"'

    async def authorization_redirect(self, location, client_id, requested_redirect):
        """Add RFC 9207 issuer only to a callback registered by this server."""
        target = urlsplit(location)
        target_query = parse_qsl(target.query,keep_blank_values=True)
        if any(key=='iss' for key,_ in target_query):
            return location
        client = await self.provider.get_client(client_id) if client_id else None
        callbacks = [str(value) for value in client.redirect_uris or []] if client else []
        if requested_redirect:
            callbacks = [requested_redirect] if requested_redirect in callbacks else []
        elif len(callbacks)!=1:
            callbacks = []
        for value in callbacks:
            registered = urlsplit(value)
            if ((target.scheme,target.netloc,target.path)!=(registered.scheme,registered.netloc,registered.path)):
                continue
            remaining = list(target_query)
            try:
                for pair in parse_qsl(registered.query,keep_blank_values=True):
                    remaining.remove(pair)
            except ValueError:
                continue
            target_query.append(('iss',self.provider.issuer))
            return target._replace(query=urlencode(target_query)).geturl()
        return location

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope,receive,send)
        headers_list = scope.get('headers',[])
        headers = {k.decode().lower():v.decode() for k,v in headers_list}
        path, method = scope['path'], scope['method']
        public = path in {'/authorize','/token','/register','/revoke','/oauth/login'} or path.startswith('/.well-known/')
        query = []
        async def respond(error, status):
            event('oauth_request_rejected', path=path, reason=error, status=status)
            return await JSONResponse({'error':error},status_code=status,headers={'Cache-Control':'no-store'})(scope,receive,send)
        if sum(k.lower()==b'host' for k,v in headers_list)!=1 or headers.get('host') != self.host:
            return await respond('invalid_host',403)
        if headers.get('origin') not in (None,self.origin):
            return await respond('invalid_origin',403)
        async def safe_send(message):
            if message['type']=='http.response.start':
                out = [(k,v) for k,v in message['headers'] if k.lower() not in (b'www-authenticate',b'cache-control')]
                if path=='/authorize':
                    fixed=[]
                    for key,value in out:
                        if key.lower()==b'location':
                            values = dict(query)
                            value=(await self.authorization_redirect(value.decode(),values.get('client_id'),values.get('redirect_uri'))).encode()
                        fixed.append((key,value))
                    out=fixed
                # no-referrer makes navigate-mode HTML POST Origin:null in
                # browsers. Only the consent document needs strict-origin; it
                # reveals no path/query and keeps same-origin Origin verifiable.
                policy = b'strict-origin' if path=='/oauth/login' and method=='GET' and message['status']==200 else b'no-referrer'
                out.extend([(b'cache-control',b'no-store'),(b'referrer-policy',policy),(b'x-content-type-options',b'nosniff')])
                if message['status']==401:
                    out.append((b'www-authenticate',self.challenge.encode()))
                message = {**message,'headers':out}
            await send(message)
        if not public:
            return await self.app(scope,receive,safe_send)
        if len(scope.get('query_string',b''))>8192:
            return await respond('invalid_request',400)
        remote = str(scope.get('client',('unknown',))[0])
        if not self.provider.rate(remote,path):
            return await respond('temporarily_unavailable',429)
        try:
            query = parse_qsl(scope.get('query_string',b'').decode(),keep_blank_values=True,max_num_fields=30)
            if len(dict(query))!=len(query) or any(k in {'token','service_token','password','access_token'} for k,v in query):
                return await respond('invalid_request',400)
            if method=='POST':
                body = bytearray()
                async with asyncio.timeout(10):
                    while True:
                        msg = await receive()
                        if msg['type']=='http.disconnect':
                            return
                        body.extend(msg.get('body',b''))
                        if len(body)>16384:
                            return await respond('request_too_large',413)
                        if not msg.get('more_body',False):
                            break
                if path=='/register':
                    value = json.loads(body)
                    if not isinstance(value,dict):
                        return await respond('invalid_client_metadata',400)
                else:
                    if headers.get('content-type','').split(';')[0] != 'application/x-www-form-urlencoded':
                        return await respond('invalid_request',400)
                    pairs = parse_qsl(body.decode(),keep_blank_values=True,max_num_fields=30)
                    if len(dict(pairs))!=len(pairs):
                        return await respond('invalid_request',400)
                    values = dict(pairs)
                    if path=='/token' and values.get('resource') not in (None,self.provider.resource):
                        return await respond('invalid_target',400)
                    if path=='/token' and values.get('grant_type')=='authorization_code' and not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', values.get('code_verifier','')):
                        return await respond('invalid_request',400)
                    # Installed SDK RevocationRequest requires nullable client_secret
                    # with no default even for public clients. Normalize absence to
                    # empty, never mint/use a client secret or bypass SDK client auth.
                    if path=='/revoke' and 'client_secret' not in values:
                        body = bytearray(urlencode({**values,'client_secret':''}).encode())
                    if path=='/oauth/login' and headers.get('origin')!=self.origin:
                        return await respond('invalid_origin',403)
                    if path=='/authorize' and values.get('code_challenge_method')!='S256':
                        return await respond('invalid_request',400)
                original, consumed = receive, False
                async def replay():
                    nonlocal consumed
                    if not consumed:
                        consumed = True
                        return {'type':'http.request','body':bytes(body),'more_body':False}
                    return await original()
                receive = replay
            if path=='/authorize' and method=='GET' and dict(query).get('code_challenge_method')!='S256':
                return await respond('invalid_request',400)
            return await self.app(scope,receive,safe_send)
        except (ValueError,UnicodeError,RecursionError):
            return await respond('invalid_request',400)
        except TimeoutError:
            return await respond('request_timeout',408)


def create_oauth_app(store, *, auth_db, issuer, resource=None, mcp_server, history=None):
    """Build opt-in server. issuer is exact HTTPS origin WITHOUT trailing slash."""
    provider = OAuthProvider(store,auth_db,issuer,resource or issuer+'/mcp/')
    protected = create_transport_app(mcp_server, provider=provider, issuer=issuer, history=history)
    registration = ClientRegistrationOptions(enabled=True,valid_scopes=[SCOPE],default_scopes=[SCOPE])
    routes = create_auth_routes(provider,AnyHttpUrl(issuer),client_registration_options=registration,revocation_options=RevocationOptions(enabled=True))
    async def metadata(request):
        return JSONResponse({'issuer':issuer,'authorization_endpoint':issuer+'/authorize',
            'token_endpoint':issuer+'/token','registration_endpoint':issuer+'/register','revocation_endpoint':issuer+'/revoke',
            'response_types_supported':['code'],'grant_types_supported':['authorization_code','refresh_token'],
            'token_endpoint_auth_methods_supported':['none','client_secret_post','client_secret_basic'],'revocation_endpoint_auth_methods_supported':['none','client_secret_post','client_secret_basic'],
            'scopes_supported':[SCOPE],'code_challenge_methods_supported':['S256'],
            'authorization_response_iss_parameter_supported':True})
    routes[0] = Route('/.well-known/oauth-authorization-server',metadata,methods=['GET'])
    routes += create_protected_resource_routes(AnyHttpUrl(provider.resource),[AnyHttpUrl(issuer)],scopes_supported=[SCOPE],resource_name='DevCoveer')
    # AnyHttpUrl adds a slash to bare origins; preserve the exact issuer string
    # advertised by authorization metadata (issuer comparison must be exact).
    async def resource_endpoint(request):
        return JSONResponse({'resource':provider.resource,'authorization_servers':[issuer],
            'scopes_supported':[SCOPE],'bearer_methods_supported':['header'],'resource_name':'DevCoveer'})
    routes[-1] = Route('/.well-known/oauth-protected-resource/mcp/',resource_endpoint,methods=['GET'])
    # Root alias helps clients that start discovery at the hostname.
    routes += [Route('/.well-known/oauth-protected-resource',resource_endpoint,methods=['GET','OPTIONS']),
               Route('/.well-known/oauth-protected-resource/mcp',resource_endpoint,methods=['GET','OPTIONS'])]
    async def login(request: Request):
        try:
            if request.method=='GET':
                request_id = request.query_params.get('request','')
                csrf, client_name, callback = provider.consent_page(request_id)
                esc = html.escape
                callback_host = urlsplit(callback).netloc
                markup = f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Подключить DevCoveer</title><body><main><h1>Подключить DevCoveer</h1><p>Клиент <strong>{esc(client_name)}</strong> запрашивает доступ через <strong>{esc(callback_host)}</strong> ко всем инструментам DevCoveer: чтению и изменению проектов, запуску задач и операциям сервера. Действующие правила DevCoveer сохраняются.</p><p>Введите ключ подключения DevCoveer. Ключ останется у DevCoveer и не будет передан MCP-клиенту.</p><form method="post" action="{esc(issuer)}/oauth/login"><input type="hidden" name="request" value="{esc(request_id)}"><input type="hidden" name="csrf" value="{esc(csrf)}"><label>Токен DevCoveer <input type="password" name="service_token" required maxlength="512" autocomplete="off"></label><p><label><input type="checkbox" name="consent" value="yes" required> Разрешаю подключение</label></p><button type="submit">Подключить</button></form><p>Не хотите подключать — закройте эту страницу.</p></main></body></html>'''
                response = HTMLResponse(markup,headers={'Content-Security-Policy':f"default-src 'none'; form-action 'self' {redirect_source(callback)}; frame-ancestors 'none'; base-uri 'none'"})
                response.set_cookie(COOKIE,csrf,secure=True,httponly=True,samesite='strict',max_age=PENDING_TTL,path='/')
                return response
            data = await request.form()
            if data.get('consent')!='yes':
                raise DomainError('oauth_consent_required')
            redirect = provider.consent(str(data.get('request','')),str(data.get('csrf','')),request.cookies.get(COOKIE,''),str(data.get('service_token','')))
            response = RedirectResponse(redirect,status_code=303)
            response.delete_cookie(COOKIE,path='/',secure=True,httponly=True,samesite='strict')
            return response
        except DomainError:
            event('oauth_consent_denied')
            return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Подключение не выполнено</title><h1>Подключение не выполнено</h1><p>Проверьте токен или начните подключение заново в MCP-клиенте.</p></html>',status_code=403)
    routes += [Route('/oauth/login',login,methods=['GET','POST']),Mount('/',app=protected)]
    # The mounted protected application owns the MCP manager lifespan.
    app = Starlette(routes=routes,lifespan=protected.router.lifespan_context)
    result = OAuthBoundary(app,provider)
    result.provider = provider
    return result
