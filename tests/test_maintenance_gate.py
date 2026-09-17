from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-adaptive-routing-v3.10.py"

spec = importlib.util.spec_from_file_location("adaptive_installer_gate", INSTALLER)
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def _record(
    task_id: str,
    *,
    status: str = "running",
    session: str | None = "ses_ABC123",
    project: str = "some-project",
    title: str = "ordinary task",
    backend: str = "opencode",
) -> dict[str, object]:
    return {
        "taskId": task_id,
        "status": status,
        "session": session,
        "project": project,
        "title": title,
        "backend": backend,
    }


def test_stale_running_registry_record_does_not_block_when_session_is_absent():
    task_id = "dvt_11111111111111111111111111111111"
    active, stale, unverified, excluded = installer.classify_active_records(
        {task_id: _record(task_id)},
        {},
    )
    assert active == []
    assert stale == [task_id]
    assert unverified == []
    assert excluded is None


def test_live_sibling_task_still_blocks_maintenance():
    maintenance = "dvt_22222222222222222222222222222222"
    sibling = "dvt_33333333333333333333333333333333"
    records = {
        maintenance: _record(
            maintenance,
            session="ses_MAINT123",
            project=installer.MAINTENANCE_PROJECT,
            title=f"fixture: {installer.MAINTENANCE_TITLE_MARKER}",
        ),
        sibling: _record(sibling, session="ses_USER456"),
    }
    active, stale, unverified, excluded = installer.classify_active_records(
        records,
        {"ses_MAINT123": "busy", "ses_USER456": "retry"},
        maintenance_task_id=maintenance,
    )
    assert active == [sibling]
    assert stale == []
    assert unverified == []
    assert excluded == maintenance


def test_only_verified_marked_live_maintenance_task_can_self_exclude():
    maintenance = "dvt_44444444444444444444444444444444"
    record = _record(
        maintenance,
        session="ses_MAINT789",
        project=installer.MAINTENANCE_PROJECT,
        title=f"fixture: {installer.MAINTENANCE_TITLE_MARKER}",
    )
    active, stale, unverified, excluded = installer.classify_active_records(
        {maintenance: record},
        {"ses_MAINT789": "busy"},
        maintenance_task_id=maintenance,
    )
    assert active == []
    assert stale == []
    assert unverified == []
    assert excluded == maintenance


@pytest.mark.parametrize(
    ("project", "title", "status"),
    [
        ("wrong-project", f"x {installer.MAINTENANCE_TITLE_MARKER}", "running"),
        (installer.MAINTENANCE_PROJECT, "ordinary task", "running"),
        (installer.MAINTENANCE_PROJECT, f"x {installer.MAINTENANCE_TITLE_MARKER}", "cancelling"),
    ],
)
def test_maintenance_self_exclusion_rejects_wrong_identity(project: str, title: str, status: str):
    maintenance = "dvt_55555555555555555555555555555555"
    record = _record(
        maintenance,
        status=status,
        session="ses_MAINT000",
        project=project,
        title=title,
    )
    with pytest.raises(SystemExit):
        installer.classify_active_records(
            {maintenance: record},
            {"ses_MAINT000": "busy"},
            maintenance_task_id=maintenance,
        )


def test_missing_session_or_non_opencode_record_remains_fail_closed_blocker():
    missing_session = "dvt_66666666666666666666666666666666"
    other_backend = "dvt_77777777777777777777777777777777"
    records = {
        missing_session: _record(missing_session, session=None),
        other_backend: _record(other_backend, backend="codex", session="ses_NATIVE123"),
    }
    active, stale, unverified, excluded = installer.classify_active_records(records, {})
    assert active == [missing_session, other_backend]
    assert stale == []
    assert unverified == [missing_session, other_backend]
    assert excluded is None


def test_idle_session_is_stale_not_live():
    task_id = "dvt_88888888888888888888888888888888"
    active, stale, unverified, excluded = installer.classify_active_records(
        {task_id: _record(task_id, session="ses_IDLE123")},
        {"ses_IDLE123": "idle"},
    )
    assert active == []
    assert stale == [task_id]
    assert unverified == []
    assert excluded is None
