from __future__ import annotations

import asyncio
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(RUNTIME))

import direct_probes as dp
import direct_probe_host_runtime as host_runtime
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


def test_system_scope_does_not_receive_user_bus_environment():
    env = host_services._systemd_env("system")
    assert "XDG_RUNTIME_DIR" not in env
    assert "DBUS_SESSION_BUS_ADDRESS" not in env


def test_allowlisted_user_service_action_has_no_generic_shell_surface(monkeypatch):
    calls: list[list[str]] = []

    async def fake_run_process(argv, *, env, timeout, output_limit):
        calls.append(argv)
        if "show" in argv:
            return dp.ProcessResult(
                b"LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=777\n",
                b"",
                0,
                False,
                1,
            )
        return dp.ProcessResult(b"", b"", 0, False, 1)

    monkeypatch.setattr(host_services, "_run_process", fake_run_process)
    binding = {
        "services": {
            "fixture": {
                "unit": "fixture.service",
                "scope": "user",
                "direct_actions": ["start", "restart"],
            }
        }
    }

    data = run(
        host_services._service_action(
            binding,
            {"id": "restart", "service": "fixture", "operation": "restart"},
        )
    )

    assert calls[0] == ["/usr/bin/systemctl", "--user", "restart", "fixture.service"]
    assert data["active_state"] == "active"
    assert data["main_pid"] == 777

    try:
        run(
            host_services._service_action(
                binding,
                {"id": "enable", "service": "fixture", "operation": "enable_start"},
            )
        )
    except dp.ProbeError as exc:
        assert exc.status == "denied"
        assert exc.category == "target_denied"
    else:
        raise AssertionError("non-allowlisted service action was accepted")


def test_system_service_action_is_denied_even_if_binding_requests_it():
    binding = {
        "services": {
            "system": {
                "unit": "fixture.service",
                "scope": "system",
                "direct_actions": ["restart"],
            }
        }
    }
    try:
        run(
            host_services._service_action(
                binding,
                {"id": "restart", "service": "system", "operation": "restart"},
            )
        )
    except dp.ProbeError as exc:
        assert exc.status == "denied"
        assert exc.category == "target_denied"
    else:
        raise AssertionError("system service action must not be reachable directly")


def test_opencode_catalog_probe_returns_metadata_not_credentials(tmp_path):
    username = "devcoveer"
    password = "secret-value-that-must-not-return"
    expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    response = {
        "providers": [
            {
                "id": "groq2",
                "models": {
                    "openai/gpt-oss-120b": {
                        "name": "GPT-OSS-120B",
                        "status": "active",
                        "capabilities": {"reasoning": True, "toolcall": True},
                    }
                },
            },
            {"id": "nvidia", "models": {"model-a": {"status": "active"}}},
        ]
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/config/providers"
            assert self.headers.get("Authorization") == expected
            body = __import__("json").dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        auth = tmp_path / "opencode-server.env"
        auth.write_text(
            f"OPENCODE_SERVER_USERNAME={username}\n"
            f"OPENCODE_SERVER_PASSWORD={password}\n",
            encoding="utf-8",
        )
        binding = {
            "opencode_catalogs": {
                "main": {
                    "url": f"http://127.0.0.1:{server.server_port}/config/providers",
                    "auth_file": str(auth),
                    "username_key": "OPENCODE_SERVER_USERNAME",
                    "password_key": "OPENCODE_SERVER_PASSWORD",
                }
            }
        }
        data = run(
            host_runtime._opencode_catalog(
                binding,
                {"id": "catalog", "kind": "opencode_catalog", "catalog": "main", "provider": "groq2"},
            )
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)

    assert data["catalog"] == "main"
    assert data["provider_filter"] == "groq2"
    assert data["models"] == [
        {
            "id": "openai/gpt-oss-120b",
            "name": "GPT-OSS-120B",
            "status": "active",
            "reasoning": True,
            "toolcall": True,
        }
    ]
    encoded = repr(data)
    assert username not in encoded
    assert password not in encoded
    assert data["values_returned"] is False
