# FnW Task Manager MCP Architecture

## What MCP means here

The Model Context Protocol (MCP) is the application's provider-neutral
capability boundary. Business services are implemented once and exposed by a
central MCP server. Cohere, future OpenAI models, external agents and other MCP
clients all discover and invoke the same tools, resources and prompts.

The MCP server is not a REST wrapper. It uses the official Python MCP SDK and
the MCP Streamable HTTP transport at:

```text
POST/GET/DELETE /mcp
```

The default deployment mounts it inside the existing FastAPI backend, so no
separate microservice is required.

## Architecture

```text
                      +-----------------------------+
  Browser / chat ---> | FastAPI task/chat REST API  |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      | Provider adapter             |
                      | AI_PROVIDER=cohere           |
                      +--------------+--------------+
                                     |
                         MCP client | discovers tools,
                         +-----------+ invokes tools
                                     v
                      +-----------------------------+
                      | MCP server /mcp             |
                      | Streamable HTTP + Bearer     |
                      | auth + per-request context  |
                      +--------------+--------------+
                                     |
               +---------------------+--------------------+
               |                     |                    |
               v                     v                    v
       task-management        notification/push     read-only SQL
       services               services              inspection
               |                     |                    |
               +---------------------+--------------------+
                                     v
                         Existing repositories / ORM
                                     |
                                     v
                                PostgreSQL
```

The provider layer never talks to PostgreSQL directly. The MCP layer never
reimplements task ownership, reminder scheduling, notification delivery,
employee lookup or SQL validation. MCP tools are thin wrappers around existing
services.

## Repository layout

```text
mcp_server/
    server.py                 low-level MCP Server + Streamable HTTP manager
    configuration.py          MCP_ENABLED, endpoint, transport and cache settings
    registry/tool_registry.py central unique-name tool registry
    tools/                    task, employee, notification, database, agent tools
    resources/                application docs and existing SKILL.md files
    prompts/                  reusable provider-neutral workflow prompts
    security/                 bearer authentication, execution context, RBAC

ai/
    providers/                BaseAIProvider, Cohere adapter, provider factory
    mcp_client/               official MCP client, discovery cache, schema adapter
```

## Endpoint and transport

- Endpoint: `MCP_PATH` (default `/mcp`).
- Transport: `MCP_TRANSPORT=streamable-http`.
- Responses default to JSON for straightforward proxies and clients.
- Stateless mode is the default, so no user identity is retained in a shared
  MCP session. Every HTTP request carries its own bearer token.
- The MCP session manager starts in the FastAPI lifespan only when
  `MCP_ENABLED=true`.

### Local run

```bash
pip install -r requirements.txt
MCP_ENABLED=true DATABASE_AGENT=1 AI_PROVIDER=cohere uvicorn app:app --port 8000
```

### Docker

The backend service in `docker-compose.yml` publishes port `8000`; `/mcp` is
available at `http://localhost:8000/mcp`. The Cohere adapter uses
`MCP_SERVER_URL=http://127.0.0.1:8000/mcp` inside the same container.

## Authentication and authorization

Every MCP request must send a bearer token:

```http
Authorization: Bearer <token>
```

Two credential types are accepted for MCP only (`/mcp/`):

1. **FnW access JWT (existing, unchanged):** validated with `auth.security`
   JWT checks and the existing database-backed `employee_for_session(...)`
   session check. Used by browser-issued flows and the internal Cohere/local
   adapter (`MCP_SERVER_URL`, default `http://127.0.0.1:8000/mcp`).
2. **Dedicated MCP client credential (additive):** a long-lived, revocable
   token prefixed with `fnw_mcp_`, issued to an authenticated employee via
   `POST /auth/mcp-credentials` (plaintext shown once). Only a SHA-256 hash is
   stored. Validation requires the credential hash to match, not be revoked,
   not be expired, and the associated employee to exist and remain active. It
   resolves the same authenticated employee/`TaskActor` and is subject to the
   same RBAC, ownership, validation and confirmation rules.

MCP client credentials do **not** authenticate browser/API routes outside MCP
and do **not** bypass JWT/session validation for normal access tokens.
Credentials are revoked immediately on explicit revocation, employee
deactivation, and global/session-revocation workflows that invalidate employee
access.

The resulting employee id and role are the only trusted identity. These are
never trusted as identity:

- employee ids supplied by an LLM or MCP tool arguments;
- display names;
- frontend claims;
- conversation memory;
- a shared MCP process identity.

The server resolves the `TaskActor` from the token and executes each tool inside
`use_task_actor(...)`. Existing RBAC and task ownership services still run:

- `ADMIN`: existing full permissions.
- `MANAGER`: existing operational permissions; no administrator-only actions.
- `EMPLOYEE`: own tasks only; cannot assign tasks to anybody else; cannot list
  or modify other employees' tasks.

Tool discovery is filtered by permission, and execution re-checks the
permission. Hiding a tool is never treated as authorization.

### OpenClaw (external MCP client)

