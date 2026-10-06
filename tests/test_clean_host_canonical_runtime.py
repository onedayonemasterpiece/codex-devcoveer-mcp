from __future__ import annotations

import hashlib
import json
import subprocess
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "historical" / "3.6.1" / "source"
INSTALLER = ROOT / "scripts" / "install-current-canonical-runtime.sh"
BUILDER = ROOT / "scripts" / "build-clean-host-runtime.py"

EXPECTED = {
    "codex-chatgpt-bridge.py": "73756c81b852cb183943c880d5918faadeee25ea9b034504a80df9f5f05d2107",
    "opencode_backend.py": "56699a39b2d635cdc397774cc25f122b119e7794599b0d50776c7596e6c9af84",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_controlled_baseline_is_complete_and_pinned() -> None:
    for name, expected in EXPECTED.items():
        path = BASE / name
        assert path.is_file()
        assert sha256(path) == expected
    assert (BASE / "README.md").is_file()
    assert (BASE / "run-codex-mcp").is_file()


def test_clean_host_installer_does_not_use_bootstrap_as_source() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert 'SOURCE_REF="${DEVCOVEER_SOURCE_REF:-origin/main}"' in source
    assert 'git -C "$WORK_REPO" archive "$SOURCE_SHA"' in source
    assert 'build-clean-host-runtime.py" "$CAND"' in source
    assert 'cp -a "$BOOT" "$CAND"' not in source
    assert 'git -C "$WORK_REPO" archive "$SOURCE_SHA"' in source


def test_clean_host_installer_accepts_probes_before_tunnel_switch() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    required_tools = (
        "consult_model",
        "project_probe", "host_probe", "project_action", "host_action",
        "list_models", "start_task", "read_task",
        "stage_source_handoff", "cleanup_source_handoff",
        "street_story_materialize_release", "street_story_deploy_user_service",
        "street_story_publish_edge_route", "projects_hub_publish_edge_route",
        "street_story_service", "street_story_deployment_receipt",
    )
    for name in required_tools:
        assert name in source
    assert "=== canonical wrapper acceptance ===" in source
    assert "systemctl --user restart codex-devserver-tunnel.service" in source


def test_clean_host_installer_runs_normally_as_dev_without_sudo() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert 'ERROR: run as root' not in source
    assert 'sudo ' not in source
    assert 'CURRENT_UID="$(id -u)"' in source
    assert '[ "$CURRENT_UID" -eq "$DEV_UID" ]' in source
    assert 'systemctl --user restart codex-devserver-tunnel.service' in source


def test_clean_host_installer_is_executable() -> None:
    mode = INSTALLER.stat().st_mode
    assert mode & stat.S_IXUSR


def test_clean_host_builder_materializes_current_tools_and_policy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "runtime"
        completed = subprocess.run(
            [sys.executable, str(BUILDER), str(out)],
            text=True, capture_output=True, check=False,
        )
        assert completed.returncode == 0, completed.stderr
        bridge = (out / "codex-chatgpt-bridge.py").read_text(encoding="utf-8")
        start_tool_begin = bridge.index('types.Tool(name="start_task"')
        start_tool_end = bridge.index("input_schema=START_TASK_INPUT_SCHEMA", start_tool_begin)
        start_task_description = bridge[start_tool_begin:start_tool_end]
        for routing_term in (
            "direct_ops_v2",
            "operation=describe",
            "project_action",
            "host_action",
            "run_tool",
            "job_start",
            "job_status",
            "python_env=auto",
            "git_fetch",
            "github_workflow_dispatch",
            "github_run_cancel",
            "reasoning beyond typed operations",
            "instructions", "web_preflight", "CHECK_BASE/CHECK_OUTPUT",
            "workspaceContext", "worktree_list", "expected_head", "existing worktree",
        ):
            assert routing_term in start_task_description
        assert 'name="direct_ops_v2"' in bridge
        direct_begin = bridge.index('types.Tool(name="direct_ops_v2"')
        direct_end = bridge.index('types.Tool(name="project_probe"', direct_begin)
        direct_tool = bridge[direct_begin:direct_end]
        assert "read_only_hint=False" in direct_tool
        assert "destructive_hint=True" in direct_tool
        assert (out / "reuse_quality.py").is_file()
        assert (out / "reuse-catalog.yaml").is_file()
        assert "reuse_prompt as _reuse_prompt" in bridge
        reuse_module = (out / "reuse_quality.py").read_text(encoding="utf-8")
        assert "UNTRUSTED ADVISORY DATA" in reuse_module
        assert "workspace_context(entry.path)" in bridge
        assert "workspaceContext" in bridge
        workspace_module = (out / "workspace_selection.py").read_text(encoding="utf-8")
        assert '"relatedWorktrees"' in workspace_module
        assert '"continuationCheckRequired"' in workspace_module
        assert "stage_source_handoff isolates a ChatGPT-authored patch; it does not select an existing worktree" in start_task_description
        assert 'name="project_probe"' in bridge
        assert 'name="host_probe"' in bridge
        assert 'name="project_action"' in bridge
        assert 'name="host_action"' in bridge
        assert 'name="consult_model"' in bridge
        assert (out / "model_consult.py").is_file()
        assert 'reason = "adaptive_opencode_default"' in bridge
        assert 'eligible = [m for m in _routing_candidates' in bridge
        assert 'name="get_task_evidence"' in bridge
        assert 'name="record_task_review"' in bridge
        assert 'BRIDGE_VERSION = "3.35.1-nvidia-multi-source"' in bridge
        assert 'include_messages=(detail != "summary")' in bridge
        assert '"inspectionStatus":"metadata_only"' in bridge
        assert "Task is running. No message history was fetched" in bridge
        assert "_maybe_auto_groq_fallback" in bridge
        assert "EVIDENCE_STORE" not in bridge
        assert "_BASE_READ_OPENCODE_TASK(record, detail=detail)" in bridge
        assert 'payload["history"] = inspected.get("messages", [])' in bridge
        assert "_BASE_READ_OPENCODE_TASK = _read_opencode_task" in bridge
        assert "_EVIDENCE_WATCHERS: dict[str, asyncio.Task[Any]] = {}" in bridge
        assert "_EVIDENCE_READ_LOCKS: dict[str, asyncio.Lock] = {}" in bridge
        assert bridge.index("_BASE_READ_OPENCODE_TASK = _read_opencode_task") < bridge.index(
            "_EVIDENCE_WATCHERS: dict[str, asyncio.Task[Any]] = {}"
        ) < bridge.index("def _watch_opencode_evidence")
        assert 'name="stage_source_handoff"' in bridge
        assert 'name="cleanup_source_handoff"' in bridge
        assert "source_handoff_list" in (out / "direct_probe_core.py").read_text(encoding="utf-8")
        assert "source_handoff_gc" in (out / "direct_actions.py").read_text(encoding="utf-8")
        assert "requirements_context" in (out / "direct_probe_core.py").read_text(encoding="utf-8")
        assert "requirements_update" in (out / "direct_actions.py").read_text(encoding="utf-8")
        assert (out / "requirements_guard.py").is_file()
        assert "opportunistically reclaims stale cleanup-safe handoffs" in bridge
        assert "explicit user requirement-change intent" in start_task_description
        assert "semantic typed operation" in start_task_description
        assert "Batch independent read-only checks" in start_task_description
        assert '_requirements_prompt(project.path) + _reuse_prompt(project.path) + _first_prompt' in bridge
        assert 'name="street_story_materialize_release"' in bridge
        assert 'name="street_story_deploy_user_service"' in bridge
        assert 'name="street_story_publish_edge_route"' in bridge
        assert 'name="projects_hub_publish_edge_route"' in bridge
        assert 'name="street_story_service"' in bridge
        assert 'name="street_story_deployment_receipt"' in bridge
        assert "_CHATGPT_OPERATING_REMINDER_V2" in bridge
        assert "my-browser-bridge directly" in bridge
        assert "registered project Playwright test/build/webServer profile" in bridge
        assert 'raw_hint.startswith("handoff:")' in bridge
        assert 'on_subscriptions_listen=tool_refresh_listen_handler' in bridge
        assert 'NotificationOptions(tools_changed=True)' in bridge
        assert 'send_tool_list_changed()' in bridge
        assert 'call-time tools/list_changed notification failed' in bridge
        assert 'session_key = id(_ctx.session)' in bridge
        assert '@direct_ops_v2' in (out / "direct_actions.py").read_text(encoding="utf-8")
        gateway = (out / "direct_ops_gateway.py").read_text(encoding="utf-8")
        assert "def _native_operation_schema(" in gateway
        assert 'if key not in native_properties:' in gateway
        assert 'if "request_key" not in native_properties:' in gateway
        backend = (out / "opencode_backend.py").read_text(encoding="utf-8")
        assert "requirements_prompt(Path(directory))" in backend
        assert "boundary + requirements + reuse + prompt" in backend
        assert "reuse_prompt(Path(directory))" in backend
        assert 'headers["x-opencode-directory"]' in backend
        assert '"User-Agent": "opencode/" + (self._server_version or "unknown")' in backend
        assert 'await self.request("GET", "/config", directory=directory' in backend
        assert '"agent": "plan" if access == "read" else "build"' in backend
        assert '"tools": tools' not in backend
        assert '"title": title[:200]' not in backend
        for name in (
            "direct_web_context.py", "workspace_selection.py", "direct_probes.py", "direct_actions.py", "direct_probe_disk_forensics.py", "direct_ops2.py", "direct_ops2_host.py", "direct_ops2_worktrees.py", "direct_jobs.py", "direct_job_runner.py", "direct_github_actions.py", "direct_ops_gateway.py", "direct_tool_catalog.py",
            "direct_probe_config.json", "opencode_backend.py",
            "source_handoff.py", "requirements_guard.py", "reuse_quality.py", "street_story_operator.py", "projects_hub_edge_operator.py", "nvidia_council.py",
        ):
            assert (out / name).is_file()


def test_direct_action_profiles_are_registered_for_mcp_rollout() -> None:
    config = json.loads(
        (ROOT / "runtime" / "direct_probe_config.json").read_text(encoding="utf-8")
    )
    binding = config["projects"]["codex-devcoveer-mcp"]
    actions = binding["project_actions"]
    assert actions["tests"]["argv"] == ["/usr/bin/python3", "-m", "pytest", "-q"]
    assert actions["tests"]["allow_handoff"] is True
    assert actions["canonical_install"]["argv"] == [
        "/usr/bin/bash", "scripts/install-current-canonical-runtime.sh"
    ]
    assert actions["canonical_install"]["env"]["DEVCOVEER_SKIP_TUNNEL_RESTART"] == "1"


def test_direct_rollout_defers_restart_to_host_action() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert 'DEVCOVEER_SKIP_TUNNEL_RESTART' in source
    assert 'tunnel_restart=DEFERRED' in source
    assert 'systemd-run --user --quiet --collect' not in source
    assert 'tunnel_restart_unit=' not in source
    assert '"project_action","host_action"' in source


def test_my_browser_bridge_is_registered_for_host_observability() -> None:
    config = json.loads(
        (ROOT / "runtime" / "direct_probe_config.json").read_text(encoding="utf-8")
    )
    binding = config["projects"]["my-browser-bridge"]
    assert binding["services"]["browser"]["unit"] == "my-browser-bridge.service"
    assert binding["services"]["browser"]["ports"] == [7311]
    assert binding["services"]["mcp_tunnel"]["unit"] == "my-browser-bridge-tunnel.service"
    assert binding["health"]["browser_health"]["url"] == "http://127.0.0.1:7311/healthz"
    assert binding["health"]["browser_ready"]["url"] == "http://127.0.0.1:7311/readyz"
    assert binding["runtimes"]["node"]["argv"] == ["/usr/local/bin/node", "--version"]
    assert binding["runtimes"]["playwright"]["argv"][-1] == "--version"


def test_clean_host_builder_source_handoff_transport_contract() -> None:
    source = BUILDER.read_text(encoding="utf-8")
    assert "stage_source_handoff" in source
    assert "cleanup_source_handoff" in source
    assert 'ROOT / "runtime" / "source_handoff.py"' in source
    assert "git diff --cached --binary" in source


def test_clean_host_builder_street_story_deployment_contract() -> None:
    source = BUILDER.read_text(encoding="utf-8")
    for name in (
        "street_story_materialize_release",
        "street_story_deploy_user_service",
        "street_story_publish_edge_route",
        "street_story_service",
        "street_story_deployment_receipt",
    ):
        assert name in source
    assert 'ROOT / "runtime" / "street_story_operator.py"' in source
    operator = (ROOT / "runtime" / "street_story_operator.py").read_text(encoding="utf-8")
    assert 'TARGET_REPOSITORY = "onedayonemasterpiece/street-story"' in operator
    assert 'TARGET_BRANCH = "work/street-story-mvp-20260908"' in operator
    assert 'TARGET_HOSTNAME = "street-story.kenigevents.ru"' in operator
    assert "TARGET_PORT = 8188" in operator
    assert 'TARGET_SERVICE = "street-story.service"' in operator
    assert "requested SHA is not the current Street Story PR branch HEAD" in operator


def test_installer_supports_isolated_checkout_without_mutating_common_main() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    assert 'dirname "${BASH_SOURCE[0]}"' in source
    assert 'WORK_REPO=/home/dev/projects/' not in source
    assert 'test -e "$WORK_REPO/.git"' in source
    assert 'merge-base --is-ancestor "$SOURCE_SHA" origin/main' in source
    assert 'remote get-url origin' in source
    assert '[[ "$SOURCE_ORIGIN" =~ $SOURCE_ORIGIN_RE ]]' in source
    assert 'only --defer-restart is accepted' in source
    assert 'git -C "$WORK_REPO" archive "$SOURCE_SHA"' in source
    for forbidden in ('git reset', 'git stash', 'git checkout', 'git clean'):
        assert forbidden not in source


def test_clean_host_builder_projects_hub_edge_contract() -> None:
    source = BUILDER.read_text(encoding="utf-8")
    assert "projects_hub_publish_edge_route" in source
    assert 'ROOT / "runtime" / "projects_hub_edge_operator.py"' not in source
    operator = (ROOT / "runtime" / "projects_hub_edge_operator.py").read_text(encoding="utf-8")
    assert 'TARGET_HOSTNAME = "projects-hub.kenigevents.ru"' in operator
    assert "TARGET_PORT = 8196" in operator
    assert 'TARGET_SERVICE = "projects-hub.service"' in operator
    assert "public_yandex+loopback_dev" in operator
    assert "LOCAL_MCP_EDGE_PROJECTS_HUB_ENABLED" in operator
    assert "renderer would remove an existing SNI route" in operator
