#!/usr/bin/env bash
set -Eeuo pipefail

DEV=dev
DEV_UID="$(id -u "$DEV")"
CURRENT_UID="$(id -u)"
[ "$CURRENT_UID" -eq "$DEV_UID" ] || {
  echo "ERROR: run directly as dev; sudo/root is not used." >&2
  exit 1
}
WORK_REPO=/home/dev/projects/codex-devcoveer-mcp
SOURCE_REF="${DEVCOVEER_SOURCE_REF:-origin/main}"
CAN=/home/dev/.local/libexec/openai-codex-mcp
BOOT=/home/dev/.local/libexec/openai-codex-mcp-bootstrap
WRAP=/home/dev/.local/bin/codex-mcp-server
BRIDGE_PY=/home/dev/.local/share/openai-codex-mcp/bridge-venv/bin/python
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
export HOME=/home/dev XDG_RUNTIME_DIR="/run/user/${DEV_UID}"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${DEV_UID}/bus"
export PATH="/home/dev/.opencode/bin:/home/dev/.local/bin:/usr/local/bin:/usr/bin:/bin"
SKIP_TUNNEL_RESTART="${DEVCOVEER_SKIP_TUNNEL_RESTART:-0}"
[ "$SKIP_TUNNEL_RESTART" = "0" ] || [ "$SKIP_TUNNEL_RESTART" = "1" ] || {
  echo "ERROR: DEVCOVEER_SKIP_TUNNEL_RESTART must be 0 or 1" >&2
  exit 1
}

die(){ echo "ERROR: $*" >&2; exit 1; }
sha(){ sha256sum "$1" | awk '{print $1}'; }
test -d "$WORK_REPO/.git" || die "repository missing"
test -x "$BRIDGE_PY" || die "bridge Python missing"
test -e "$WRAP" || die "MCP wrapper missing"

TMP="$(dev-artifacts new codex-devcoveer-mcp install-current-runtime --retain --reason "Runtime deployment and rollback evidence")"
SOURCE_SHA="$(git -C "$WORK_REPO" rev-parse "$SOURCE_REF^{commit}")" || die "cannot resolve $SOURCE_REF"
git -C "$WORK_REPO" merge-base --is-ancestor "$SOURCE_SHA" main || die "source must be a validated main commit"
SRC="$TMP/source"
mkdir -p "$SRC"
git -C "$WORK_REPO" archive "$SOURCE_SHA" | tar -x -C "$SRC"
echo "source_sha=$SOURCE_SHA"

CAND="$TMP/runtime"
"$BRIDGE_PY" "$SRC/scripts/build-clean-host-runtime.py" "$CAND"
BRIDGE_SHA="$(sha "$CAND/codex-chatgpt-bridge.py")"
BACKEND_SHA="$(sha "$CAND/opencode_backend.py")"

echo "=== candidate MCP acceptance ==="
PYTHONPATH="$CAND" "$BRIDGE_PY" - "$CAND/codex-chatgpt-bridge.py" <<'PY'
import asyncio, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
required={"direct_ops_v2","project_probe","host_probe","project_action","host_action","list_models","start_task","read_task","stage_source_handoff","cleanup_source_handoff","get_task_evidence","record_task_review","street_story_materialize_release","street_story_deploy_user_service","street_story_publish_edge_route","street_story_service","street_story_deployment_receipt"}
async def main():
    async with stdio_client(StdioServerParameters(command=sys.executable,args=[sys.argv[1]])) as (r,w):
        async with ClientSession(r,w) as s:
            init=await asyncio.wait_for(s.initialize(),30)
            tools=await asyncio.wait_for(s.list_tools(),30)
            by={x.name:x for x in tools.tools}
            names=set(by); missing=sorted(required-names)
            print(f"candidate_version={init.server_info.version}")
            print(f"candidate_tool_count={len(names)}")
            if missing: raise SystemExit("missing tools: "+",".join(missing))
            if init.capabilities.tools is None or init.capabilities.tools.list_changed is not True:
                raise SystemExit("tools.listChanged capability is not advertised")
            project_kinds={
                row.get("properties",{}).get("kind",{}).get("const")
                for row in by["project_probe"].input_schema["properties"]["checks"]["items"]["oneOf"]
            }
            for required_kind in {"capabilities","workspace_compare","worktree_compare"}:
                if required_kind not in project_kinds:
                    raise SystemExit("project_probe schema missing "+required_kind)
            action_kinds={
                row.get("properties",{}).get("kind",{}).get("const")
                for row in by["project_action"].input_schema["properties"]["action"]["oneOf"]
            }
            for required_kind in {"write_text","worktree_reconcile","adb","job_start","batch","github_pr_merge"}:
                if required_kind not in action_kinds:
                    raise SystemExit("project_action schema missing "+required_kind)
            gateway=by["direct_ops_v2"].input_schema
            op_schema=gateway["properties"]["operation"]
            if op_schema.get("type")!="string" or "enum" in op_schema:
                raise SystemExit("direct_ops_v2 operation schema is not future-stable")
