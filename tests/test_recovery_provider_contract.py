from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECOVERY = ROOT / "scripts" / "devcoveer-env-recovery.py"
ROUTING = ROOT / "runtime" / "bridge_fragments" / "model_discovery_adaptive.py"
HEALTH = ROOT / "runtime" / "model_health.py"
DOC = ROOT / "docs" / "recovery-provider-and-tunnel-topology.md"


def test_owner_provider_contract_has_three_groq_and_three_gigachat_keys() -> None:
    text = RECOVERY.read_text(encoding="utf-8")

    for name in (
        "GROQ_API_KEY",
        "GROQ_API_KEY2",
        "GROQ_API_KEY3",
        "GIGACHAT_API_KEY",
        "GIGACHAT_API_KEY2",
        "GIGACHAT_API_KEY3",
    ):
        assert f'"{name}"' in text

    assert '"GIGACHAT_CREDENTIALS": "GIGACHAT_API_KEY"' in text


def test_opencode_provider_env_keeps_groq_pool_but_not_upstream_gigachat_secret() -> None:
    text = RECOVERY.read_text(encoding="utf-8")
    start = text.index("def write_opencode_provider_env")
    end = text.index("def install_opencode_provider_dropin", start)
    body = text[start:end]

    assert '"GROQ_API_KEY": ["GROQ_API_KEY", "GROQ_KEY"]' in body
    assert '"GROQ_API_KEY2": ["GROQ_API_KEY2", "GROQ_KEY2"]' in body
    assert '"GROQ_API_KEY3": ["GROQ_API_KEY3", "GROQ_KEY3"]' in body
    assert '"NVIDIA_API_KEY": ["NVIDIA_API_KEY"]' in body

    # GigaChat is consumed by local gpt2giga gateway services; it must not be
    # handed straight to the long-lived OpenCode server.
    assert '"GIGACHAT_API_KEY": [' not in body
    assert '"GIGACHAT_CREDENTIALS": [' not in body


def test_adaptive_routing_knows_all_groq_lanes_and_giga_family() -> None:
    routing = ROUTING.read_text(encoding="utf-8")
    health = HEALTH.read_text(encoding="utf-8")

    assert '{"groq": 0, "groq2": 1, "groq3": 2}' in routing
    assert '"groqKeyOrder": ["groq", "groq2", "groq3"]' in routing
    assert '"policy": "zen_then_groq_then_nvidia_then_metered"' in routing
    assert 'provider.startswith("groq")' in health
    assert 'provider.startswith("giga")' in health


def test_recovery_contract_pins_three_distinct_secure_mcp_tunnels() -> None:
    text = DOC.read_text(encoding="utf-8")

    for value in (
        "DevCoveer Codex",
        "DevCoveer browser-bridge",
        "DevCoveer mailru",
        "codex-devserver-tunnel.service",
        "my-browser-bridge-tunnel.service",
        "mail-mcp-bridge-tunnel.service",
        "http://127.0.0.1:7311/mcp",
        "http://127.0.0.1:7312/mcp",
    ):
        assert value in text


PROVIDER_BOOTSTRAP = ROOT / "scripts" / "bootstrap-devcoveer2-opencode-providers.sh"
TUNNEL_BOOTSTRAP = ROOT / "scripts" / "bootstrap-devcoveer2-mcp-tunnels.sh"
DIRECT_CONFIG = ROOT / "runtime" / "direct_probe_config.json"
DIRECT_INSTALLER = ROOT / "scripts" / "install-direct-probes.py"


def test_provider_bootstrap_restores_live_pool_without_inventing_giga2_giga3() -> None:
    text = PROVIDER_BOOTSTRAP.read_text(encoding="utf-8")

    assert '"apiKey": "{env:GROQ_API_KEY2}"' in text
    assert '"apiKey": "{env:GROQ_API_KEY3}"' in text
    assert "GROQ_API_KEY4" not in text

    assert "gpt2giga-giga1.service" in text
    assert "127.0.0.1:8091" in text
    assert 'providers["giga1"]' in text
    assert 'providers["giga2"]' not in text
    assert 'providers["giga3"]' not in text
    assert "giga_planned_not_enabled=giga2,giga3" in text

    forbidden_controls = [
        ch for ch in text
        if ord(ch) < 32 and ch not in {"\n", "\r", "\t"}
    ]
    assert forbidden_controls == []


def test_tunnel_bootstrap_has_three_distinct_tunnel_contracts() -> None:
    text = TUNNEL_BOOTSTRAP.read_text(encoding="utf-8")

    assert "codex-devserver-tunnel.service" in text
    assert "my-browser-bridge-tunnel.service" in text
    assert "mail-mcp-bridge-tunnel.service" in text
    assert "DEVCOVEER_BROWSER_BRIDGE_TUNNEL_ID" in text
    assert "DEVCOVEER_MAILRU_TUNNEL_ID" in text
    assert "127.0.0.1:7311/mcp" in text
    assert "127.0.0.1:7312/mcp" in text
    assert "127.0.0.1:8081" in text
    assert "127.0.0.1:8082" in text
    assert "127.0.0.1:18080/readyz" in text


def test_direct_host_surface_is_allowlist_only_and_knows_provider_catalog() -> None:
    import json

    config = json.loads(DIRECT_CONFIG.read_text(encoding="utf-8"))
    host = config["projects"]["codex-devcoveer-mcp"]

    assert host["opencode_catalogs"]["main"]["url"] == "http://127.0.0.1:4097/config/providers"
    assert host["services"]["giga1"]["direct_actions"] == ["start", "restart", "enable_start"]
    assert host["services"]["mcp_tunnel"]["direct_actions"] == ["start", "restart", "enable_start"]
    assert host["services"]["opencode"]["direct_actions"] == ["start"]

    installer = DIRECT_INSTALLER.read_text(encoding="utf-8")
    assert 'name="host_action"' in installer
    assert '"host_action": Draft202012Validator(HOST_ACTION_INPUT_SCHEMA)' in installer
    assert '"tools":["project_probe","host_probe","host_action"]' in installer
