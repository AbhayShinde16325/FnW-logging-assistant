"""Official MCP client and the provider-independent tool-calling wrapper."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ai.tool_calling import AssistantTurn, ProviderNetworkError
from services.task_scope import TaskActor

logger = logging.getLogger(__name__)

WRITE_CONFIRMATION_TOOLS = frozenset(
    {
        "update_task",
        "update_task_status",
        "update_task_progress",
        "update_task_deadline",
        "update_task_reminder",
        "create_task_reminder",
        "delete_task",
    }
)


class MCPClientError(Exception):
    """The MCP transport or JSON-RPC call failed."""


def _run_sync(coroutine):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


class MCPToolClient:
    def __init__(self, server_url: str, access_token: str, timeout: float = 30.0):
        self.server_url = server_url
        self.access_token = access_token
        self.timeout = timeout

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=httpx.Timeout(self.timeout, read=self.timeout * 2),
        )

    async def _with_session(self, operation):
        try:
            async with self._http_client() as http_client:
                async with streamable_http_client(
                    self.server_url,
                    http_client=http_client,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        return await operation(session)
        except Exception as error:
            raise MCPClientError(str(error)) from error

    async def list_tools(self):
        async def operation(session):
            result = await session.list_tools()
            return result.tools

        return await self._with_session(operation)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async def operation(session):
            result = await session.call_tool(name, arguments)

            if result.structuredContent is not None:
                payload = result.structuredContent
            else:
                text = "\n".join(
                    block.text
                    for block in result.content
                    if getattr(block, "type", None) == "text"
                )

                try:
                    payload = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    payload = {"message": text}

            if result.isError and isinstance(payload, dict):
                payload.setdefault("status", "error")

            return payload if isinstance(payload, dict) else {"result": payload}

        return await self._with_session(operation)

    async def list_resources(self):
        async def operation(session):
            result = await session.list_resources()
            return result.resources

        return await self._with_session(operation)

    async def read_resource(self, uri: str):
        async def operation(session):
            return await session.read_resource(uri)

        return await self._with_session(operation)

    async def list_prompts(self):
        async def operation(session):
            result = await session.list_prompts()
            return result.prompts

        return await self._with_session(operation)

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None):
        async def operation(session):
            return await session.get_prompt(name, arguments)

        return await self._with_session(operation)

    def list_tools_sync(self):
        return _run_sync(self.list_tools())

    def call_tool_sync(self, name: str, arguments: dict[str, Any]):
        return _run_sync(self.call_tool(name, arguments))

    def list_resources_sync(self):
        return _run_sync(self.list_resources())

    def read_resource_sync(self, uri: str):
        return _run_sync(self.read_resource(uri))

    def list_prompts_sync(self):
        return _run_sync(self.list_prompts())

    def get_prompt_sync(self, name: str, arguments: dict[str, str] | None = None):
        return _run_sync(self.get_prompt(name, arguments))


class MCPToolCallingClient:
    """Discover MCP tools and route model tool calls through MCP."""

    def __init__(
        self,
        provider,
        *,
        server_url: str,
        access_token: str,
        actor: TaskActor,
        cache_seconds: float = 300.0,
    ):
        self.provider = provider
        self.server_url = server_url
        self.access_token = access_token
        self.actor = actor
        self.cache_seconds = cache_seconds
        self._mcp = MCPToolClient(server_url, access_token)
        self._cache_key = self._key()
        self._cached_tools = None
        self._cached_at = 0.0

    @property
    def message_adapter(self):
        return self.provider.message_adapter

    def _key(self) -> tuple:
        digest = hashlib.sha256(self.access_token.encode("utf-8")).hexdigest()[:16]
        return (
            self.server_url,
            digest,
            self.actor.id,
            tuple(sorted(self.actor.permissions)),
        )

    def discover_tools(self):
        import time

        now = time.monotonic()

        if (
            self._cached_tools is not None
            and now - self._cached_at < self.cache_seconds
        ):
            return self._cached_tools

        from ai.mcp_client.discovery import tool_discovery

        self._cached_tools = tool_discovery.get(self, self.actor)
        self._cached_at = now
        return self._cached_tools

    def tool_definitions(self):
        from ai.mcp_client.tool_adapter import to_function_tools

        return to_function_tools(self.discover_tools(), self.actor.permissions)

    def chat(self, *, messages: list[dict], tools: list[dict] | None = None):
        try:
            discovered = self.tool_definitions()
        except Exception as error:
            raise ProviderNetworkError(
                "The MCP tool server is unavailable.",
                provider_detail=str(error)[:500],
            ) from error

        return self.provider.chat(messages=messages, tools=discovered)

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        trusted_confirmation: str | None = None,
    ) -> dict[str, Any]:
        safe_arguments = dict(arguments or {})

        if name in WRITE_CONFIRMATION_TOOLS:
            safe_arguments["confirmation"] = trusted_confirmation

        try:
            return self._mcp.call_tool_sync(name, safe_arguments)
        except MCPClientError as error:
            logger.warning("MCP tool call failed: %s", error)
            return {
                "status": "error",
                "message": "The MCP tool server is unavailable.",
            }
