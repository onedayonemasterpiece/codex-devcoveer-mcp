from __future__ import annotations

import logging
from typing import Any, Callable

from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler
from mcp.shared.subscriptions import ToolsListChanged

log = logging.getLogger(__name__)


class ToolRefreshSubscriptionBus(InMemorySubscriptionBus):
    """Prime every modern MCP subscription with one tools-list refresh event."""

    def subscribe(self, listener: Callable[[Any], None]) -> Callable[[], None]:
        unsubscribe = super().subscribe(listener)
        try:
            listener(ToolsListChanged())
        except Exception:
            log.exception("initial tools-list refresh listener failed")
        return unsubscribe


def build_tool_refresh_listen_handler() -> ListenHandler:
    return ListenHandler(ToolRefreshSubscriptionBus())
