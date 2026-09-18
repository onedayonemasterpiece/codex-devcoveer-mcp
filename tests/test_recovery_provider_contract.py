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
