#!/usr/bin/env python3
"""Build canonical runtime from the checked-in 3.6.1 baseline."""
from __future__ import annotations

import argparse
import hashlib
import py_compile
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "historical" / "3.6.1" / "source"
BASE_SHA = {
    "codex-chatgpt-bridge.py": "73756c81b852cb183943c880d5918faadeee25ea9b034504a80df9f5f05d2107",
    "opencode_backend.py": "56699a39b2d635cdc397774cc25f122b119e7794599b0d50776c7596e6c9af84",
}
DIRECT_FILES = (
    "direct_probes.py", "direct_probe_core.py", "direct_probe_project.py",
    "direct_probe_project_git.py", "direct_probe_project_files.py",
    "direct_probe_project_github.py", "direct_probe_host.py",
    "direct_probe_host_services.py", "direct_probe_host_runtime.py",
    "direct_probe_disk_forensics.py",
)

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"clean-host bridge anchor {label!r}: expected 1, found {count}")
    return source.replace(old, new, 1)

def transform(source: str) -> str:
    source = replace_once(source, 'BRIDGE_VERSION = "3.6.1"',
                          'BRIDGE_VERSION = "3.10.0-clean-host.1"', "version")
    import_anchor = """from opencode_backend import (
    DEFAULT_OPENCODE_MODEL as BACKEND_DEFAULT_OPENCODE_MODEL,
    OpenCodeBackend,
    OpenCodeError,
    TASK_ID_RE,
    TaskRegistry,
)
"""
    direct_import = import_anchor + """from direct_probes import (
    HOST_PROBE_INPUT_SCHEMA,
    PROJECT_PROBE_INPUT_SCHEMA,
    ProbeError,
    run_host_probe as _run_host_probe,
    run_project_probe as _run_project_probe,
)
"""
    source = replace_once(source, import_anchor, direct_import, "direct-probe import")
    tool_anchor = '    types.Tool(name="council_run", title="Run multi-model council",\n'
    tool_block = '''    types.Tool(name="project_probe", title="Read deterministic project facts",
        description=("Read bounded deterministic project facts without starting an agent task. "
            "Use this for Git state, files, hashes, searches, and narrowly scoped CI status."),
        input_schema=PROJECT_PROBE_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
            idempotent_hint=True, open_world_hint=True)),
    types.Tool(name="host_probe", title="Read deterministic host/runtime facts",
        description=("Read bounded facts from server-registered services, listeners, logs, "
            "health endpoints, runtimes, and installed files without starting an agent task."),
        input_schema=HOST_PROBE_INPUT_SCHEMA, output_schema=GENERIC_OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False)),
'''
    source = replace_once(source, tool_anchor, tool_block + tool_anchor, "tool catalog")
    validator_anchor = '    "council_run": Draft202012Validator(COUNCIL_RUN_INPUT_SCHEMA),\n'
    validators = ('    "project_probe": Draft202012Validator(PROJECT_PROBE_INPUT_SCHEMA),\n'
                  '    "host_probe": Draft202012Validator(HOST_PROBE_INPUT_SCHEMA),\n')
    source = replace_once(source, validator_anchor, validators + validator_anchor,
                          "validator catalog")
    old_default = '''    if model is None:
        selected_model, selected_effort = await _resolve_model_selection(
            history, None, reasoning_effort,
        )
        return {"backend": "codex", "provider": "codex", "model": selected_model,
                "reasoningEffort": selected_effort, "reason": "default_codex"}
'''
    new_default = '''    if model is None:
        if reasoning_effort is not None:
            raise _invalid_params("reasoning_effort",
                "reasoning_effort requires provider=codex")
        selected = await OPENCODE_BACKEND.resolve_model(
            provider="groq", model="groq/openai/gpt-oss-120b", directory=project.path,
        )
        _assert_model_not_excluded(selected["selection"])
        return {"backend": "opencode", "provider": selected["provider"],
                "model": selected["nativeModelId"], "selection": selected["selection"],
                "reasoningEffort": None, "reason": "default_opencode"}
'''
    source = replace_once(source, old_default, new_default, "providerless routing")
    handler_anchor = '        if name == "council_run":\n'
    handlers = '''        if name == "project_probe":
            assert project is not None
            try:
                return _tool_result(await _run_project_probe(
                    project.path, project.canonical, arguments["checks"],
                ))
            except ProbeError as exc:
                payload = {"status": exc.status, "errorCategory": exc.category,
                           "message": str(exc)}
                if exc.exit_code is not None:
                    payload["exitCode"] = exc.exit_code
                return _tool_result(payload, error=True)

        if name == "host_probe":
            assert project is not None
            try:
                return _tool_result(await _run_host_probe(
                    project.path, project.canonical, arguments["checks"],
                ))
            except ProbeError as exc:
                payload = {"status": exc.status, "errorCategory": exc.category,
                           "message": str(exc)}
                if exc.exit_code is not None:
                    payload["exitCode"] = exc.exit_code
                return _tool_result(payload, error=True)

'''
    return replace_once(source, handler_anchor, handlers + handler_anchor, "probe dispatch")

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"output directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    for name, expected in BASE_SHA.items():
        observed = sha(BASE / name)
        if observed != expected:
            raise SystemExit(f"baseline SHA mismatch for {name}: {observed}")
    shutil.copytree(BASE, out, dirs_exist_ok=True)
    bridge = out / "codex-chatgpt-bridge.py"
    bridge.write_text(transform(bridge.read_text(encoding="utf-8")), encoding="utf-8")
    bridge.chmod(0o755)
    for name in DIRECT_FILES:
        shutil.copy2(ROOT / "runtime" / name, out / name)
    shutil.copy2(ROOT / "runtime" / "direct_probe_config.json",
                 out / "direct_probe_config.json")
    for path in out.glob("*.py"):
        py_compile.compile(str(path), doraise=True)
    print(f"bridge_sha256={sha(bridge)}")
    print(f"backend_sha256={sha(out / 'opencode_backend.py')}")
    print("clean_host_build=PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
