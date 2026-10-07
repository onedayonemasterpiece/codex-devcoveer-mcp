from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from direct_probe_core import ProbeError, _git, _redact_text, _run_process, _validate_alias, _validate_branch
from direct_ops2 import resolve_github_remote
from requirements_guard import MANIFEST_PATH

HEX40_RE=re.compile(r"^[0-9a-f]{40}$")
CHATGPT_BRANCH_RE=re.compile(r"^chatgpt/[A-Za-z0-9][A-Za-z0-9._/-]*$")
REQUEST_KEY_RE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
DISPATCH_STATE_PATH=Path.home()/".local"/"state"/"openai-codex-mcp"/"github-workflow-dispatches.json"
DISPATCH_LOCK_PATH=DISPATCH_STATE_PATH.with_suffix(".lock")


def _env() -> dict[str,str]:
    return {"PATH":"/usr/local/bin:/usr/bin:/bin","HOME":os.environ.get("HOME","/home/dev"),
            "LANG":"C.UTF-8","LC_ALL":"C.UTF-8"}


def _gh_path(binding: dict[str,Any]) -> str:
    raw=(binding.get("executables") or {}).get("gh","/usr/bin/gh")
    if not isinstance(raw,str) or not raw.startswith("/"):
        raise ProbeError("config","gh executable is not trusted")
    path=Path(raw)
    if not path.is_file() or not os.access(path,os.X_OK):
        raise ProbeError("tool_unavailable","trusted gh executable is unavailable",status="unavailable")
    return str(path)


async def _gh(binding: dict[str,Any],args:list[str],*,timeout:float=30,limit:int=256*1024) -> str:
    result=await _run_process([_gh_path(binding),*args],env=_env(),timeout=timeout,output_limit=limit)
    if result.exit_code!=0:
        err,_=_redact_text(result.stderr.decode("utf-8","replace"))
        raise ProbeError("github",err[-2000:] or f"gh exited {result.exit_code}",exit_code=result.exit_code)
    text,red=_redact_text(result.stdout.decode("utf-8","replace"))
    return text


async def _repo(root:Path,binding:dict[str,Any],remote:str) -> tuple[str,str]:
    alias=_validate_alias(remote)
    configured=binding.get("remotes") or {}
    if configured and alias not in configured:
        raise ProbeError("remote_denied","remote alias is not registered for this project",status="denied")
    repo,_url=await resolve_github_remote(root,binding,alias)
    return alias,repo


async def _remote_sha(root:Path,remote:str,branch:str) -> str:
    result=await _git(root,"ls-remote","--heads",remote,f"refs/heads/{branch}",timeout=20,output_limit=4096)
    rows=[line.split("\t",1) for line in result.stdout.decode("utf-8","replace").splitlines() if line]
    if len(rows)!=1 or len(rows[0])!=2 or not HEX40_RE.fullmatch(rows[0][0].lower()):
        raise ProbeError("github","remote branch did not resolve to exactly one SHA")
    return rows[0][0].lower()


async def _view_pr(binding:dict[str,Any],repo:str,pr:str|int) -> dict[str,Any]:
    text=await _gh(binding,[
        "pr","view",str(pr),"--repo",repo,
        "--json","number,url,state,headRefName,headRefOid,baseRefName,mergeable,mergedAt,mergeCommit",
    ])
    try:
        data=json.loads(text)
    except ValueError as exc:
        raise ProbeError("github","PR response was not valid JSON") from exc
    if not isinstance(data,dict):
        raise ProbeError("github","PR response was not an object")
    return data


