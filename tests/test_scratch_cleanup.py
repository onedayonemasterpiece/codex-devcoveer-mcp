"""All deletion tests operate only on pytest-owned temporary directories."""
import ast
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('maintenance', ROOT / 'scripts/maintain-artifacts.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / 'artifacts'
    name = '20261001T080000Z-finished-test'
    run = root / 'street-story' / name
    child = run / 'pytest-tmp'
    nested = child / 'pytest-1'
    nested.mkdir(parents=True)
    (nested / 'value').write_bytes(b'1234')
    (child / 'pytest-current').symlink_to('pytest-1')
    (run / 'evidence.png').write_bytes(b'keep')
    monkeypatch.setattr(m, '_is_referenced', lambda *a: False)
    monkeypatch.setattr(m, '_in_use', lambda *a: False)
    old = time.time() - m.MIN_AGE_SECONDS - 100
    for p in list(child.rglob('*')) + [child]:
        os.utime(p, (old, old), follow_symlinks=False)
    return root, tmp_path, name, run, child

def plan(t, **kw):
    root, repo, name, _, _ = t
    return m.scratch_cleanup(root, repo, 'street-story', name, 'pytest-tmp', **kw)

def apply(t, p):
    return plan(t, apply=True, expected_manifest=p['manifest_sha256'], expected_bytes=p['bytes'])

def test_dry_run_then_exact_child_no_follow(tree):
    p = plan(tree)
    assert p['safe_to_delete'] and p['bytes'] == 4
    assert tree[4].exists()
    r = apply(tree, p)
    assert r['status'] == 'removed'
    assert 'free_after_bytes' in r
    assert not tree[4].exists()
    assert (tree[3] / 'evidence.png').read_bytes() == b'keep'

@pytest.mark.parametrize('child', ['..', 'python-deps', '/tmp', 'pytest-tmp/../evidence.png'])
def test_wrong_child_denied(tree, child):
    with pytest.raises(m.UnsafeArtifact):
        m.scratch_cleanup(tree[0], tree[1], 'street-story', tree[2], child)

@pytest.mark.parametrize('change', ['bytes', 'manifest', 'contents', 'root_replace', 'root_missing'])
def test_stale_snapshot_denied(tree, change):
    p = plan(tree)
    if change == 'bytes': p['bytes'] += 1
    if change == 'manifest': p['manifest_sha256'] = '0' * 64
    if change == 'contents': (tree[4] / 'pytest-1/value').write_bytes(b'5678')
    if change == 'root_replace':
        tree[4].rename(tree[3] / 'saved')
        tree[4].mkdir()
    if change == 'root_missing': tree[4].rename(tree[3] / 'saved')
    with pytest.raises((m.UnsafeArtifact, OSError)):
        apply(tree, p)
    assert (tree[3] / 'evidence.png').exists()

@pytest.mark.parametrize('guard', ['active', 'incomplete', 'referenced', 'recent', 'owner', 'mount', 'limit', 'bytes'])
def test_guards_fail_closed(tree, monkeypatch, guard):
    if guard == 'active': monkeypatch.setattr(m, '_in_use', lambda *a: True)
    if guard == 'incomplete':
        def incomplete(*a): raise m.UnsafeArtifact('inaccessible FD')
        monkeypatch.setattr(m, '_in_use', incomplete)
    if guard == 'referenced': monkeypatch.setattr(m, '_is_referenced', lambda *a: True)
    if guard == 'recent': os.utime(tree[4] / 'pytest-1/value', None)
    if guard == 'owner': monkeypatch.setattr(m.os, 'getuid', lambda: 999999)
    if guard == 'mount':
        original = m.os.stat
        def mounted(path, **kw):
            s = original(path, **kw)
            if path == 'value':
                d = {k:getattr(s,k) for k in dir(s) if k.startswith('st_')}
                d['st_dev'] += 1
                return SimpleNamespace(**d)
            return s
        monkeypatch.setattr(m.os, 'stat', mounted)
    if guard == 'limit': monkeypatch.setattr(m, 'MAX_ENTRIES', 2)
    if guard == 'bytes': monkeypatch.setattr(m, 'MAX_SCRATCH_BYTES', 3)
    try:
        p = plan(tree)
    except m.UnsafeArtifact:
        pass
    else:
        assert not p['safe_to_delete']
        with pytest.raises(m.UnsafeArtifact): apply(tree, p)
    assert tree[4].exists()

@pytest.mark.parametrize('kind', ['external_link', 'root_link', 'fifo', 'hardlink', 'protected'])
def test_bad_objects(tree, kind):
    child = tree[4]
    if kind == 'external_link': (child / 'badcurrent').symlink_to(tree[3] / 'evidence.png')
    if kind == 'root_link':
        child.rename(tree[3] / 'saved'); child.symlink_to('saved')
    if kind == 'fifo': os.mkfifo(child / 'pipe')
    if kind == 'hardlink': os.link(tree[3] / 'evidence.png', child / 'hardlink')
    if kind == 'protected': (child / '.env').write_text('fixture')
    with pytest.raises((m.UnsafeArtifact, OSError)): plan(tree)
    assert (tree[3] / 'evidence.png').read_bytes() == b'keep'

def test_real_active_fd_blocks(tree, monkeypatch):
    # Restore the real proc scanner; self-held inspection FDs must not block dry-run.
    fresh = importlib.util.module_from_spec(spec); spec.loader.exec_module(fresh)
    monkeypatch.setattr(m, '_in_use', fresh._in_use)
    with (tree[4] / 'pytest-1/value').open('rb'):
        p = plan(tree)
        assert not p['safe_to_delete']
        assert 'open_by_process' in p['blockers']


def test_typed_host_dispatch_and_operator_gate(tmp_path):
    # Execute the actual dispatcher function in isolation from unrelated adapters.
    source = (ROOT / 'runtime/direct_actions.py').read_text()
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef)
                and n.name == '_host_artifact_maintenance')
    class ProbeError(Exception):
        def __init__(self, code, text, **kw): super().__init__(text)
    calls = []
    async def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(exit_code=2 if '--delete' in argv else 0,
                               stdout=b'{"status":"blocked","partial_cleanup":true}' if '--delete' in argv else b'{"status":"dry_run"}')
    fake_module = tmp_path / 'direct_actions.py'; fake_module.touch()
    (tmp_path / 'maintain-artifacts.py').touch()
    ns = dict(Any=object, Path=Path, ProbeError=ProbeError, re=re, json=json,
              __file__=str(fake_module), _run_process=run,
              _decode=lambda r, _: (r.stdout.decode(), False))
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<dispatcher>', 'exec'), ns)
    action = dict(mode='scratch_cleanup', artifact_project='street-story',
                  run='20261001T080000Z-finished-test', child='pytest-tmp')
    with pytest.raises(ProbeError, match='registered'):
        asyncio.run(ns['_host_artifact_maintenance']('street-story', action))
    asyncio.run(ns['_host_artifact_maintenance']('codex-devcoveer-mcp', action))
    assert calls[-1][-2:] == ['--scratch-child', 'pytest-tmp']
    assert '--delete' not in calls[-1]
    with pytest.raises(ProbeError):
        asyncio.run(ns['_host_artifact_maintenance']('codex-devcoveer-mcp', {**action,'apply':True}))
    result = asyncio.run(ns['_host_artifact_maintenance']('codex-devcoveer-mcp',
        {**action,'apply':True,'expected_bytes':4,'expected_manifest':'a'*64}))
    assert result['status'] == 'blocked' and result['details']['partial_cleanup']
    assert calls[-1][-5:] == ['--delete','--expected-manifest','a'*64,'--expected-bytes','4']

