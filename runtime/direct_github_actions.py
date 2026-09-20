from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from direct_probe_core import ProbeError, _git, _redact_text, _run_process, _validate_alias, _validate_branch
from direct_ops2 import resolve_github_remote

HEX40_RE=re.compile(r"^[0-9a-f]{40}$")
CHATGPT_BRANCH_RE=re.compile(r"^chatgpt/[A-Za-z0-9][A-Za-z0-9._/-]*$")


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
    method=action.get("method","merge")
    flag={"merge":"--merge","squash":"--squash","rebase":"--rebase"}[method]
    await _gh(binding,["pr","merge",str(pr),"--repo",repo,flag],timeout=120)
    after=await _view_pr(binding,repo,pr)
    if after.get("state")!="MERGED":
        raise ProbeError("github","PR merge did not reach MERGED state")
    merge_commit=after.get("mergeCommit")
    merge_sha=merge_commit.get("oid") if isinstance(merge_commit,dict) else None
    return {"status":"ok","action":"github_pr_merge","repository":repo,"number":pr,
            "url":after.get("url"),"merged_at":after.get("mergedAt"),"merge_sha":merge_sha,
            "head_sha":expected,"method":method}


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
