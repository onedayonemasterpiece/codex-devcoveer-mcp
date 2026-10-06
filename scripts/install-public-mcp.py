#!/usr/bin/env python3
"""Add OAuth MCP without replacing/restarting the OpenAI tunnel.

Run as dev, from a verified main commit. Uses the existing TLS edge/certificate.
No secret values are printed. The existing nginx file is a file bind mount:
preserve its inode, validate nginx, gracefully reload and restore on failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import secrets
import shutil
import subprocess
from pathlib import Path

ISSUER = 'https://identity.kenigevents.ru/devcoveer'
EDGE = Path('/home/dev/.local/state/my-data-hub-control-plane/edge/nginx.conf')
CONTAINER = 'record-idea-hub-backend-edge-1'
STATE = Path('/home/dev/.local/state/devcoveer-public-mcp')
CONFIG = Path('/home/dev/.config/devcoveer-public-mcp')
RELEASES = Path('/home/dev/.local/libexec/devcoveer-public-mcp/releases')
PYTHON = Path('/home/dev/.local/share/devcoveer/public-mcp/venv/bin/python')

LOCATIONS = '''        # DevCoveer OAuth MCP: additive route, independent of OpenAI tunnel.
        location ^~ /devcoveer/ {
            proxy_pass http://127.0.0.1:7320/;
            include /etc/nginx/proxy-common.conf;
            client_max_body_size 4m;
        }
        location = /.well-known/oauth-authorization-server/devcoveer {
            proxy_pass http://127.0.0.1:7320/.well-known/oauth-authorization-server;
            include /etc/nginx/proxy-common.conf;
        }
        location = /.well-known/oauth-protected-resource/devcoveer/mcp {
            proxy_pass http://127.0.0.1:7320/.well-known/oauth-protected-resource/mcp/;
            include /etc/nginx/proxy-common.conf;
        }
        location = /.well-known/oauth-protected-resource/devcoveer/mcp/ {
            proxy_pass http://127.0.0.1:7320/.well-known/oauth-protected-resource/mcp/;
            include /etc/nginx/proxy-common.conf;
        }

'''


def run(args, timeout=60):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{Path(args[0]).name} failed ({result.returncode}): {result.stderr[-1200:]}')
    return result.stdout


def render_edge(text):
    if '# DevCoveer OAuth MCP:' in text:
        if LOCATIONS not in text:
            raise ValueError('Existing DevCoveer route differs; review before replacement')
        return text
    start = text.index('        server_name identity.kenigevents.ru;')
    anchor = text.index('        location / {', start)
    end = text.index('\n    }', start)
    if anchor >= end:
        raise ValueError('Identity route insertion point is outside its server')
    return text[:anchor] + LOCATIONS + text[anchor:]


def write_bound_file(path, content):
    # Replacing a file inode would leave docker reading its old bind mount.
    with path.open('w') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def install(repo, evidence):
    if os.getuid() != pwd.getpwnam('dev').pw_uid:
        raise ValueError('Run as the dev user')
    if not (evidence / '.artifact.json').is_file():
        raise ValueError('A managed artifact directory is required')
    sha = run(['git', '-C', str(repo), 'rev-parse', 'HEAD']).strip()
    run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', sha, 'origin/main'])
    if run(['git', '-C', str(repo), 'status', '--porcelain']).strip():
        raise ValueError('Install only clean validated source')
    if not PYTHON.is_file():
        raise ValueError('Provision public-mcp virtualenv first')
    for path in (STATE, CONFIG, RELEASES):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    key = CONFIG / 'owner-key'
    if not key.exists():
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write('dvc_owner_' + secrets.token_urlsafe(48) + '\n')
    if key.is_symlink() or key.stat().st_mode & 0o077:
        raise ValueError('Owner key must be private, regular and non-symlink')
    release = RELEASES / sha
    release.mkdir(exist_ok=True, mode=0o700)
    for name in ('public_mcp_oauth.py', 'public_mcp_owner.py', 'public_mcp_transport.py', 'public_mcp_server.py'):
        shutil.copy2(repo / 'runtime' / name, release / name)
    unit = Path('/home/dev/.config/systemd/user/devcoveer-public-mcp.service')
    if unit.exists():
        raise ValueError('Service already exists; use a reviewed upgrade/rollback')
    unit.write_text(f'''[Unit]
Description=DevCoveer public OAuth MCP (Grok and other MCP clients)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/dev
Environment=DEVCOVEER_PUBLIC_ISSUER={ISSUER}
Environment=DEVCOVEER_PUBLIC_STATE={STATE}
Environment=DEVCOVEER_PUBLIC_OWNER_KEY_FILE={key}
Environment=DEVCOVEER_PUBLIC_PORT=7320
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={PYTHON} {release}/public_mcp_server.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
UMask=0077
TimeoutStopSec=30

[Install]
WantedBy=default.target
''')
    original = EDGE.read_text()
    candidate = render_edge(original)
    (evidence / 'nginx-before.conf').write_text(original)
    (evidence / 'nginx-candidate.conf').write_text(candidate)
    run(['systemctl', '--user', 'daemon-reload'])
    try:
        run(['systemctl', '--user', 'enable', '--now', unit.name])
        # Keep public ingress closed until the new backend answers health.
        run(['curl', '-fsS', '--retry', '8', '--retry-connrefused', '--retry-delay', '1',
             '--max-time', '3', '-H', 'Host: identity.kenigevents.ru', 'http://127.0.0.1:7320/healthz'])
        if EDGE.read_text() != original:
            raise ValueError('Edge changed during installation')
        write_bound_file(EDGE, candidate)
        run(['docker', 'exec', CONTAINER, 'nginx', '-t'])
        run(['docker', 'exec', CONTAINER, 'nginx', '-s', 'reload'])
    except BaseException:
        write_bound_file(EDGE, original)
        run(['docker', 'exec', CONTAINER, 'nginx', '-t'])
        run(['docker', 'exec', CONTAINER, 'nginx', '-s', 'reload'])
        run(['systemctl', '--user', 'disable', '--now', unit.name])
        unit.unlink()
        run(['systemctl', '--user', 'daemon-reload'])
        raise
    receipt = dict(source_sha=sha, issuer=ISSUER, mcp_url=ISSUER+'/mcp/',
                   owner_key_file=str(key), service=unit.name,
                   nginx_before_sha256=hashlib.sha256(original.encode()).hexdigest(),
                   nginx_after_sha256=hashlib.sha256(candidate.encode()).hexdigest(),
                   openai_tunnel_action='none')
    (evidence / 'deployment.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args()
    install(Path(__file__).resolve().parents[1], args.evidence)
