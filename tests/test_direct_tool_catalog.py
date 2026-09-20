from __future__ import annotations

import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
BRIDGE_PY = Path("/home/dev/.local/share/openai-codex-mcp/bridge-venv/bin/python")


def test_tool_refresh_catalog_with_canonical_mcp_sdk():
    script = r"""
import json
import sys
sys.path.insert(0, sys.argv[1])
from mcp.server import NotificationOptions, Server
from mcp.server.subscriptions import ListenHandler
from mcp.shared.subscriptions import ToolsListChanged
import direct_tool_catalog as catalog

bus = catalog.ToolRefreshSubscriptionBus()
seen = []
unsubscribe = bus.subscribe(seen.append)
try:
    primed = len(seen) == 1 and isinstance(seen[0], ToolsListChanged)
finally:
    unsubscribe()

async def list_tools(_ctx, _params):
    raise AssertionError("not called")

listen = catalog.build_tool_refresh_listen_handler()
server = Server("x", on_list_tools=list_tools, on_subscriptions_listen=listen)
modern = server.get_capabilities(protocol_version="2026-07-28")
legacy = server.get_capabilities(
    NotificationOptions(tools_changed=True),
    protocol_version="2025-11-25",
)
print(json.dumps({
    "primed": primed,
    "listen_handler": isinstance(listen, ListenHandler),
    "modern": bool(modern.tools and modern.tools.list_changed),
    "legacy": bool(legacy.tools and legacy.tools.list_changed),
}))
"""
    completed = subprocess.run(
        [str(BRIDGE_PY), "-c", script, str(RUNTIME)],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload == {
        "primed": True,
        "listen_handler": True,
        "modern": True,
        "legacy": True,
    }
