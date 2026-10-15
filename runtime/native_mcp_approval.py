"""Least-broad native Codex tool approval for an owner-authorized write task.

The native app-server runs with approvalPolicy=never for its asynchronous
background turns. Codex otherwise refuses *every* connected MCP tool with a
mutation hint, even when the MCP operation itself is describe/read-only.
Permit only the owner's own DevCoveer app in explicit write tasks. The app's
registered MCP server continues to validate every operation and target.

Do not change the global ~/.codex/config.toml, change other apps' policies,
disable the task filesystem sandbox or mark mutating tools as read-only.
"""
from __future__ import annotations

from typing import Any

DEVCOVEER_APP_ID = "asdk_app_6a8b3564e3fc81918b210222d5dbb212"


def native_devcoveer_app_config(access: str | None) -> dict[str, Any]:
    """Return a per-thread app override; read/unknown access stays untrusted."""
    if access != "write":
        return {}
    return {
        "apps": {
            DEVCOVEER_APP_ID: {
                "default_tools_approval_mode": "approve",
            }
        }
    }
