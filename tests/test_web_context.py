"""Web workflow regression tests: local fixtures only, no browser download or website writes."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import direct_actions as actions
import direct_ops_gateway as gateway
import direct_web_context as web
from direct_probe_core import ProbeError


def run(coro):
    return asyncio.run(coro)


def fixture_project(root):
    site = root / 'site'
    site.mkdir()
    (site / 'package.json').write_text(json.dumps({'scripts': {'test:web': 'node check.mjs'},
        'devDependencies': {'playwright': '1.2.3'}}))
    return site


def fixture_playwright(site):
    package = site / 'node_modules/playwright'
    package.mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({'name': 'playwright', 'version': '1.2.3', 'main': 'index.js'}))
    (package / 'index.js').write_text("module.exports={chromium:{executablePath:()=>require('node:path').join(__dirname,'chrome')},firefox:{executablePath:()=>require('node:path').join(__dirname,'firefox')}};")
    (package / 'cli.js').write_text("throw new Error('preflight must not execute installer');")
    return package


def test_new_operations_are_discoverable_without_new_top_level_schema():
    for operation in ('instructions', 'web_preflight'):
        schema = gateway.describe_plane('project_probe', operation)['payload_schema']
        assert 'kind' not in schema['properties'] and 'id' not in schema['properties']
        assert schema['description']
    assert 'enum' not in gateway.DIRECT_OPS_V2_INPUT_SCHEMA['properties']['operation']
    assert 'job_start' in ' '.join(gateway.describe_plane('project_action')['operator_guidance'].values())


@pytest.mark.parametrize('kind', ['run_tool', 'job_start'])
def test_execution_schema_allows_exact_web_flags_and_explains_paths(kind):
    schema = gateway.describe_plane('project_action', kind)['payload_schema']
    keys = schema['properties']['env']['propertyNames']['enum']
    assert {'CHECK_BASE', 'CHECK_OUTPUT'} <= set(keys)
    assert 'project-root-relative' in schema['properties']['path']['description']
    assert 'job_start' in schema['description']
    assert 'API_KEY' not in keys and 'NODE_OPTIONS' not in keys


def test_home_instruction_read_is_exact_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'AGENTS.md').write_text('Policy\npassword=synthetic_value\n')
    result = web.read_instructions(tmp_path, {'scope': 'home'})
    assert result['found'] and result['redacted']
    assert 'synthetic_value' not in result['text']
    assert result['path'] == str(tmp_path / 'AGENTS.md')


def test_home_instructions_cannot_read_arbitrary_relative_path(tmp_path):
    with pytest.raises(ProbeError):
        web.read_instructions(tmp_path, {'scope': 'home', 'cwd': '.ssh'})


def test_instruction_pagination_and_absence(tmp_path):
    assert web.read_instructions(tmp_path, {})['found'] is False
    (tmp_path / 'AGENTS.md').write_text('\n'.join(str(i) for i in range(250)))
    first = web.read_instructions(tmp_path, {})
    assert first['next_start_line'] == 201 and first['end_line'] == 200
    second = web.read_instructions(tmp_path, {'start_line': first['next_start_line']})
    assert second['end_line'] == 250 and second['next_start_line'] is None


def test_nested_instructions_only_read_named_directory(tmp_path):
    (tmp_path / 'site').mkdir()
    (tmp_path / 'site/AGENTS.md').write_text('nested rules')
    assert web.read_instructions(tmp_path, {'cwd': 'site'})['text'] == 'nested rules'
    with pytest.raises(ProbeError):
        web.read_instructions(tmp_path, {'cwd': '../outside'})


def test_instruction_symlink_and_size_are_rejected(tmp_path):
    (tmp_path / 'target').write_text('not instructions')
    (tmp_path / 'AGENTS.md').symlink_to(tmp_path / 'target')
    with pytest.raises(ProbeError):
        web.read_instructions(tmp_path, {})
    (tmp_path / 'AGENTS.md').unlink()
    (tmp_path / 'AGENTS.md').write_bytes(b'x' * (web.MAX_METADATA_BYTES + 1))
    with pytest.raises(ProbeError):
        web.read_instructions(tmp_path, {})


def test_preflight_absent_package_json_is_not_browser_missing(tmp_path):
    with pytest.raises(ProbeError) as exc:
        run(web.web_preflight(tmp_path, {}))
    assert exc.value.category == 'not_found'


def test_preflight_missing_dependency_does_not_install(tmp_path):
    site = fixture_project(tmp_path)
    data = run(web.web_preflight(tmp_path, {'cwd': 'site'}))
    assert data['playwright']['status'] == 'dependency_missing'
    assert data['scripts'] == ['test:web']
    assert 'install_action' not in data
    assert not (site / 'node_modules').exists()


def test_preflight_uses_pinned_local_cli_and_reports_missing_browser(tmp_path):
    site = fixture_project(tmp_path)
    package = fixture_playwright(site)
    data = run(web.web_preflight(tmp_path, {'cwd': 'site'}))
    assert data['playwright']['version'] == '1.2.3'
    assert not data['playwright']['executable_present'] and not data['launch_verified']
    install = data['install_action']
    assert install['operation'] == 'job_start'
    assert install['payload']['path'] == 'site/node_modules/playwright/cli.js'
    assert install['payload']['cwd'] == 'site'
    assert install['payload']['args'] == ['install', 'chromium']
    assert not (package / 'chrome').exists()


def test_binary_presence_is_not_launch_or_headless_acceptance(tmp_path):
    site = fixture_project(tmp_path)
    package = fixture_playwright(site)
    (package / 'chrome').write_text('not a browser')
    (package / 'chrome').chmod(0o700)
    data = run(web.web_preflight(tmp_path, {'cwd': 'site'}))
    assert data['playwright']['executable_present']
    assert data['launch_verified'] is False
    assert 'headless' in data['playwright']['note']


def test_no_dependency_resolution_from_another_checkout(tmp_path):
    parent = tmp_path / 'outer'
    parent.mkdir()
    fixture_playwright(parent)
    project = parent / 'project'
    project.mkdir()
    fixture_project(project)
    data = run(web.web_preflight(project, {'cwd': 'site'}))
    assert data['playwright']['status'] == 'dependency_outside_project'
    assert 'install_action' not in data


def test_web_env_roundtrips_in_existing_runner_without_shell(tmp_path):
    (tmp_path / 'check.mjs').write_text("console.log(JSON.stringify({base:process.env.CHECK_BASE,out:process.env.CHECK_OUTPUT}));")
    flags = {'CHECK_BASE': 'http://127.0.0.1:4321/review/', 'CHECK_OUTPUT': '/home/dev/artifacts/example/check'}
    data = run(actions._run_tool(tmp_path, {'tool': 'node_script', 'path': 'check.mjs', 'env': flags, 'timeout_seconds': 10}))
    assert data['exit_code'] == 0
    assert json.loads(data['stdout']) == {'base': flags['CHECK_BASE'], 'out': flags['CHECK_OUTPUT']}


@pytest.mark.parametrize('name', ['API_KEY', 'NODE_OPTIONS', 'HOME', 'PATH'])
def test_env_widening_does_not_admit_secrets_or_loader_overrides(name):
    with pytest.raises(ProbeError):
        actions._tool_env({'env': {name: 'dummy'}}, None)


def test_invalid_package_shape_is_reported(tmp_path):
    (tmp_path / 'package.json').write_text('[]')
    with pytest.raises(ProbeError) as exc:
        run(web.web_preflight(tmp_path, {}))
    assert exc.value.category == 'invalid_package'