OpenClaw 2026.10.10 can use a static bearer header for Streamable HTTP MCP.
Configure it with an issued `fnw_mcp_` credential (do not copy browser access
tokens):

```bash
openclaw mcp set fnw '{"url":"https://<tunnel>/mcp/","transport":"streamable-http","headers":{"Authorization":"Bearer fnw_mcp_<credential>"}}'
openclaw mcp doctor fnw --probe
```

Store the credential only in the operator/client secret store, use HTTPS/TLS,
and revoke it if exposed. Browser refresh/CSRF/JWT/session-rotation and
reuse-detection behavior remain unchanged.

## Tools

Tools are defined centrally in `mcp_server/registry/tool_registry.py` and
published with JSON Schema. Names and required permissions are unique.

### Task tools

- `create_task`
- `get_task`
- `list_tasks`
- `search_tasks`
- `update_task`
- `update_task_status`
- `update_task_progress`
- `update_task_deadline`
- `update_task_reminder`
- `create_task_reminder`
- `delete_task`

### Employee tools

- `get_current_employee`
- `get_employee_profile`
- `list_employees`
- `get_employee_permissions`

### Notification tools

- `list_notifications`
- `get_unread_notification_count`
- `mark_notification_read`
- `mark_all_notifications_read`

Notification tools call `services.notification_service`; reminders continue to
use the existing reminder fields and worker. There is no second scheduler.

### Database and agent tools

- `inspect_database_schema`
- `inspect_table_definition`
- `validate_sql_query`
- `execute_read_query`
- `retrieve_conversation_context`
- `update_conversation_context`

Only validated, read-only SQL is exposed. Raw writes and `DROP` are not MCP
tools. Task writes use the task-management tools, which preserve ownership,
validation and confirmation behavior.

## Resources and prompts

Resources expose existing documentation and skills under `fnw://` URIs:

- `fnw://docs/readme`
- `fnw://docs/database-schema`
- `fnw://docs/architecture`
- `fnw://docs/task-management`
- `fnw://docs/reminders`
- `fnw://skills/<skill-name>` for every `.agents/skills/*/SKILL.md`

Prompts expose reusable instructions:

- `task_creation`
- `task_update`
- `database_analysis`
- `reminder_management`

A skill is not automatically a tool. Skills remain instructions; only safe,
well-defined operations become tools.

## Confirmation rules

`update_*` and `delete_task` require the exact confirmation phrase in the
`confirmation` argument:

- `CONFIRM UPDATE`
- `CONFIRM DELETE`

When the in-process Cohere adapter invokes a write tool, it overwrites the
model's `confirmation` argument with the authenticated employee's actual
message. A model cannot invent or silently supply approval. An external MCP
client must obtain the phrase from the employee and pass it explicitly.

## Cohere integration

`ai/providers/cohere_provider.py` is the Cohere adapter. The agent loop in
`services/database_agent.py` asks the MCP client for tool definitions, converts
them once with `ai/mcp_client/tool_adapter.py`, sends them to Cohere, receives
tool calls, invokes them through the MCP server, and sends the structured result
back to Cohere until a final answer is produced.

There is no second hand-maintained Cohere tool registry. Adding a tool to the
MCP registry makes it discoverable without editing the Cohere adapter.

If the MCP server is unavailable, the agent returns a structured error; the
existing deterministic task flows remain the fallback when the agent cannot
start.

## Add a new MCP tool

1. Create a handler in `mcp_server/tools/` that calls an existing service.
2. Build an `MCPToolSpec` with a unique name, description, JSON Schema and
   required permissions.
3. Return the spec from that module's `build_*_tools()` function.
4. Register it in `mcp_server/server.py`.
5. Add protocol and authorization tests.

Do not put SQL, ownership checks, reminder scheduling or notification delivery
inside the MCP handler.

## Future OpenAI integration

Add `ai/providers/openai_provider.py` implementing `BaseAIProvider.chat(...)`.
Select it with `AI_PROVIDER=openai`. If the OpenAI API supports native remote MCP
directly, point it at `/mcp` with the user's bearer token. Otherwise reuse
`ai/mcp_client/client.py` and `ai/mcp_client/tool_adapter.py` exactly as Cohere
does. Business services and MCP schemas do not change.

## Connect an external MCP client

An external client needs an access token from the existing authentication
flow. Browser clients receive one from `/auth/refresh`; non-browser clients can
complete the same Google OIDC flow and retain the refresh cookie, then exchange
it for a short-lived access token at `/auth/refresh`.

```json
{
  "mcpServers": {
    "fnw-task-manager": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer <access-token>"
      }
    }
  }
}
```

For remote deployments use HTTPS, keep `MCP_REQUIRE_AUTH=true`, configure
`MCP_ALLOWED_HOSTS`/`MCP_ALLOWED_ORIGINS` with
`MCP_DNS_REBINDING_PROTECTION=true` when behind a known proxy, and apply rate
limiting at the ingress. Never place PostgreSQL credentials or provider API
keys in tool descriptions, resources, logs or MCP responses.