asyncio.run(main())
PY

CAN_BACKUP=
if [ -e "$CAN" ]; then
  CAN_BACKUP="$TMP/previous-runtime"
  mv "$CAN" "$CAN_BACKUP"
  echo "canonical_backup=$CAN_BACKUP"
fi
mv "$CAND" "$CAN"

WRAP_BACKUP="$TMP/previous-wrapper"
cp -a "$WRAP" "$WRAP_BACKUP"
restore_previous(){
  cp -a "$WRAP_BACKUP" "$WRAP" 2>/dev/null || true
  if [ -n "$CAN_BACKUP" ] && [ -e "$CAN_BACKUP" ]; then
    mv "$CAN" "$TMP/failed-runtime" 2>/dev/null || true
    mv "$CAN_BACKUP" "$CAN" 2>/dev/null || true
  fi
}
restart_after_failure(){
  if [ "$SKIP_TUNNEL_RESTART" != "1" ]; then
    systemctl --user restart codex-devserver-tunnel.service || true
  fi
}
trap 'rc=$?; if [ "$rc" -ne 0 ]; then restore_previous; restart_after_failure; fi; exit "$rc"' EXIT

if [ -L "$WRAP" ]; then
  target="$(readlink -f "$WRAP")"
  case "$target" in
    "$CAN"/*) ;;
    "$BOOT"/*) ln -sfn "${target/$BOOT/$CAN}" "$WRAP" ;;
    *) die "wrapper target is neither bootstrap nor canonical: $target" ;;
  esac
elif [ -f "$WRAP" ]; then
  if grep -qF "$BOOT" "$WRAP"; then
    "$BRIDGE_PY" - "$WRAP" "$BOOT" "$CAN" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); text=p.read_text(encoding="utf-8")
if text.count(sys.argv[2]) != 1: raise SystemExit("bootstrap wrapper anchor is not unique")
p.write_text(text.replace(sys.argv[2],sys.argv[3]),encoding="utf-8")
PY
  elif ! grep -qF "$CAN" "$WRAP"; then
    die "wrapper references neither bootstrap nor canonical runtime"
  fi
else
  die "unsupported wrapper type"
fi

echo "=== canonical wrapper acceptance ==="
"$BRIDGE_PY" - "$WRAP" <<'PY'
import asyncio, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
required={"direct_ops_v2","project_probe","host_probe","project_action","host_action","list_models","start_task","read_task","stage_source_handoff","cleanup_source_handoff","get_task_evidence","record_task_review","street_story_materialize_release","street_story_deploy_user_service","street_story_publish_edge_route","street_story_service","street_story_deployment_receipt"}
async def main():
    async with stdio_client(StdioServerParameters(command=sys.argv[1],args=[])) as (r,w):
        async with ClientSession(r,w) as s:
            await asyncio.wait_for(s.initialize(),30)
            tools=await asyncio.wait_for(s.list_tools(),30)
            missing=sorted(required-{x.name for x in tools.tools})
            if missing: raise SystemExit("wrapper missing tools: "+",".join(missing))
asyncio.run(main())
PY

echo "bridge=$BRIDGE_SHA"
echo "backend=$BACKEND_SHA"
echo "wrapper_canonical=PASS"
if [ "$SKIP_TUNNEL_RESTART" = "1" ]; then
  # Return the deployment receipt before the caller explicitly restarts the
  # registered MCP tunnel through host_action. Do not schedule a self-restart
  # here: even a deferred systemd-run can race the current MCP response.
  trap - EXIT
  echo "tunnel_restart=DEFERRED"
  echo "deployment_evidence=$TMP"
  exit 0
fi

systemctl --user restart codex-devserver-tunnel.service
ready=0
for _ in $(seq 1 60); do
  if systemctl --user is-active --quiet codex-devserver-tunnel.service \
     && curl -fsS --connect-timeout 1 --max-time 3 http://127.0.0.1:18080/readyz >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 1
done
[ "$ready" -eq 1 ] || die "tunnel did not become ready"
trap - EXIT
echo "tunnel_ready=PASS"
echo "deployment_evidence=$TMP"
