import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_projects_hub_devcoveer_runtime_is_registered_and_bounded():
    config = json.loads((ROOT / "runtime" / "direct_probe_config.json").read_text(encoding="utf-8"))
    binding = config["projects"]["projects-hub"]

    assert binding["github_repo"] == "onedayonemasterpiece/projects-hub"
    backend = binding["services"]["backend"]
    assert backend["unit"] == "projects-hub.service"
    assert backend["scope"] == "user"
    assert backend["ports"] == [8196]
    assert backend["working_directory"] == "/home/dev/.local/share/projects-hub/current/source"

    assert binding["logs"]["backend"] == {"type": "journal", "service": "backend"}
    assert binding["health"]["backend"]["url"] == "http://127.0.0.1:8196/healthz"
    assert binding["roots"]["releases"] == "/home/dev/.local/share/projects-hub/releases"
    assert binding["roots"]["runtime_state"] == "/home/dev/.local/state/projects-hub"

    service_keys = set(binding["config_presence"]["service_env"]["keys"])
    assert "PROJECTS_HUB_SESSION_SECRET_FILE" in service_keys
    assert "PROJECTS_HUB_SESSION_SECRET" not in service_keys

    provider_keys = set(binding["config_presence"]["provider_env"]["keys"])
    assert "GOOGLE_API_KEY4" in provider_keys
    assert not {"GOOGLE_API_KEY", "GOOGLE_API_KEY2", "GOOGLE_API_KEY3", "GOOGLE_API_KEY5"} & provider_keys
