import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import direct_probe_host_runtime as runtime
from direct_probe_core import ProbeError

KEY = 'RKB_VIBEPUBLISH_GRANT_FILE'
ROOT = '/home/dev/.local/state/regional-knowledge-base'


def binding(path, opt_in=True):
    cfg = {'path': str(path), 'keys': [KEY]}
    if opt_in:
        cfg['path_metadata_keys'] = {KEY: ROOT}
    return {'config_presence': {'archive_grant_path': cfg}}


def test_path_metadata_only_opt_in_and_does_not_open_target(tmp_path):
    source = tmp_path / 'service.env'
    source.write_text(f'{KEY}="{ROOT}/nonexistent.json"\nSECRET=do-not-return\n')
    check = {'source': 'archive_grant_path', 'keys': [KEY]}
    old = asyncio.run(runtime._config_metadata(binding(source, False), check))
    assert old['keys'][0]['kind'] == 'opaque'
    result = asyncio.run(runtime._config_metadata(binding(source), check))
    assert result['keys'][0]['path'] == ROOT + '/nonexistent.json'
    assert result['path_metadata_returned'] is True
    assert result['values_returned'] is False
    assert result['hashes_returned'] is False
    assert 'do-not-return' not in json.dumps(result)
    with pytest.raises(ProbeError):
        asyncio.run(runtime._config_metadata(binding(source), {**check, 'keys': ['SECRET']}))


@pytest.mark.parametrize('value', [
    'https://host/token?secret=x', 'secret-token', '../grant.json',
    ROOT + '/../other/grant.json', ROOT + '/./grant.json',
    ROOT + '/grant.json?token=secret', ROOT + '/grant.json#secret',
    '/etc/passwd', ROOT + '-other/grant.json', ROOT + '/grant token.json',
    ROOT + '/grant.json\nSECRET=x', ROOT + '//grant.json',
])
def test_invalid_path_is_opaque(value):
    row = runtime._safe_path_metadata(KEY, value, ROOT)
    assert row['kind'] == 'opaque'
    assert 'path' not in row


def test_missing_and_invalid_opt_in(tmp_path):
    source = tmp_path / 'service.env'
    source.write_text('')
    check = {'source': 'archive_grant_path', 'keys': [KEY]}
    assert asyncio.run(runtime._config_metadata(binding(source), check))['keys'][0]['kind'] == 'missing'
    cfg = binding(source)
    cfg['config_presence']['archive_grant_path']['path_metadata_keys']['SECRET'] = ROOT
    with pytest.raises(ProbeError):
        asyncio.run(runtime._config_metadata(cfg, check))


def test_rkb_registration_is_limited_to_approved_controller_and_existing_metadata():
    config = json.loads((Path(__file__).resolve().parents[1] / 'runtime/direct_probe_config.json').read_text())
    rkb = config['projects']['regional-knowledge-base']
    # Owner approved the controller operational binding on 2026-10-21.
    # Do not silently add release roots, environment values or other services.
    assert set(rkb) == {'config_presence', 'github_repo', 'services', 'logs'}
    assert rkb['github_repo'] == 'onedayonemasterpiece/regional-knowledge-base'
    assert rkb['services'] == {'bge_controller': {
        'unit': 'regional-knowledge-bge-controller.service', 'scope': 'user',
        'safe_environment_keys': [],
    }}
    assert rkb['logs'] == {'bge_controller': {
        'type': 'journal', 'service': 'bge_controller',
    }}
    assert rkb['config_presence'] == {'archive_grant_path': {
        'path': ROOT + '/service.env', 'keys': [KEY],
        'path_metadata_keys': {KEY: ROOT},
    }}
