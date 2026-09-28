from pathlib import Path

import pytest

from runtime.projects_hub_edge_operator import (
    OperatorError,
    ProjectsHubEdgeOperator,
    TARGET_HOSTNAME,
    TARGET_PORT,
    TARGET_SERVICE,
)


def operator(tmp_path: Path) -> ProjectsHubEdgeOperator:
    return ProjectsHubEdgeOperator(
        state_root=tmp_path / "operator-state",
        current_link=tmp_path / "current",
        releases_root=tmp_path / "releases",
        vpn_root=tmp_path / "vpn",
        certbot_root=tmp_path / "certbot",
        yc_binary=tmp_path / "yc",
    )


def test_projects_hub_edge_target_is_compile_time_allowlisted() -> None:
    assert TARGET_HOSTNAME == "projects-hub.kenigevents.ru"
    assert TARGET_PORT == 8196
    assert TARGET_SERVICE == "projects-hub.service"


def test_projects_hub_edge_rejects_any_other_target_before_host_action(tmp_path: Path) -> None:
    edge = operator(tmp_path)
    with pytest.raises(OperatorError, match="not allowlisted"):
        edge.publish_edge_route(
            hostname="other.kenigevents.ru",
            loopback_port=8196,
            idempotency_key="projects-hub-edge-test-0001",
        )
    with pytest.raises(OperatorError, match="not allowlisted"):
        edge.publish_edge_route(
            hostname=TARGET_HOSTNAME,
            loopback_port=8197,
            idempotency_key="projects-hub-edge-test-0002",
        )


def test_current_release_requires_exact_sha_symlink(tmp_path: Path) -> None:
    edge = operator(tmp_path)
    release = (tmp_path / "releases" / ("a" * 40))
    release.mkdir(parents=True)
    (tmp_path / "current").symlink_to(release)
    assert edge._current_release_sha() == "a" * 40

    (tmp_path / "current").unlink()
    (tmp_path / "current").symlink_to(tmp_path / "releases" / "mutable")