async def pr_create(root:Path,binding:dict[str,Any],action:dict[str,Any]) -> dict[str,Any]:
    remote,repo=await _repo(root,binding,action.get("remote","origin"))
    head=_validate_branch(action["head"])
    base=_validate_branch(action.get("base","main"))
    expected=action["expected_head_sha"].lower()
    if CHATGPT_BRANCH_RE.fullmatch(head) is None or HEX40_RE.fullmatch(expected) is None:
        raise ProbeError("invalid_input","PR head must be chatgpt/* with exact expected SHA")
    observed=await _remote_sha(root,remote,head)
    if observed!=expected:
        raise ProbeError("remote_moved",f"remote head {observed} differs from expected {expected}",status="denied")

    existing_text=await _gh(binding,[
        "pr","list","--repo",repo,"--state","open","--head",head,"--base",base,
        "--json","number,url,headRefOid",
    ])
    try:
        existing=json.loads(existing_text)
    except ValueError as exc:
        raise ProbeError("github","PR list response was not valid JSON") from exc
    if isinstance(existing,list) and len(existing)==1:
        row=existing[0]
        if row.get("headRefOid")!=expected:
            raise ProbeError("remote_moved","existing PR head differs from expected SHA",status="denied")
        return {"status":"ok","action":"github_pr_create","repository":repo,
                "number":row.get("number"),"url":row.get("url"),"head_sha":expected,"reused":True}
    if existing:
        raise ProbeError("github","multiple matching open PRs exist",status="denied")

    title=action["title"].strip()
    body=action.get("body","")
    await _gh(binding,[
        "pr","create","--repo",repo,"--head",head,"--base",base,
        "--title",title,"--body",body,
    ],timeout=45)
    data=await _view_pr(binding,repo,head)
    if data.get("headRefOid")!=expected:
        raise ProbeError("remote_moved","created PR head differs from expected SHA",status="denied")
    return {"status":"ok","action":"github_pr_create","repository":repo,
            "number":data.get("number"),"url":data.get("url"),"head_sha":expected,"reused":False}


async def pr_update(root:Path,binding:dict[str,Any],action:dict[str,Any]) -> dict[str,Any]:
    _remote,repo=await _repo(root,binding,action.get("remote","origin"))
    pr=int(action["pr"])
    expected=action["expected_head_sha"].lower()
    data=await _view_pr(binding,repo,pr)
    if data.get("state")!="OPEN" or not isinstance(data.get("headRefName"),str) or CHATGPT_BRANCH_RE.fullmatch(data["headRefName"]) is None:
        raise ProbeError("pr_denied","only open chatgpt/* PRs may be updated",status="denied")
    if data.get("headRefOid")!=expected:
        raise ProbeError("remote_moved","PR head differs from expected SHA",status="denied")
    fields=[]
    if "title" in action: fields.extend(["-f","title="+action["title"]])
    if "body" in action: fields.extend(["-f","body="+action["body"]])
    if not fields:
        raise ProbeError("invalid_input","PR update requires title or body")
    await _gh(
        binding,
        ["api","--method","PATCH",f"repos/{repo}/pulls/{pr}",*fields],
        timeout=30,
    )
    after=await _view_pr(binding,repo,pr)
    return {"status":"ok","action":"github_pr_update","repository":repo,
            "number":pr,"url":after.get("url"),"head_sha":expected}


async def _pr_changed_paths(binding: dict[str, Any], repo: str, pr: int) -> set[str]:
    text = await _gh(
        binding,
        [
            "api", "--paginate", f"repos/{repo}/pulls/{pr}/files",
            "--jq", ".[] | .filename, (.previous_filename // empty)",
        ],
        limit=256 * 1024,
    )
    paths: set[str] = set()
    for raw in text.splitlines():
        path = raw.strip()
        if path and len(path) <= 1000 and "\x00" not in path:
            paths.add(path)
    return paths


