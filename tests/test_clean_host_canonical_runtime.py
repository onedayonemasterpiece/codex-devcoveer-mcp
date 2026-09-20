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
        "project_probe", "host_probe", "project_action", "host_action",
        "list_models", "start_task", "read_task",
        "stage_source_handoff", "cleanup_source_handoff",
        "street_story_materialize_release", "street_story_deploy_user_service",
        "street_story_publish_edge_route", "street_story_service",
        "street_story_deployment_receipt",
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
        assert 'name="direct_ops_v2"' in bridge
        assert 'name="project_probe"' in bridge
        assert 'name="host_probe"' in bridge
        assert 'name="project_action"' in bridge
        assert 'name="host_action"' in bridge
        assert 'reason = "adaptive_opencode_default"' in bridge
        assert 'eligible = [m for m in _routing_candidates' in bridge
        assert 'name="get_task_evidence"' in bridge
        assert 'name="record_task_review"' in bridge
        assert 'BRIDGE_VERSION = "3.23.0-direct-ops2"' in bridge
        assert 'name="stage_source_handoff"' in bridge
        assert 'name="cleanup_source_handoff"' in bridge
        assert 'name="street_story_materialize_release"' in bridge
        assert 'name="street_story_deploy_user_service"' in bridge
        assert 'name="street_story_publish_edge_route"' in bridge
        assert 'name="street_story_service"' in bridge
        assert 'name="street_story_deployment_receipt"' in bridge
        assert "_CHATGPT_OPERATING_REMINDER_V2" in bridge
        assert "my-browser-bridge directly" in bridge
        assert "registered project Playwright test/build/webServer profile" in bridge
        assert 'raw_hint.startswith("handoff:")' in bridge
        assert 'on_subscriptions_listen=tool_refresh_listen_handler' in bridge
        assert 'NotificationOptions(tools_changed=True)' in bridge
        assert 'send_tool_list_changed()' in bridge
        assert '@direct_ops_v2' in (out / "direct_actions.py").read_text(encoding="utf-8")
        backend = (out / "opencode_backend.py").read_text(encoding="utf-8")
        assert 'headers["x-opencode-directory"]' in backend
        assert '"User-Agent": "opencode/" + (self._server_version or "unknown")' in backend
        assert 'await self.request("GET", "/config", directory=directory' in backend
        assert '"agent": "plan" if access == "read" else "build"' in backend
        assert '"tools": tools' not in backend
        assert '"title": title[:200]' not in backend
        for name in (
            "direct_probes.py", "direct_actions.py", "direct_probe_disk_forensics.py", "direct_ops2.py", "direct_ops2_host.py", "direct_ops2_worktrees.py", "direct_ops2_android.py", "direct_jobs.py", "direct_job_runner.py", "direct_github_actions.py", "direct_ops_gateway.py", "direct_tool_catalog.py",
            "direct_probe_config.json", "opencode_backend.py",
            "source_handoff.py", "street_story_operator.py",
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
