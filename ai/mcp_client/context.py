"""Request-scoped access token used only to call the local MCP endpoint."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT_ACCESS_TOKEN: ContextVar[str | None] = ContextVar(
    "mcp_access_token", default=None
)


def current_access_token() -> str | None:
    return _CURRENT_ACCESS_TOKEN.get()


@contextmanager
def use_mcp_access_token(token: str | None):
    current = _CURRENT_ACCESS_TOKEN.set(token)

    try:
        yield token
    finally:
        _CURRENT_ACCESS_TOKEN.reset(current)