def test_root_mount_denied(tree, monkeypatch):
    original = m._mount_id
    def mount(fd):
        value = original(fd)
        if os.readlink(f'/proc/self/fd/{fd}').endswith('/pytest-tmp'):
            return value + 1
        return value
    monkeypatch.setattr(m, '_mount_id', mount)
    with pytest.raises(m.UnsafeArtifact, match='mount boundary'): plan(tree)

def test_same_device_nested_bind_mount_denied(tree, monkeypatch):
    original = m._mount_id
    def mount(fd):
        value = original(fd)
        if os.readlink(f'/proc/self/fd/{fd}').endswith('/pytest-1'):
            return value + 1
        return value
    monkeypatch.setattr(m, '_mount_id', mount)
    with pytest.raises(m.UnsafeArtifact, match='mount boundary'): plan(tree)

def test_mid_erase_failure_reports_partial_cleanup(tree, monkeypatch):
    p = plan(tree)
    original = m.os.unlink
    count = 0
    def unlink(path, **kw):
        nonlocal count
        count += 1
        if count == 2: raise OSError('injected second-unlink failure')
        return original(path, **kw)
    monkeypatch.setattr(m.os, 'unlink', unlink)
    r = apply(tree, p)
    assert r['status'] == 'blocked' and r['partial_cleanup']
    assert r['removed']['files_or_links'] == 1
    assert (tree[3] / 'evidence.png').read_bytes() == b'keep'

def test_mid_erase_root_move_stops_with_partial_receipt(tree, monkeypatch):
    p = plan(tree)
    original = m.os.unlink
    def unlink(path, **kw):
        result = original(path, **kw)
        tree[4].rename(tree[3] / 'moved-scratch')
        return result
    monkeypatch.setattr(m.os, 'unlink', unlink)
    r = apply(tree, p)
    assert r['status'] == 'blocked' and r['partial_cleanup']
    assert (tree[3] / 'moved-scratch' / 'pytest-current').is_symlink()
    assert (tree[3] / 'evidence.png').exists()

def test_cooperating_cleanup_lock_blocks(tree):
    import fcntl
    fd = os.open(tree[3], os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(m.UnsafeArtifact, match='run lock'): plan(tree)
    finally:
        os.close(fd)


def test_cli_partial_cleanup_is_nonzero(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['maintain-artifacts.py', '--project', 'street-story',
        '--run', '20261001T080000Z-finished-test', '--scratch-child', 'pytest-tmp',
        '--delete', '--expected-manifest', 'a'*64, '--expected-bytes', '4'])
    monkeypatch.setattr(m, 'scratch_cleanup', lambda *a, **kw:
        {'status': 'blocked', 'partial_cleanup': True, 'removed': {'files_or_links': 1}})
    assert m.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result['partial_cleanup'] and result['status'] == 'blocked'
