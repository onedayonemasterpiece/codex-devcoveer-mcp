from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

import runtime.street_story_operator as street_story_operator
from runtime.street_story_operator import (
    OperatorError,
    StreetStoryOperator,
    TARGET_BRANCH,
    TARGET_HOSTNAME,
    TARGET_PORT,
    TARGET_REPOSITORY,
    TARGET_SERVICE,
)


def _operator(tmp_path: Path) -> StreetStoryOperator:
    return StreetStoryOperator(
        state_root=tmp_path / "operator-state",
        project_root=tmp_path / "street-story",
        releases_root=tmp_path / "releases",
        current_link=tmp_path / "current",
        app_state_root=tmp_path / "app-state",
        vpn_root=tmp_path / "vpn",
        user_unit_root=tmp_path / "units",
        certbot_root=tmp_path / "certbot",
        vibe_source=tmp_path / "vibe",
        vibe_py=tmp_path / "vibe-python",
        vibe_db=tmp_path / "vibe.sqlite3",
        vibe_owner_token_file=tmp_path / "owner-token",
        host_env=tmp_path / "host.env",
    )


def test_operator_targets_are_compile_time_allowlisted() -> None:
    assert TARGET_REPOSITORY == "onedayonemasterpiece/street-story"
    assert TARGET_BRANCH == "work/street-story-mvp-20260908"
    assert TARGET_HOSTNAME == "street-story.kenigevents.ru"
    assert TARGET_PORT == 8188
    assert TARGET_SERVICE == "street-story.service"


def test_source_gate_accepts_only_current_remote_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    operator = _operator(tmp_path)
    current_sha = "a" * 40
    tree = "b" * 40
    monkeypatch.setattr(operator, "_remote_head_and_tree", lambda: (current_sha, tree))

    assert operator._verify_approved_source(current_sha) == tree


def test_source_gate_rejects_non_current_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    operator = _operator(tmp_path)
    monkeypatch.setattr(operator, "_remote_head_and_tree", lambda: ("a" * 40, "b" * 40))

    with pytest.raises(OperatorError, match="not the current Street Story PR branch HEAD"):
        operator._verify_approved_source("c" * 40)


def test_live_token_is_host_generated_private_reused_and_never_returned_by_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = _operator(tmp_path)
    token_path = tmp_path / "app-state" / "device-token"
    monkeypatch.setattr(street_story_operator, "DEVICE_TOKEN_FILE", token_path)
    monkeypatch.setattr(street_story_operator, "GH_BINARY", Path("/bin/true"))

    first = operator._ensure_live_token()
    second = operator._ensure_live_token()

    assert first == second
    assert len(first) >= 32
    assert token_path.read_text(encoding="utf-8").strip() == first
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_python312_prefers_healthy_bridge_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = _operator(tmp_path)
    bridge = tmp_path / "bridge-python"
    bridge.touch()
    bridge.chmod(0o700)
    monkeypatch.setattr(street_story_operator, "BRIDGE_PYTHON", bridge)
    monkeypatch.setattr(
        street_story_operator.shutil, "which", lambda name: "/broken/python3.12"
    )

    assert operator._python312() == str(bridge)


def test_vibe_principal_name_avoids_existing_ids(tmp_path: Path) -> None:
    operator = _operator(tmp_path)
    with sqlite3.connect(operator.vibe_db) as db:
        db.execute("CREATE TABLE principals(id TEXT PRIMARY KEY)")
        db.executemany(
            "INSERT INTO principals(id) VALUES(?)",
            [("street-story-runtime-abc",), ("street-story-runtime-abc-2",)],
        )

    assert operator._choose_vibe_principal("street-story-runtime-abc") == "street-story-runtime-abc-3"


def test_vibe_preview_preflight_requires_supported_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = _operator(tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_request(token: str, method: str, path: str, **kwargs):
        assert token == "t" * 40
        calls.append((method, path))
        if path == "/v1/publications":
            return {"operation_id": "op_preview"}
        if path == "/v1/operations/op_preview":
            return {
                "receipts": [
                    {"operation_id": "op_preview", "operation_complete": True, "state": "verified"}
                ]
            }
        if path == "/v1/bootstrap":
            return {
                "capabilities": [
                    {
                        "destination": "lovekenig_tg",
                        "operation": "publish",
                        "surface": "post",
                        "status": "supported",
                        "reason": "preview verified",
                    }
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(operator, "_vibe_request", fake_request)

    result = operator._vibe_preview_preflight("t" * 40, "a" * 40)

    assert result["alias"] == "lovekenig_tg"
    assert result["status"] == "supported"
    assert calls == [
        ("POST", "/v1/publications"),
        ("GET", "/v1/operations/op_preview"),
        ("GET", "/v1/bootstrap"),
    ]


def test_edge_target_rejects_arbitrary_hostname_before_host_access(tmp_path: Path) -> None:
    operator = _operator(tmp_path)
    with pytest.raises(OperatorError, match="edge target is not allowlisted"):
        operator.publish_edge_route(
            hostname="example.invalid",
            loopback_port=TARGET_PORT,
            idempotency_key="street-story-edge-test-001",
        )


def test_edge_target_rejects_wrong_loopback_port_before_host_access(tmp_path: Path) -> None:
    operator = _operator(tmp_path)
    with pytest.raises(OperatorError, match="edge target is not allowlisted"):
        operator.publish_edge_route(
            hostname=TARGET_HOSTNAME,
            loopback_port=8080,
            idempotency_key="street-story-edge-test-002",
        )
