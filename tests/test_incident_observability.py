from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from jsonschema import Draft202012Validator

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/"runtime"
sys.path.insert(0,str(RUNTIME))

import direct_probe_core as core
import direct_probe_host_services as host_services
import direct_probes as probes
import direct_actions as actions
import direct_ops_gateway as gateway
import incident_registry as incidents


def run(coro):
    return asyncio.run(coro)


def git(cwd: Path,*args: str) -> str:
    env=os.environ.copy()
    env.update({"GIT_AUTHOR_NAME":"T","GIT_AUTHOR_EMAIL":"t@example.invalid",
                "GIT_COMMITTER_NAME":"T","GIT_COMMITTER_EMAIL":"t@example.invalid"})
    return subprocess.check_output(["/usr/bin/git","-C",str(cwd),*args],env=env,text=True).strip()


def init_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.check_call(["/usr/bin/git","init","-q","-b","main",str(path)])
    git(path,"config","user.name","T")
    git(path,"config","user.email","t@example.invalid")
    (path/"a.txt").write_text("x\n",encoding="utf-8")
    git(path,"add","a.txt")
    git(path,"commit","-q","-m","init")
    return path


def use_incident_store(monkeypatch,tmp_path: Path) -> None:
    monkeypatch.setattr(incidents,"INCIDENT_STATE_PATH",tmp_path/"state"/"incidents.json")
    monkeypatch.setattr(incidents,"INCIDENT_LOCK_PATH",tmp_path/"state"/"incidents.lock")


def test_incident_registry_links_history_and_requires_verification(monkeypatch,tmp_path):
    use_incident_store(monkeypatch,tmp_path)
    first=incidents.open_incident("o/r",{
        "title":"Gemini live limiter fallback failure",
        "summary":"Live sessions failed while the shared limiter selected a fallback credential.",
        "severity":"sev2","services":["runtime"],"tags":["gemini","limiter"],
    })
    first_id=first["incident"]["incident_id"]
    incidents.note_incident("o/r",{
        "incident_id":first_id,"note":"journal shows limiter rejected the fallback",
        "note_kind":"observation","evidence_refs":["journal:runtime:2026-09-27T08:00Z"],
    })
    incidents.close_incident("o/r",{
        "incident_id":first_id,
        "root_cause":"Fallback credential metadata did not match the project binding.",
        "resolution":"Corrected the project fallback binding.",
        "verification":"Live session succeeded and the runtime journal showed the selected fallback.",
        "action_items":["Add a regression test for project-specific fallback selection."],
    })
    second=incidents.open_incident("o/r",{
        "title":"Gemini limiter fallback failure recurred",
        "summary":"Another live session failed while selecting the limiter fallback.",
        "severity":"sev2","services":["runtime"],"tags":["gemini","limiter"],
    })
    assert first_id in second["incident"]["related_incidents"]
    assert second["related"][0]["incident_id"] == first_id
    found=incidents.search_incidents("o/r",query="gemini limiter")
    assert len(found["incidents"]) == 2
    loaded=incidents.get_incident("o/r",first_id)
    assert loaded["incident"]["status"] == "closed"
    assert loaded["incident"]["verification"]


def test_incident_registry_redacts_secret_like_text(monkeypatch,tmp_path):
    use_incident_store(monkeypatch,tmp_path)
    opened=incidents.open_incident("o/r",{
        "title":"Provider authentication failure",
        "summary":"API_KEY=<redacted> failed during provider startup.",
    })
    raw=(tmp_path/"state"/"incidents.json").read_text(encoding="utf-8")
    assert "supersecretvalue12345" not in raw
    assert "<redacted>" in raw
    assert opened["incident"]["status"] == "open"


def test_log_search_is_targeted_bounded_and_redacted(monkeypatch):
    binding={
        "services":{"app":{"unit":"fixture.service","scope":"user"}},
        "logs":{"app":{"type":"journal","service":"app"}},
        "roots":{},
    }
    rows=[
        {"MESSAGE":"INFO request_id=r1 started","__CURSOR":"c1","__REALTIME_TIMESTAMP":"1790493600000000"},
        {"MESSAGE":"ERROR request_id=r1 API_KEY=<redacted> provider timeout","__CURSOR":"c2","__REALTIME_TIMESTAMP":"1790493601000000"},
        {"MESSAGE":"INFO request_id=r2 ok","__CURSOR":"c3","__REALTIME_TIMESTAMP":"1790493602000000"},
    ]
    text="\n".join(json.dumps(row) for row in rows)+"\n"
    async def fake_run(argv,**kwargs):
        assert "--output=json" in argv
        assert "--reverse" in argv
        return SimpleNamespace(stdout=text.encode(),stderr=b"",exit_code=0,truncated=False,duration_ms=1)
    monkeypatch.setattr(host_services,"_run_process",fake_run)
    monkeypatch.setattr(host_services.time, "time", lambda: 1790493602.5)
    result=run(host_services._log_search(binding,{
        "log":"app","query":"request_id=r1","limit":20,"context_lines":0,
        "since_seconds":600,"regex":False,"case_sensitive":False,
    }))
    assert result["match_count"] == 2
    assert result["returned"] == 2
    rendered=json.dumps(result)
    assert "supersecretvalue12345" not in rendered
    assert result["source"] == "journal:fixture.service"
    assert result["redacted"] is True


