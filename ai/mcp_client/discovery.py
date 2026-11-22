"""Permission-aware MCP tool discovery with a short-lived cache."""

from __future__ import annotations

import threading
import time

from services.task_scope import TaskActor


class ToolDiscoveryCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, list]] = {}

    def get(self, client, actor: TaskActor) -> list:
        key = client._key()
        now = time.monotonic()

        with self._lock:
            cached = self._cache.get(key)

            if cached is not None and now < cached[0]:
                return cached[1]

        tools = client._mcp.list_tools_sync()

        with self._lock:
            self._cache[key] = (now + client.cache_seconds, tools)

        return tools

    def invalidate(self, key: tuple | None = None) -> None:
        with self._lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(key, None)


tool_discovery = ToolDiscoveryCache()