async def pr_merge(root:Path,binding:dict[str,Any],action:dict[str,Any]) -> dict[str,Any]:
    _remote,repo=await _repo(root,binding,action.get("remote","origin"))
    pr=int(action["pr"]); expected=action["expected_head_sha"].lower()
    expected_base=_validate_branch(action.get("expected_base","main"))
    data=await _view_pr(binding,repo,pr)
    if data.get("state")!="OPEN":
        raise ProbeError("pr_state","PR is not open",status="denied")
    head=data.get("headRefName")
    if not isinstance(head,str) or CHATGPT_BRANCH_RE.fullmatch(head) is None:
        raise ProbeError("pr_denied","only chatgpt/* PRs may be merged directly",status="denied")
    if data.get("headRefOid")!=expected:
        raise ProbeError("remote_moved","PR head differs from expected SHA",status="denied")
    if data.get("baseRefName")!=expected_base:
        raise ProbeError("pr_base_mismatch","PR base differs from expected branch",status="denied")
    changed=await _pr_changed_paths(binding,repo,pr)
    requirements_change_ref=action.get("requirements_change_ref")
    if MANIFEST_PATH in changed and not requirements_change_ref:
        raise ProbeError(
            "requirements_change_required",
            "PR changes or renames the protected requirements contract; merge requires requirements_change_ref "
            "pointing to the explicit user task/agreement",
            status="denied",
        )
    method=action.get("method","merge")
    flag={"merge":"--merge","squash":"--squash","rebase":"--rebase"}[method]
    await _gh(binding,["pr","merge",str(pr),"--repo",repo,flag],timeout=120)
    after=await _view_pr(binding,repo,pr)
    if after.get("state")!="MERGED":
        raise ProbeError("github","PR merge did not reach MERGED state")
    merge_commit=after.get("mergeCommit")
    merge_sha=merge_commit.get("oid") if isinstance(merge_commit,dict) else None
    result={"status":"ok","action":"github_pr_merge","repository":repo,"number":pr,
            "url":after.get("url"),"merged_at":after.get("mergedAt"),"merge_sha":merge_sha,
            "head_sha":expected,"method":method}
    if MANIFEST_PATH in changed:
        result["requirementsChangeRef"]=requirements_change_ref
    return result


async def delete_remote_branch(root:Path,binding:dict[str,Any],action:dict[str,Any]) -> dict[str,Any]:
    remote,repo=await _repo(root,binding,action.get("remote","origin"))
    branch=_validate_branch(action["branch"]); expected=action["expected_sha"].lower()
    if CHATGPT_BRANCH_RE.fullmatch(branch) is None:
        raise ProbeError("branch_denied","only chatgpt/* remote branches may be deleted directly",status="denied")
    observed=await _remote_sha(root,remote,branch)
    if observed!=expected:
        raise ProbeError("remote_moved",f"remote branch {observed} differs from expected {expected}",status="denied")
    result=await _git(root,"push",remote,"--delete",branch,timeout=120,output_limit=32*1024)
    if result.exit_code!=0:
        err,_=_redact_text(result.stderr.decode("utf-8","replace"))
        raise ProbeError("push_failed",err[-1200:] or "remote branch delete failed",exit_code=result.exit_code)
    return {"status":"ok","action":"github_branch_delete","repository":repo,
            "branch":branch,"deleted_sha":expected}


