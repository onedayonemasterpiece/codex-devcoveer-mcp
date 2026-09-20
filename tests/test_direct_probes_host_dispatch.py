from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME))

import direct_probes as dp
import direct_probe_host_services as host_services


def run(coro):
    return asyncio.run(coro)


def test_service_and_listeners_dispatch_have_required_helpers(monkeypatch):
    binding = {
        "services": {
            "svc": {
                "unit": "fixture.service",
                "scope": "user",
                "ports": [1234],
            }
        }
    }

    async def fake_systemctl_show(cfg):
        return {"unit": cfg["unit"], "MainPID": "321"}

    async def fake_listeners(cfg):
        return {
            "unit": cfg["unit"],
            "main_pid": 321,
            "registered_ports": [1234],
            "listeners": [],
            "complete": True,
        }

    monkeypatch.setattr(dp, "_systemctl_show", fake_systemctl_show)
    monkeypatch.setattr(dp, "_listeners", fake_listeners)

    service, service_source = run(
        dp._one_host(binding, {"id": "service", "kind": "service", "service": "svc"})
    )
    listeners, listeners_source = run(
        dp._one_host(binding, {"id": "listeners", "kind": "listeners", "service": "svc"})
    )

    assert service["alias"] == "svc"
    assert service_source == "systemd"
    assert listeners["alias"] == "svc"
    assert listeners["registered_ports"] == [1234]
    assert listeners_source == "systemd+/proc-sockets"


def test_user_systemctl_receives_fixed_user_bus_environment(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_run_process(argv, *, env, timeout, output_limit):
        captured["argv"] = argv
        captured["env"] = env
        return dp.ProcessResult(
            b"LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=321\n",
            b"",
            0,
            False,
            1,
        )

    monkeypatch.setattr(host_services, "_run_process", fake_run_process)
    data = run(host_services._systemctl_show({"unit": "fixture.service", "scope": "user"}))

    runtime_dir = f"/run/user/{os.getuid()}"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["XDG_RUNTIME_DIR"] == runtime_dir
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime_dir}/bus"
    assert captured["argv"][1] == "--user"
    assert data["ActiveState"] == "active"


def test_systemctl_show_allows_explicit_safe_extra_properties(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_run_process(argv, *, env, timeout, output_limit):
        captured["argv"] = argv
        return dp.ProcessResult(
            b"LoadState=loaded\nExecStart={ path=/usr/bin/example ; argv[]=/usr/bin/example --flag ; }\nProtectHome=read-only\n",
            b"",
            0,
            False,
            1,
        )

    monkeypatch.setattr(host_services, "_run_process", fake_run_process)
    data = run(host_services._systemctl_show({
        "unit": "fixture.service",
        "scope": "user",
        "show_properties": ["ExecStart", "ProtectHome"],
    }))

    property_arg = next(item for item in captured["argv"] if item.startswith("--property="))
    assert "ExecStart" in property_arg and "ProtectHome" in property_arg
    assert data["ProtectHome"] == "read-only"


def test_system_scope_does_not_receive_user_bus_environment():
    env = host_services._systemd_env("system")
    assert "XDG_RUNTIME_DIR" not in env
    assert "DBUS_SESSION_BUS_ADDRESS" not in env


def test_service_list_and_journal_use_fixed_systemd_argv(monkeypatch):
    calls = []

    async def fake_run_process(argv, *, env, timeout, output_limit):
        calls.append(argv)
        if "list-units" in argv:
            return dp.ProcessResult(b"fixture.service loaded active running Fixture service\n", b"", 0, False, 1)
        return dp.ProcessResult(b"TOKEN=hidden\nnormal line\n", b"", 0, False, 1)

    monkeypatch.setattr(host_services, "_run_process", fake_run_process)
    listed = run(host_services._service_list("user", "fixture", 10))
    journal = run(host_services._journal_unit("fixture.service", "user", 20, 60))
    assert listed["services"][0]["unit"] == "fixture.service"
    assert "hidden" not in journal["text"] and journal["redacted"] is True
    assert calls[0][0:2] == ["/usr/bin/systemctl", "--user"]
    assert calls[1][0:2] == ["/usr/bin/journalctl", "--user"]