def test_log_search_regex_and_context():
    result=host_services._search_log_text(
        "one\nERROR code=42\nthree\nfour",
        query=r"code=\d+",use_regex=True,case_sensitive=False,limit=10,context_lines=1,
    )
    assert result["match_count"] == 1
    assert "one" in result["matches"][0]["text"]
    assert "three" in result["matches"][0]["text"]


def test_worktree_inherits_observability_binding_by_repository(tmp_path):
    repo=init_repo(tmp_path/"repo")
    git(repo,"remote","add","origin","https://github.com/o/r.git")
    cfg=tmp_path/"cfg.json"
    cfg.write_text(json.dumps({"version":1,"projects":{"canonical":{
        "github_repo":"o/r","remotes":{"origin":"o/r"},"services":{"app":{"unit":"fixture.service","scope":"user"}},
        "logs":{"app":{"type":"journal","service":"app"}},"roots":{"source":str(repo)},
        "health":{},"runtimes":{},"runtime_files":{},"config_presence":{},
    }}}),encoding="utf-8")
    out=run(probes.run_host_probe(str(repo),"worktree-generated-id",[
        {"id":"inventory","kind":"inventory"}
    ],config_path=cfg))
    assert out["target"]["binding_project"] == "canonical"
    assert out["target"]["binding_inherited"] is True
    assert out["results"][0]["data"]["logs"] == ["app"]


def test_incident_and_log_search_operations_are_on_typed_surface():
    project_schema=probes.PROJECT_PROBE_INPUT_SCHEMA
    host_schema=probes.HOST_PROBE_INPUT_SCHEMA
    action_schema=actions.PROJECT_ACTION_INPUT_SCHEMA
    project_kinds={x["properties"]["kind"]["const"] for x in project_schema["properties"]["checks"]["items"]["oneOf"]}
    host_kinds={x["properties"]["kind"]["const"] for x in host_schema["properties"]["checks"]["items"]["oneOf"]}
    action_kinds={x["properties"]["kind"]["const"] for x in action_schema["properties"]["action"]["oneOf"]}
    assert {"incident_search","incident_get"} <= project_kinds
    assert "log_search" in host_kinds
    assert {"incident_open","incident_note","incident_close"} <= action_kinds
    guidance=gateway.describe_plane("host_probe")["operator_guidance"]
    assert "targeted" in guidance["observability"]
    assert "Ordinary unit-test" in guidance["incidents"]


def test_live_service_log_bindings_cover_known_devcoveer_services():
    cfg=json.loads((ROOT/"runtime"/"direct_probe_config.json").read_text(encoding="utf-8"))
    projects=cfg["projects"]
    expected={
        "codex-devcoveer-mcp":{"mcp_tunnel","opencode"},
        "my-browser-bridge":{"browser","mcp_tunnel"},
        "vibepublish":{"server","worker"},
        "street-story":{"backend"},
        "wonderful-lections":{"runtime"},
        "mailru-mcp-bridge":{"bridge"},
        "my-data-hub":{"control_plane"},
        "record-idea-hub":{"edge","cert_renew"},
    }
    for project,logs in expected.items():
        assert logs <= set(projects[project]["logs"])


def test_incident_skill_and_installer_are_source_controlled(tmp_path,monkeypatch):
    script=ROOT/"scripts"/"install-incident-observability.py"
    spec=importlib.util.spec_from_file_location("incident_installer",script)
    assert spec and spec.loader
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base="# Existing instructions\n"
    once=module.transformed_agents(base)
    twice=module.transformed_agents(once)
    assert once == twice
    assert "devcoveer-incident-investigation" in once
    assert "full-power agent/shell remains available" in once
    skill=(ROOT/"skills"/"devcoveer-incident-investigation"/"SKILL.md").read_text(encoding="utf-8")
    assert "incident_search" in skill and "log_search" in skill and "incident_close" in skill


def test_incident_actions_execute_without_git_mutation(monkeypatch,tmp_path):
    use_incident_store(monkeypatch,tmp_path)
    repo=init_repo(tmp_path/"repo-actions")
    cfg=tmp_path/"cfg-actions.json"
    cfg.write_text(json.dumps({"version":1,"projects":{"p":{
        "github_repo":"o/r","remotes":{"origin":"o/r"},"services":{},"logs":{},
        "roots":{"source":str(repo)},"health":{},"runtimes":{},"runtime_files":{},
        "config_presence":{},"project_actions":{}
    }}}),encoding="utf-8")
    before=git(repo,"rev-parse","HEAD")
    opened=run(actions.run_project_action(str(repo),"p",{
        "kind":"incident_open","title":"Live request regression",
        "summary":"Production requests return an unexpected retry error.","severity":"sev3",
        "services":["api"],"tags":["retry"],
    },config_path=cfg))
    incident_id=opened["incident"]["incident_id"]
    noted=run(actions.run_project_action(str(repo),"p",{
        "kind":"incident_note","incident_id":incident_id,"note_kind":"observation",
        "note":"Runtime log_search found the retry error.","evidence_refs":["journal:api"],
    },config_path=cfg))
    assert noted["timeline_count"] == 2
    closed=run(actions.run_project_action(str(repo),"p",{
        "kind":"incident_close","incident_id":incident_id,
        "root_cause":"Retry state was carried across requests.",
        "resolution":"Reset retry state at request start.",
        "verification":"Live request succeeded and no retry error appeared in logs.",
        "action_items":["Keep request correlation IDs in retry logs."],
    },config_path=cfg))
    assert closed["incident"]["status"] == "closed"
    assert git(repo,"rev-parse","HEAD") == before
