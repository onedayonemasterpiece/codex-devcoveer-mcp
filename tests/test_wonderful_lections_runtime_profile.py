from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SHA = "5f9de5e17605fb2a1c49ed1876de5681142a2ae8"
EXPECTED_OLD = "20a42062934be7f656d87d14f215b5c73de35786"
EXPECTED_BRANCH = "main"

THESIS_CONTRACT_SHA = "782f4c6bb5b9e81c8e859cdfc7554f1a9586cda0"
THESIS_CONTRACT_OLD = "ae47f61bac02c5546143122f7f3dba0cdbf24158"
THESIS_CONTRACT_PROFILE = "deploy_thesis_field_contract_20260921"


def test_wonderful_lections_runtime_profile_is_allowlisted_and_exact():
    config = json.loads((ROOT / "runtime" / "direct_probe_config.json").read_text())
    project = config["projects"]["wonderful-lections"]
    assert project["github_repo"] == "onedayonemasterpiece/wonderful-lections"
    assert project["services"]["runtime"]["unit"] == "wonderful-lections.service"
    assert project["services"]["runtime"]["scope"] == "user"
    assert project["services"]["runtime"]["ports"] == [4310]
    assert project["roots"]["runtime_source"] == "/home/dev/.local/opt/wonderful-lections-runtime-source"
    profile = project["host_actions"]["deploy_review_theses_20260920"]
    assert profile["cwd"] == project["roots"]["runtime_source"]
    assert profile["argv"] == [
        "/usr/bin/bash",
        "/home/dev/projects/codex-devcoveer-mcp/scripts/deploy-wonderful-lections-release.sh",
    ]
    assert profile["env"]["WONDERFUL_EXPECTED_OLD_SHA"] == EXPECTED_OLD
    assert profile["env"]["WONDERFUL_RELEASE_SHA"] == EXPECTED_SHA
    assert profile["env"]["WONDERFUL_RELEASE_BRANCH"] == EXPECTED_BRANCH


def test_wonderful_lections_deployer_fails_closed_around_exact_release():
    text = (ROOT / "scripts" / "deploy-wonderful-lections-release.sh").read_text()
    assert "git_cmd diff --quiet" in text
    assert "git_cmd diff --cached --quiet" in text
    assert 'REMOTE_SHA="$(git_cmd rev-parse FETCH_HEAD)"' in text
    assert '[ "$REMOTE_SHA" = "$RELEASE_SHA" ]' in text
    assert 'git_cmd switch --detach "$RELEASE_SHA"' in text
    assert '/usr/bin/env npm --prefix "$TARGET" ci' in text
    assert '/usr/bin/env npm --prefix "$TARGET" run build' in text
    assert "width:fit-content" in text
    assert "max-width:min(520px,42%)" in text
    assert "systemctl" not in text


def test_wonderful_lections_thesis_contract_release_profile_is_exact():
    config = json.loads((ROOT / "runtime" / "direct_probe_config.json").read_text())
    project = config["projects"]["wonderful-lections"]
    profile = project["host_actions"][THESIS_CONTRACT_PROFILE]
    assert profile["cwd"] == project["roots"]["runtime_source"]
    assert profile["argv"] == [
        "/usr/bin/bash",
        "/home/dev/projects/codex-devcoveer-mcp/scripts/deploy-wonderful-lections-release.sh",
    ]
    assert profile["env"]["WONDERFUL_EXPECTED_OLD_SHA"] == THESIS_CONTRACT_OLD
    assert profile["env"]["WONDERFUL_RELEASE_SHA"] == THESIS_CONTRACT_SHA
    assert profile["env"]["WONDERFUL_RELEASE_BRANCH"] == EXPECTED_BRANCH
