"""Authenticated HTTP transport for the existing, complete DevCoveer server."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

LOG = logging.getLogger('devcoveer.public_mcp')


def event(name, **fields):
    LOG.info(json.dumps({'component': 'public_mcp', 'event': name, **fields}, sort_keys=True))


def create_transport_app(server, *, provider, issuer, history=None):
    host = urlsplit(issuer).hostname
    origin = 'https://' + urlsplit(issuer).netloc
    manager = StreamableHTTPSessionManager(
        server, json_response=True, stateless=True,
        max_request_body_size=4 * 1024 * 1024,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host], allowed_origins=[origin],
        ),
    )

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            event('started', transport='streamable_http', authority='full_devcoveer')
            try:
                yield
            finally:
                if history is not None:
                    await history.close()
                event('stopped')

    async def health(request):
        return JSONResponse({'status': 'ok', 'component': 'devcoveer-public-mcp'})

    class ProtectedMCP:
        async def __call__(self, scope, receive, send):
            headers = {k.lower(): v for k, v in scope.get('headers', [])}
            bearer = headers.get(b'authorization', b'')
            token = None
            if bearer.lower().startswith(b'bearer '):
                try:
                    token = await provider.load_access_token(bearer[7:].decode('ascii'))
                except (UnicodeError, ValueError):
                    pass
            if token is None or token.scopes != ['devcoveer'] or token.resource != provider.resource:
                event('access_denied', reason='invalid_or_missing_access_token')
                await JSONResponse({'error': 'unauthorized'}, status_code=401)(scope, receive, send)
                return
            status = 0

            async def record(message):
                nonlocal status
                if message['type'] == 'http.response.start':
                    status = message['status']
                await send(message)

            try:
                await manager.handle_request(scope, receive, record)
            finally:
                event('request_finished', client_id=token.client_id, method=scope['method'], status=status)

    endpoint = ProtectedMCP()
    return Starlette(
        routes=[Route('/healthz', health), Route('/mcp', endpoint, methods=['GET', 'POST', 'DELETE']),
                Route('/mcp/', endpoint, methods=['GET', 'POST', 'DELETE'])],
        lifespan=lifespan,
    )
