from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME))

import direct_probes as dp


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
