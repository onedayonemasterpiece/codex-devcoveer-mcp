from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from direct_probe_core import ProbeError, _redact_text, _validate_branch
from direct_probe_project_git import _gh

async def _github_status(binding: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    repo = binding.get("github_repo")
    if not repo:
        raise ProbeError("github_unbound", "project has no trusted GitHub binding", status="denied")
    out: dict[str, Any] = {"repository": repo}
    branch = check.get("branch")
    if branch:
        branch = _validate_branch(branch)
        branch_json = await _gh(binding, ["api", "--method", "GET", f"repos/{repo}/branches/{quote(branch, safe='')}"] , limit=32 * 1024)
        payload = json.loads(branch_json.stdout)
        out["branch"] = {"name": branch, "head": payload.get("commit", {}).get("sha")}
    pr = check.get("pr")
    if pr:
        pr_json = await _gh(binding, ["api", "--method", "GET", f"repos/{repo}/pulls/{int(pr)}"], limit=64 * 1024)
        payload = json.loads(pr_json.stdout)
        out["pr"] = {"number": int(pr), "state": payload.get("state"), "draft": payload.get("draft"),
                     "head": payload.get("head", {}).get("sha"), "head_ref": payload.get("head", {}).get("ref"),
                     "base_ref": payload.get("base", {}).get("ref"), "mergeable_state": payload.get("mergeable_state")}
        head_sha = out["pr"]["head"]
    else:
        head_sha = out.get("branch", {}).get("head")
    if head_sha:
        checks_json = await _gh(binding, ["api", "--method", "GET", f"repos/{repo}/commits/{head_sha}/check-runs?per_page=50"], limit=96 * 1024)
        payload = json.loads(checks_json.stdout)
        out["checks"] = [{"id": item.get("id"), "name": item.get("name"), "status": item.get("status"),
                          "conclusion": item.get("conclusion"), "started_at": item.get("started_at"),
                          "completed_at": item.get("completed_at")} for item in payload.get("check_runs", [])[:50]]
        runs_json = await _gh(binding, ["api", "--method", "GET", f"repos/{repo}/actions/runs?head_sha={head_sha}&per_page=20"], limit=96 * 1024)
        runs_payload = json.loads(runs_json.stdout)
        out["workflow_runs"] = [{"id": r.get("id"), "name": r.get("name"), "event": r.get("event"),
                                 "status": r.get("status"), "conclusion": r.get("conclusion"),
                                 "run_attempt": r.get("run_attempt")} for r in runs_payload.get("workflow_runs", [])[:20]]
    run_id = check.get("run_id")
    if run_id:
        jobs_json = await _gh(binding, ["api", "--method", "GET", f"repos/{repo}/actions/runs/{int(run_id)}/jobs?per_page=50"], limit=96 * 1024)
        jobs_payload = json.loads(jobs_json.stdout)
        out["jobs"] = [{"id": j.get("id"), "name": j.get("name"), "status": j.get("status"),
                        "conclusion": j.get("conclusion"), "started_at": j.get("started_at"),
                        "completed_at": j.get("completed_at")} for j in jobs_payload.get("jobs", [])[:50]]
        if int(check.get("failed_log_lines", 0)) > 0:
            log = await _gh(binding, ["run", "view", str(int(run_id)), "--repo", str(repo), "--log-failed"], timeout=20.0, limit=32 * 1024)
            text = "\n".join(log.stdout.decode("utf-8", "replace").splitlines()[-int(check["failed_log_lines"]):])
            text, red = _redact_text(text)
            out["failed_log"] = {"text": text, "redacted": red, "truncated": log.truncated}
    if check.get("include_runners"):
        runners_json = await _gh(binding, ["api", "--method", "GET", f"repos/{repo}/actions/runners?per_page=100"], limit=64 * 1024)
        runners_payload = json.loads(runners_json.stdout)
        out["runners"] = [{"id": r.get("id"), "name": r.get("name"), "os": r.get("os"),
                           "status": r.get("status"), "busy": r.get("busy")} for r in runners_payload.get("runners", [])[:100]]
    return out