async def git_fetch(root: Path, binding: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    remote, repo = await _repo(root, binding, action.get("remote", "origin"))
    branch = _validate_branch(action["branch"])
    tracking = f"refs/remotes/{remote}/{branch}"
    await _git(root, "fetch", "--no-tags", "--no-recurse-submodules", remote,
               f"refs/heads/{branch}:{tracking}", timeout=120, output_limit=32768)
    result = await _git(root, "rev-parse", "--verify", f"{tracking}^{{commit}}", output_limit=128)
    return {"status": "ok", "action": "git_fetch", "repository": repo, "remote": remote,
            "branch": branch, "fetched_sha": result.stdout.decode().strip(), "checkout_updated": False}




def _dispatch_spec_sha(repo: str, workflow: str, ref: str, inputs: dict[str, Any]) -> str:
    payload = json.dumps(
        {"repository": repo, "workflow": workflow, "ref": ref, "inputs": inputs},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _dispatch_state_load() -> dict[str, Any]:
    if not DISPATCH_STATE_PATH.exists():
        return {"version": 1, "receipts": {}}
    if DISPATCH_STATE_PATH.is_symlink():
        raise ProbeError("dispatch_receipt", "dispatch receipt store may not be a symlink")
    try:
        data = json.loads(DISPATCH_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProbeError("dispatch_receipt", "dispatch receipt store is unreadable") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("receipts"), dict):
        raise ProbeError("dispatch_receipt", "dispatch receipt store has unsupported shape")
    return data


def _dispatch_state_save(data: dict[str, Any]) -> None:
    DISPATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".workflow-dispatch-", dir=DISPATCH_STATE_PATH.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, DISPATCH_STATE_PATH)
        os.chmod(DISPATCH_STATE_PATH, 0o600)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


class _DispatchLock:
    def __enter__(self):
        DISPATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(DISPATCH_LOCK_PATH, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.handle = os.fdopen(fd, "r+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        self.data = _dispatch_state_load()
        return self.data

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                _dispatch_state_save(self.data)
        finally:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


async def _workflow_run_rows(binding: dict[str, Any], repo: str, workflow: str, ref: str) -> list[dict[str, Any]]:
    text = await _gh(
        binding,
        ["api", "--method", "GET", f"repos/{repo}/actions/workflows/{workflow}/runs",
         "-f", "event=workflow_dispatch", "-f", "per_page=50"],
        timeout=30,
    )
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProbeError("github", "workflow runs response was not valid JSON") from exc
    rows = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ProbeError("github", "workflow runs response has no workflow_runs list")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        run_id = row.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            continue
        head_branch = row.get("head_branch")
        if isinstance(head_branch, str) and head_branch != ref:
            continue
        out.append({
            "id": run_id,
            "status": row.get("status"),
            "conclusion": row.get("conclusion"),
            "head_sha": row.get("head_sha"),
            "head_branch": head_branch,
            "created_at": row.get("created_at"),
            "html_url": row.get("html_url"),
        })
    return out


async def _reconcile_dispatch(binding: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    rows = await _workflow_run_rows(
        binding, receipt["repository"], receipt["workflow"], receipt["ref"]
    )
    baseline = set(int(x) for x in receipt.get("baseline_run_ids", []) if isinstance(x, int))
    candidates = [row for row in rows if row["id"] not in baseline]
    if len(candidates) == 1:
        return {"state": "found", "run": candidates[0]}
    if len(candidates) > 1:
        return {"state": "ambiguous", "runs": candidates[:10]}
    return {"state": "not_found", "runs": []}


def _dispatch_public(receipt: dict[str, Any], *, reused: bool) -> dict[str, Any]:
    run_id = receipt.get("run_id")
    return {
        "status": receipt.get("status", "accepted_unknown"),
        "action": "github_workflow_dispatch",
        "repository": receipt["repository"],
        "workflow": receipt["workflow"],
        "requested_ref": receipt["ref"],
        "request_key": receipt["request_key"],
        "run_id": run_id,
        "url": receipt.get("url"),
        "receipt_complete": isinstance(run_id, int),
        "automatic_retry_safe": True,
        "reused": reused,
        "note": (
            "Same request_key never performs a second dispatch; use a new key for an intentional rerun."
        ),
    }

async def workflow_dispatch(root: Path, binding: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    _remote, repo = await _repo(root, binding, action.get("remote", "origin"))
    workflow = action["workflow"]
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", workflow) is None:
        raise ProbeError("invalid_input", "workflow must be an ID or a filename")
    ref = _validate_branch(action["ref"])
    inputs = action.get("inputs") or {}
    request_key = action.get("request_key")

    async def post_dispatch() -> None:
        args = ["api", "--method", "POST", f"repos/{repo}/actions/workflows/{workflow}/dispatches",
                "-H", "X-GitHub-Api-Version: 2026-03-10", "-f", f"ref={ref}"]
        for key, value in inputs.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", key) is None:
                raise ProbeError("invalid_input", "invalid workflow input name")
            if isinstance(value, str):
                args.extend(["-f", f"inputs[{key}]={value}"])
            else:
                args.extend(["-F", f"inputs[{key}]={json.dumps(value, allow_nan=False)}"])
        await _gh(binding, args, timeout=45)

    if request_key is None:
        args = ["api", "--method", "POST", f"repos/{repo}/actions/workflows/{workflow}/dispatches",
                "-H", "X-GitHub-Api-Version: 2026-03-10", "-f", f"ref={ref}"]
        for key, value in inputs.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", key) is None:
                raise ProbeError("invalid_input", "invalid workflow input name")
            if isinstance(value, str):
                args.extend(["-f", f"inputs[{key}]={value}"])
            else:
                args.extend(["-F", f"inputs[{key}]={json.dumps(value, allow_nan=False)}"])
        text = await _gh(binding, args, timeout=45)
        try:
            legacy = json.loads(text) if text.strip() else {}
        except ValueError:
            legacy = {}
        run_id = legacy.get("workflow_run_id") if isinstance(legacy, dict) else None
        run_id = run_id if isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0 else None
        return {
            "status": "accepted" if run_id is not None else "accepted_unknown",
            "action": "github_workflow_dispatch",
            "repository": repo, "workflow": workflow, "requested_ref": ref,
            "run_id": run_id,
            "url": f"https://github.com/{repo}/actions/runs/{run_id}" if run_id else None,
            "receipt_complete": run_id is not None,
            "automatic_retry_safe": False,
            "note": "No request_key was supplied; do not automatically retry after a lost response.",
        }
    if REQUEST_KEY_RE.fullmatch(request_key) is None:
        raise ProbeError("invalid_input", "invalid workflow dispatch request_key")

    spec_sha = _dispatch_spec_sha(repo, workflow, ref, inputs)
    created_new = False
    with _DispatchLock() as state:
        existing = state["receipts"].get(request_key)
        if existing is not None:
            if existing.get("spec_sha256") != spec_sha:
                raise ProbeError(
                    "request_key_conflict",
                    "workflow dispatch request_key is already bound to different arguments",
                    status="denied",
                )
            receipt = dict(existing)
        else:
            receipt = {
                "request_key": request_key, "spec_sha256": spec_sha,
                "repository": repo, "workflow": workflow, "ref": ref,
                "input_names": sorted(inputs), "baseline_run_ids": [],
                "created_at": int(time.time()), "updated_at": int(time.time()),
                "status": "reserving", "run_id": None, "url": None,
            }
            state["receipts"][request_key] = receipt
            created_new = True

    if not created_new:
        if isinstance(receipt.get("run_id"), int):
            return _dispatch_public(receipt, reused=True)
        if receipt.get("status") == "reserving":
            result = _dispatch_public(receipt, reused=True)
            result["status"] = "pending_reconciliation"
            result["note"] = (
                "The first caller reserved this request_key and is establishing its baseline; "
                "this caller did not dispatch."
            )
            return result
        reconciled = await _reconcile_dispatch(binding, receipt)
        if reconciled["state"] == "found":
            run = reconciled["run"]
            receipt["run_id"] = run["id"]
            receipt["url"] = run.get("html_url") or f"https://github.com/{repo}/actions/runs/{run['id']}"
            receipt["status"] = "accepted"
            receipt["updated_at"] = int(time.time())
            with _DispatchLock() as state:
                state["receipts"][request_key] = receipt
            return _dispatch_public(receipt, reused=True)
        receipt["status"] = "ambiguous" if reconciled["state"] == "ambiguous" else "pending_reconciliation"
        receipt["updated_at"] = int(time.time())
        with _DispatchLock() as state:
            state["receipts"][request_key] = receipt
        result = _dispatch_public(receipt, reused=True)
        if reconciled["state"] == "ambiguous":
            result["candidate_run_ids"] = [row["id"] for row in reconciled["runs"]]
        return result

    # The reservation is durable before any network mutation. Establish a baseline
    # outside the file lock so another caller cannot block the event loop.
    rows = await _workflow_run_rows(binding, repo, workflow, ref)
    receipt["baseline_run_ids"] = [row["id"] for row in rows]
    receipt["status"] = "prepared"
    receipt["updated_at"] = int(time.time())
    with _DispatchLock() as state:
        current = state["receipts"].get(request_key)
        if not isinstance(current, dict) or current.get("spec_sha256") != spec_sha:
            raise ProbeError("dispatch_receipt", "workflow dispatch reservation changed unexpectedly")
        state["receipts"][request_key] = receipt

    try:
        await post_dispatch()
    except ProbeError:
        receipt["status"] = "unknown_after_error"
        receipt["updated_at"] = int(time.time())
        with _DispatchLock() as state:
            state["receipts"][request_key] = receipt
        raise

    receipt["status"] = "submitted"
    receipt["updated_at"] = int(time.time())
    with _DispatchLock() as state:
        state["receipts"][request_key] = receipt

    for _ in range(5):
        reconciled = await _reconcile_dispatch(binding, receipt)
        if reconciled["state"] == "found":
            run = reconciled["run"]
            receipt["run_id"] = run["id"]
            receipt["url"] = run.get("html_url") or f"https://github.com/{repo}/actions/runs/{run['id']}"
            receipt["status"] = "accepted"
            break
        if reconciled["state"] == "ambiguous":
            receipt["status"] = "ambiguous"
            break
        await asyncio.sleep(1)
    else:
        receipt["status"] = "accepted_unknown"

    receipt["updated_at"] = int(time.time())
    with _DispatchLock() as state:
        state["receipts"][request_key] = receipt
    result = _dispatch_public(receipt, reused=False)
    if receipt["status"] == "ambiguous":
        result["note"] = (
            "Dispatch was submitted but multiple new runs appeared; same request_key will not dispatch again."
        )
    return result


async def workflow_run_wait(root: Path, binding: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    _remote,repo=await _repo(root,binding,action.get("remote","origin")); run_id=int(action["run_id"])
    expected=action["expected_head_sha"].lower(); timeout_seconds=max(1,min(int(action.get("timeout_seconds",20)),25))
    poll_interval_ms=max(250,min(int(action.get("poll_interval_ms",750)),2000)); return_on_progress=bool(action.get("return_on_progress",True)); started=time.monotonic()
    async def read() -> dict[str, Any]:
        data=json.loads(await _gh(binding,["api",f"repos/{repo}/actions/runs/{run_id}"]))
        if not isinstance(data,dict) or data.get("head_sha")!=expected: raise ProbeError("run_moved","workflow run does not match expected head SHA",status="denied")
        return {"status":data.get("status"),"conclusion":data.get("conclusion"),"run_id":run_id,"repository":repo,"head_sha":expected,
                "run_attempt":data.get("run_attempt"),"updated_at":data.get("updated_at"),"url":data.get("html_url") or f"https://github.com/{repo}/actions/runs/{run_id}"}
    current=await read(); baseline=(current.get("status"),current.get("conclusion"),current.get("run_attempt"))
    while current.get("status")!="completed":
        elapsed=time.monotonic()-started
        if elapsed>=timeout_seconds: return {**current,"action":"github_run_wait","waited_ms":int(elapsed*1000),"wait_timed_out":True,"progress_changed":False}
        await asyncio.sleep(min(poll_interval_ms/1000.0,timeout_seconds-elapsed)); current=await read()
        token=(current.get("status"),current.get("conclusion"),current.get("run_attempt"))
        if current.get("status")=="completed" or (return_on_progress and token!=baseline):
            return {**current,"action":"github_run_wait","waited_ms":int((time.monotonic()-started)*1000),"wait_timed_out":False,"progress_changed":token!=baseline}
    return {**current,"action":"github_run_wait","waited_ms":int((time.monotonic()-started)*1000),"wait_timed_out":False,"progress_changed":False}


async def run_cancel(root: Path, binding: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    _remote, repo = await _repo(root, binding, action.get("remote", "origin"))
    run_id = int(action["run_id"])
    endpoint = f"repos/{repo}/actions/runs/{run_id}"
    data = json.loads(await _gh(binding, ["api", endpoint]))
    if not isinstance(data, dict) or data.get("head_sha") != action["expected_head_sha"]:
        raise ProbeError("run_moved", "workflow run does not match expected head SHA", status="denied")
    if data.get("status") == "completed":
        return {"status": "terminal", "action": "github_run_cancel", "run_id": run_id,
                "repository": repo, "conclusion": data.get("conclusion")}
    await _gh(binding, ["api", "--method", "POST", endpoint + "/cancel"])
    return {"status": "cancelling", "action": "github_run_cancel", "repository": repo,
            "run_id": run_id, "head_sha": data["head_sha"],
            "url": f"https://github.com/{repo}/actions/runs/{run_id}"}
