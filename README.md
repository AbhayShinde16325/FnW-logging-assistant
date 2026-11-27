# FnW Task Logging Assistant

A Python-based task logging application that uses AI to extract structured task details from natural-language descriptions and stores them in PostgreSQL. It also supports speech-to-text processing using Faster-Whisper.

## Tech Stack

- **Python** – Application development
- **Cohere API** (`command-a-plus-05-2026`) – Task information extraction
- **Faster-Whisper** – Audio transcription and speech-to-text
- **Pydantic** – Data validation
- **SQLAlchemy** – Database ORM
- **PostgreSQL 16** – Database
- **Docker & Docker Compose** – Containerization and database management
- **Web Push / VAPID** – browser and installed-PWA reminders

## Quick Start (Docker)

On any server with Docker Engine and the Compose v2 plugin:

```bash
# 1. Get the code and create the environment file
git clone https://github.com/AbhayShinde16325/FnW-logging-assistant.git
cd FnW-logging-assistant
cp .env.example .env
```

Set `DATABASE_URL` (Supabase/external PostgreSQL), `COHERE_API_KEY`,
`AUTH_SECRET_KEY`, `GOOGLE_CLIENT_ID`, and `GOOGLE_CLIENT_SECRET` in `.env`.
For Render/external IPv4 deployments, prefer a Supabase **Session Pooler**
URI if direct connectivity is incompatible. Do not assume transaction pooling.
To enable browser push, also generate a VAPID key pair:

```bash
# 2. Generate VAPID keys, merge the values into .env, and delete the temp file
python scripts/generate_vapid_keys.py --env-out /tmp/vapid.env
```

Then start everything:

```bash
# 3. Build and start the API, reminder worker, frontend, and tunnel (external DB)
docker compose up -d --build

# 4. Provision the first administrator (once)
docker compose exec backend python -m auth.bootstrap --email you@example.com
```

For isolated local development with a local PostgreSQL container:

```bash
docker compose -f docker-compose.yml -f docker-compose.local-db.yml up -d --build
```

Then open `http://<server-ip>:8080` and sign in with Google. See
[Authentication and roles](#authentication-and-roles) and
[Run with Docker](#3-run-with-docker-recommended) for configuration and
troubleshooting.

## 1. Project Setup

Clone the repository:

```bash
git clone https://github.com/AbhayShinde16325/FnW-logging-assistant.git
cd FnW-logging-assistant
```

Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## 2. Environment Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

Set at least these values in `.env`:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/postgres
COHERE_API_KEY=your_cohere_api_key_here
AUTH_SECRET_KEY=generate_a_long_random_value
GOOGLE_CLIENT_ID=your_google_oauth_client_id
GOOGLE_CLIENT_SECRET=your_google_oauth_client_secret
```

The default Docker Compose file uses this external `DATABASE_URL` (Supabase).
It does **not** start a local PostgreSQL container. For local isolated
development/testing, use `docker-compose.local-db.yml`, which provides a local
`db` service and overrides `DATABASE_URL` for backend/reminder-worker only.

Optional settings:

```env
FRONTEND_PORT=8080          # host port for the web UI
BACKEND_PORT=8000           # host port for the API
CORS_ORIGINS=http://localhost:5173
WHISPER_MODEL_SIZE=small
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
DATABASE_AGENT=1            # LLM tool-calling database agent
AGENTIC_READ=1              # let the read planner repair a zero-row query
TASK_REQUIRED_FIELDS=task_title,task_description,assigned_to,deadline_date,deadline_time

GOOGLE_REDIRECT_URI=http://localhost:8080/api/auth/google/callback
FRONTEND_URL=http://localhost:8080
AUTH_POST_LOGIN_REDIRECT=http://localhost:8080
AUTH_ALLOWED_EMAIL_DOMAIN=example.com   # optional work-email restriction
ACCESS_TOKEN_MINUTES=15
REFRESH_TOKEN_DAYS=14
AUTH_COOKIE_SECURE=false                # true on an HTTPS deployment
AUTH_COOKIE_SAME_SITE=lax
BOOTSTRAP_ADMIN_EMAIL=admin@example.com
BOOTSTRAP_ADMIN_NAME=Administrator

# Reminder worker and Web Push (optional, but required for browser push)
VAPID_PUBLIC_KEY=
VAPID_PRIVATE_KEY=
VAPID_SUBJECT=mailto:admin@example.com
REMINDER_POLL_SECONDS=20
REMINDER_BATCH_SIZE=100
REMINDER_MAX_RETRIES=5
REMINDER_MAX_AGE_HOURS=168
```

Generate the auth secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

If `FRONTEND_PORT` or `BACKEND_PORT` is already in use on the server, change
it in `.env`.

**Important:** Never commit your `.env` file or API keys to GitHub.

## MCP server and provider-independent AI

One MCP server implementation supports two modes:

- **Development**: local Docker + OpenAI tunnel client (`docker compose --profile dev up -d --build`).
- **Production**: Render direct HTTPS MCP endpoint at `https://fnw-logging-assistant.onrender.com/mcp` (no tunnel client/container).

```env
APP_ENV=development|production
MCP_ENABLED=true
MCP_TRANSPORT=streamable-http
MCP_SERVER_NAME=fnw-task-manager
MCP_PATH=/mcp
MCP_REQUIRE_AUTH=true
MCP_SERVER_URL=http://127.0.0.1:8000/mcp   # internal adapter URL, keep local
MCP_PUBLIC_RESOURCE_URL=https://fnw-logging-assistant.onrender.com  # production metadata URL
MCP_TUNNEL_ENABLED=true|false
```

`MCP_SERVER_URL` is internal-only for the Cohere/local MCP adapter. `MCP_PUBLIC_RESOURCE_URL` is used only for OAuth protected-resource metadata. MCP auth remains enabled in both modes.

See [docs/MCP_ARCHITECTURE.md](docs/MCP_ARCHITECTURE.md) for dev/prod commands, Render env vars, tunnel dev vars, auth model, and ChatGPT/direct MCP connection steps.

## Authentication and roles

Every task, chat, voice, feedback, and AI database-tool action requires a
signed-in employee. Authentication uses Google OpenID Connect Authorization
Code flow with PKCE. The backend validates the OAuth `state`, the OIDC
`nonce`, Google's RS256 signature, the issuer, the audience, the expiry, the
Google `sub`, and the verified email address. Only pre-provisioned, active
employees can sign in; there is no public registration.

### Google Cloud setup

1. Create an OAuth 2.0 Client ID of type **Web application** in Google Cloud
   Console.
2. Add the redirect URI for this deployment, for example:
   `http://localhost:8080/api/auth/google/callback` or
   `https://tasks.example.com/api/auth/google/callback`.
3. Put the client ID and secret in `.env` as `GOOGLE_CLIENT_ID` and
   `GOOGLE_CLIENT_SECRET`.
4. Set `GOOGLE_REDIRECT_URI`, `FRONTEND_URL`, and
   `AUTH_POST_LOGIN_REDIRECT` to the public URL of the frontend. On HTTPS,
   set `AUTH_COOKIE_SECURE=true`.

### Provisioning employees

Start the stack, then create the first administrator:

```bash
docker compose exec backend python -m auth.bootstrap \
  --email admin@example.com --name "Admin"
```

That command seeds the canonical roles and permissions and promotes or creates
the administrator. Add managers and employees through SQL or an internal admin
tool with a `roles.name` of `MANAGER` or `EMPLOYEE`. Deactivating an employee
(`employees.is_active = false`) immediately blocks new refreshes because the
backend rechecks the employee and session on every request.

### Roles

| Role | Capabilities |
| --- | --- |
| `ADMIN` | All task, employee, role, and report permissions |
| `MANAGER` | Manage all tasks and employees, view reports; cannot grant `ADMIN` or modify administrator accounts |
| `EMPLOYEE` | Read tasks assigned to their employee account and update only their progress |

Backend permission dependencies and the direct `/tasks` endpoints enforce
this. Task ownership uses `assigned_to_employee_id` only; display names never
authorize access. Employees may update progress through
`PATCH /tasks/{id}/progress`, while full edits and deletions require the
manager/admin permissions. The `0002_rbac_task_policy` migration fixes the
role mappings and backfills task and conversation-memory employee ids only
from unique, exact display-name matches.

### Sessions

- Short-lived JWT access tokens are held in frontend memory only.
- Rotating refresh tokens are random, hashed in the `sessions` table, and sent
  in an HttpOnly, Secure-configurable, SameSite cookie.
- Refresh cookies are rotated on every refresh. Replaying a revoked token
  revokes every session for that employee.
- Logout revokes one session; logout-all revokes every session of the
  employee.
- The refresh cookie is paired with a readable CSRF cookie and the
  `X-CSRF-Token` header on state-changing auth requests.

### Migrations

Alembic owns schema changes:

```bash
alembic upgrade head
```

The backend container runs `alembic upgrade head` automatically before
startup (`RUN_MIGRATIONS=1` by default). The reminder worker uses the same
`DATABASE_URL` but must keep `RUN_MIGRATIONS=0` so only one startup process
runs migrations. Existing task rows and volumes are preserved. `0001_auth_rbac`
creates the auth/RBAC tables and nullable employee-id columns;
`0002_rbac_task_policy` updates the canonical role mappings and safely
backfills ownership ids; `0003_reminder_notifications` adds
`employees.timezone`, `notifications`, and `push_subscriptions` with additive
inspector-guarded changes. `0004_intelligent_notification_fields` adds
notification `priority`, `voice_text`, and `payload` metadata for
attention/prioritization.

### Reminder notifications and Web Push

A task reminder is scheduled only when both `tasks.reminder_date` and
`tasks.reminder_time` are set. The employee's local wall-clock time is
resolved through `employees.timezone`, converted to naive UTC, and stored in
`notifications.scheduled_at`. The default timezone is `Asia/Kolkata`; each
employee can change their own timezone from the notification panel.

The separate `reminder-worker` Compose service polls PostgreSQL, claims due
rows with `FOR UPDATE SKIP LOCKED`, commits the claim, and only then delivers
push messages. It never holds a database transaction open during delivery.
Unsent reminders are cancelled when a task is rescheduled, reassigned,
completed, cancelled, or deleted, so stale reminders cannot fire later. The
worker also recovers stale claims and retries transient push failures with
bounded exponential backoff. Duplicate prevention is enforced by the unique
key `(employee_id, task_id, notification_type, scheduled_at)`.

The in-app notification center works without push. Browser push additionally
requires:

1. A VAPID key pair, configured on both the backend and worker as
   `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY` (or `VAPID_PRIVATE_KEY_PATH`).
   The private key is server-only and is never returned or logged.
2. HTTPS in production. Service workers and Push API require a secure
   context; `localhost` is the exception for development.
3. The employee enabling notifications from the bell menu. Devices and
   subscriptions are self-only: a manager or administrator cannot read
   another employee's notifications or register/remove their devices.
4. On iOS/iPadOS 16.4 or later, the site must be added to the Home Screen and
   notifications enabled from the installed web app. iOS does not expose Web
   Push to an ordinary Safari tab.

Generate keys once and keep the private key backed up and out of Git:

```bash
python scripts/generate_vapid_keys.py --env-out /tmp/vapid.env
# Merge the three lines into .env, then:
rm /tmp/vapid.env
```

The reminder worker can run more than once. Multiple workers coordinate through
the claim columns and will not send the same notification twice. To run a
single deterministic processing pass for operations/debugging:

```bash
REMINDER_WORKER_ONCE=1 python -m reminder_worker
```

**Sound:** the notification center can play a short optional sound while the
app is open and the browser permits audio. A website cannot bypass silent
mode, Do Not Disturb, OS notification settings, or autoplay restrictions; it
also cannot guarantee a sound for a background push.

**Testing limitation:** automated tests mock the push provider. Real delivery
still has to be verified in a real browser with a real subscription.

## 3. Run with Docker (recommended)

Prerequisites: Docker Engine with the Compose v2 plugin
(`docker compose version`).

Build and start the whole stack (`db`, `backend`, `reminder-worker`, `frontend`)
with one command:

```bash
docker compose up -d --build
```

That single command builds the images, starts PostgreSQL, waits for it to be
healthy, starts the API, starts the reminder worker, and then serves the
frontend through nginx.

Open the application:

| What | URL |
| --- | --- |
| Web UI | `http://<server-ip>:8080` |
| API docs | `http://<server-ip>:8000/docs` |
| Health check | `http://<server-ip>:8000/health` |
| API through the UI proxy | `http://<server-ip>:8080/api/health` |

Replace `<server-ip>` with the server's public or LAN address, and use your
configured `FRONTEND_PORT` / `BACKEND_PORT`. PostgreSQL is not exposed
publicly; only the frontend and API ports are published.

Check the stack:

```bash
docker compose ps
docker compose logs -f backend
docker compose logs -f reminder-worker
```

To start already-built images without rebuilding:

```bash
docker compose up -d --no-build
```

To rebuild and restart after pulling new code:

```bash
git pull
docker compose up -d --build
```

Stop the stack (data is kept):

```bash
docker compose down
```

Stop and delete the database and Whisper model cache:

```bash
docker compose down -v
```

## 4. Database Access

Connect to PostgreSQL inside the Docker stack:

```bash
docker compose exec db psql -U postgres -d fnw_task_logger
```

Useful SQL commands:

```sql
SELECT version();
\dt
SELECT * FROM tasks;
```

Exit PostgreSQL:

```sql
\q
```

## 5. Local Development with Docker Compose

The repository includes `docker-compose.yml` for the default external-database stack and `docker-compose.local-db.yml` as an overlay for isolated local PostgreSQL development.

For the dev stack with attached logs and the MCP tunnel enabled, run this from the project root:

```bash
docker compose --profile dev up
```

**What it does:**
- `--profile dev` enables services configured under the `dev` Docker Compose profile in `docker-compose.yml`. In the current Compose configuration, that includes `mcp-tunnel` (also profiled as `development`).
- `up` creates and starts the configured containers and attaches the terminal to their logs.
- With the current Compose configuration, this starts `backend`, `frontend`, `reminder-worker`, and `mcp-tunnel`. The default backend/frontend/reminder-worker services start because they have no profile; `mcp-tunnel` starts because the `dev` profile is enabled.
- Use `Ctrl+C` to stop the attached Compose process. This stops the attached services; containers may remain stopped rather than being removed. Inspect state with `docker compose ps`; use `docker compose down` only when you intentionally want to remove Compose-managed containers/network.

**Illustrative startup output:**

```text
Network task-flow-automations_taskflow Created
Container task-flow-automations-backend-1 Created
Container task-flow-automations-frontend-1 Created
Container task-flow-automations-reminder-worker-1 Created
Attaching to backend-1, frontend-1, mcp-tunnel-1, reminder-worker-1
```

**Important startup observations:**
1. `RUN_MIGRATIONS disabled; skipping Alembic migrations.` means automatic Alembic migrations were skipped by the container entrypoint. Do **not** describe this as migrations having run successfully. If schema changes must be applied, run `alembic upgrade head` intentionally using the project's migration strategy.
2. `Database initialized successfully` and `Database tables created successfully` indicate that the application's database initialization/table-creation process completed successfully. Treat this as application-level initialization; it is separate from Alembic migration execution.
3. Docker's orphan-container warning about `task-flow-automations-db-1` means a container exists for this Compose project but is not defined in the current Compose configuration. Investigate it before removing it (for example, confirm whether it was created by `docker-compose.local-db.yml` or an older workflow and whether it contains needed local data). Do **not** recommend or run `docker compose down --remove-orphans` blindly, because it may remove a container still needed by the developer.

**Dev tunnel prerequisites:** the `mcp-tunnel` service requires tunnel configuration/secrets, including `CONTROL_PLANE_API_KEY` and the mounted tunnel profile/authorization files described in `.env.example` and `docker-compose.yml`. For the detached core stack without the tunnel, use `docker compose up -d --build`. For isolated local PostgreSQL development, use `docker compose -f docker-compose.yml -f docker-compose.local-db.yml up -d --build`.

## 6. Local Development without Docker

Use this only when running the backend and frontend directly on the machine.
For a server deployment, use [Run with Docker](#3-run-with-docker-recommended)
instead.

Activate the virtual environment:

```bash
source venv/bin/activate
```

Start the backend (FastAPI, <http://localhost:8000>):

```bash
uvicorn app:app --reload --port 8000
```

Start the frontend (Vite, <http://localhost:5173>) in a second terminal:

```bash
cd frontend
npm install
npm run dev
```

The frontend talks to the backend on `http://localhost:8000`. Another port
(for example a test server) is used with the `VITE_API_BASE_URL` variable:

```bash
VITE_API_BASE_URL=http://localhost:8001 npm run dev
```

Run the database CRUD demo:

```bash
python -m database.crud_demo
```

Run the deterministic test suite (no live Cohere calls):

```bash
AGENTIC_READ=0 DATABASE_AGENT=0 python -m pytest tests -q
```

`AGENTIC_READ=1` uses a live model and is not deterministic. Run the separate
reminder worker while developing push locally:

```bash
REMINDER_WORKER_ONCE=1 python -m reminder_worker
# or, for a polling loop:
python -m reminder_worker
```

The tests in `tests/test_task_intents_db.py` and `tests/test_task_flows_db.py`
run against the real PostgreSQL database and are skipped automatically when it
is not reachable. They only add rows and change the rows they added
themselves: every row carries a unique tag, and no test ever deletes a
record.

## 7. Application Workflow

1. **Intent detection** – `ai/intent_detection.py` classifies the message as
   `CREATE_TASK`, `UPDATE_TASK`, `GET_TASK`, `LIST_TASKS`, `DELETE_TASK`,
   `GENERAL_QUERY` or `CLARIFICATION_REQUIRED`. A new task is never started
   by accident.
2. **Speech-to-Text** – Faster-Whisper converts audio input into text.
3. **Information Extraction** – Cohere extracts the structured task details.
4. **Validation** – Pydantic validates the extracted task details.
5. **Task identification** – the service finds the task the employee means
   with a natural-language search (see below) and asks which one to use when
   it is ambiguous.
6. **SQL generation and validation** – `database/sql_generator.py` builds a
   structured operation from the schema, and `database/sql_execution.py`
   validates and executes the parameterised statement.
7. **Storage** – PostgreSQL stores the task records.
8. **Conversation memory** – the task the employee is working on is stored in
   PostgreSQL, and a follow-up message is rewritten into a standalone
   instruction before the agent runs (see below).

### Task search

A lookup does not compare the whole sentence with one substring. Both the
query and the stored values are reduced to the same small set of concepts
(`database/search.py`):

- case and punctuation are ignored, and stop words ("do", "have", "a",
  "my", "task") carry no meaning,
- a small English stemmer handles plural and verb endings
  ("servers" -> "server", "researching" -> "research"),
- a conservative synonym map groups related words
  ("investigate", "study", "analysis" -> "research"),
- a small fuzzy comparison tolerates a spelling mistake ("reserch").

The database fetches the candidates with case-insensitive `ILIKE` terms
(title, assignee and status for a free-text query), then the candidates are
ranked by relevance: exact and partial hits, all concepts present and the
word order weigh most. Only the best candidates are shown, but the whole
ranked list stays in the draft so an exact task id can always be chosen.

Examples:

```
"do I have a task like research about MCP"  -> "Researching about MCP servers"
"MCP servers task"                          -> "Researching about MCP servers"
"investigate MCP servers"                   -> "Researching about MCP servers"
"reserch about mcp"                         -> "Researching about MCP servers"
```

`owner` is an exact ownership scope, so a lookup can never return another
employee's task, even when the title matches.

### Conversation memory and query rewriting

The assistant remembers the task of the conversation, so a follow-up message
does not have to name it again:

```
User: Update the progress of the business report to 10%.
Assistant: Which task do you mean?
User: Task #211.
Assistant: Task #211 updated successfully.
User: Also update its deadline to 21 November 2026.   -> "Update the deadline of task ID 211 to 21 November 2026."
User: No, change it to 25%.                            -> "Update the progress of task ID 211 to 25%."
```

- `services/conversation_memory.py` stores one row per user and conversation
  in the table `conversation_memory` (`active_task_id`, `last_action` and a
  small JSON `context`). Every user message and assistant reply is stored
  with a UTC timestamp. It survives a restart of the backend.
- Before a new message is sent to the model, the latest 10 messages are read
  from PostgreSQL (newest first, then put back into chronological order)
  together with the active task, the last action, the pending clarification
  and the task candidates that were displayed.
- `services/query_rewriter.py` rewrites a follow-up message with the existing
  Cohere model. The rewrite is checked in code: it may only use a task id
  that is already known (including a displayed candidate), and every value
  the employee stated has to survive the rewrite. When the context is not
  enough, the original message is used and the assistant asks for
  clarification.
- The change the employee asked for is kept while the task is still unknown
  (`pending` in the context). When the employee answers "Task #211", the
  pending change is completed on that task.
- The displayed candidates and the waiting clarification are stored too. A
  later "the second one" or "the previous one" is resolved from PostgreSQL,
  even after a restart; an explicit new request always wins over the stored
  clarification.
- The rewriting happens in `services/task_service.py`, right before the
  existing agent flow runs. The tools, the validation and the authorisation
  are unchanged: the rewritten instruction goes through the same path as any
  other message.
- A turn of a running conversation (the answer to a question of the
  assistant) is never rewritten.
- A new conversation never reuses the active task of an older one, and two
  users never share a memory.

The chat route takes two optional fields for this:

| Field | Meaning |
| --- | --- |
| `conversation_id` | The conversation the turn belongs to. A new id starts a new conversation. |
| `user_id` | The user of the conversation. The `employee` of the request is used when it is missing. |

### Intent rules

- An explicit creation request ("add a task", "create a task") is always a
  `CREATE_TASK`. It never looks an existing task up, whatever the wording of
  the task description is.
- An explicit update request ("update my existing task", "change its
  progress") is always an `UPDATE_TASK`. It never runs the new-task
  extraction.
- An update that refers to a task by a pronoun ("change its progress") only
  has a meaning when the conversation already identified a task, so the
  classifier is told about the context.
- A request that arrives while a conversation is still running replaces that
  conversation: an explicit creation starts a new task, an explicit update
  changes an existing one. The frontend therefore always takes the stage and
  the draft from the last answer, never from an earlier one.
- A message that only names an existing task ("Task #211") is the answer to
  "which task do you mean?". It never starts a new task.
- A rewritten read ("Retrieve the assigned date of task ID 211", "Get the
  deadline of the LangChain research task") is a `GET_TASK`, not a new task.
- The extraction model never gets to invent the task a request points at. When
  a request also names a title or an assignee, an id that the employee did not
  write is dropped and the title search is used instead.

### The structured output of the model

The extraction model answers with a structured object: the JSON schema of the
Pydantic model is sent with the request, so a task id is a number or null and
never an unquoted word.

Every JSON object of the answer is handed to the Pydantic model, and the
first one the model accepts is used. A value of the wrong type (a task id
that is not a number) is rejected by the model, never converted into another
value and never into another action.


## 8. API Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness check |
| `GET` | `/tasks` | Tasks visible to the signed-in employee |
| `GET` | `/tasks/{task_id}` | One visible task |
| `POST` | `/tasks` | Create a task (ADMIN/MANAGER) |
| `PATCH` | `/tasks/{task_id}` | Edit a task (ADMIN/MANAGER) |
| `PATCH` | `/tasks/{task_id}/progress` | Update progress on an assigned task |
| `DELETE` | `/tasks/{task_id}` | Delete a task with `CONFIRM DELETE` (ADMIN/MANAGER) |
| `POST` | `/tasks/text` | One turn of the text conversation |
| `POST` | `/tasks/chat` | One turn of the chat conversation |
| `POST` | `/tasks/voice` | One turn from an audio file |
| `POST` | `/tasks/voice/turn` | One turn from an audio file (conversation state) |
| `GET` | `/auth/google/login` | Start Google OIDC sign-in |
| `GET` | `/auth/google/callback` | Google redirect URI |
| `POST` | `/auth/refresh` | Rotate the refresh token and issue an access token |
| `POST` | `/auth/logout` | Revoke the current session |
| `POST` | `/auth/logout-all` | Revoke all sessions of the employee |
| `GET` | `/auth/me` | The signed-in employee and permissions |
| `GET` | `/notifications` | Self-only notification list and unread count |
| `GET` | `/notifications/unread-count` | Self-only unread count |
| `GET` | `/notifications/settings` | Self-only timezone and push status |
| `PATCH` | `/notifications/settings` | Update the employee's timezone |
| `PATCH` | `/notifications/{id}/read` | Mark one own notification read |
| `POST` | `/notifications/read-all` | Mark all own notifications read |
| `GET` | `/push/public-key` | Public VAPID key and push configuration status |
| `GET` | `/push/subscriptions` | List only the signed-in employee's devices |
| `POST` | `/push/subscribe` | Register the signed-in employee's subscription |
| `POST` | `/push/unsubscribe` | Remove the signed-in employee's subscription |

The conversation is stateless: the client sends the `stage` and the `draft`
it received in the previous answer back with the next message.

The chat and the voice route take an optional `conversation_id`. The backend
derives `user_id` from the authenticated employee, so a client can never read
or write another employee's conversation memory. `AUTH_REQUIRED=0` is only
for the automated test suite and keeps the legacy unauthenticated behaviour.

Every answer contains the `intent` that was detected, so the client can tell
a creation from an update:

```json
{
  "status": "success",
  "intent": "UPDATE_TASK",
  "stage": "update",
  "message": "Updated task #12 — Business report. Changed progress: 70.",
  "task": { "id": 12, "status": "IN_PROGRESS", "progress": 70 }
}
```

A task that does not exist is answered with the same keys, so a client can
always read `body["task"]`:

```json
{
  "status": "not_found",
  "intent": "GET_TASK",
  "message": "I couldn't find task #999.",
  "reply": "I couldn't find task #999.",
  "task": null,
  "tasks": []
}
```

`GET /tasks/{task_id}` answers such a request with the HTTP status `404` and
the body above.

### Conversation stages

| Stage | Meaning |
| --- | --- |
| `fields` | A new task is being collected, one field at a time |
| `confirm_fields` | The new task is waiting for a confirmation |
| `reminder` / `confirm_reminder` | The reminder of the new task is being set; it is optional and is skipped when the employee says it is not required |
| `select_task` | Several tasks match; the employee has to pick one |
| `confirm_update` | An update is waiting for the missing value or a confirmation |
| `confirm_delete` | A delete is waiting for `CONFIRM DELETE` |

### Database safety

- Only the `tasks` table is used, and every value is bound as a parameter.
- The model never chooses a table, a column, a task id or a value: the
  schema rules live in `database/sql_generator.py`.
- An `UPDATE` always carries `id = :task_id` and, when the employee is
  known, `assigned_to = :assigned_to`.
- A `DELETE` only runs after the exact phrase `CONFIRM DELETE`.
- The SQL generation skill (`.agents/skills/sql-generation/SKILL.md`) is
  implemented in Python, so the running API obeys the same rules.

## 9. Docker Operations Reference

| Task | Command |
| --- | --- |
| Build and start everything | `docker compose up -d --build` |
| Start existing images | `docker compose up -d --no-build` |
| Show status | `docker compose ps` |
| Follow backend logs | `docker compose logs -f backend` |
| Follow all logs | `docker compose logs -f` |
| Restart the stack | `docker compose restart` |
| Stop, keep data | `docker compose down` |
| Stop, delete database and model cache | `docker compose down -v` |

The Docker stack contains three services: `db` (PostgreSQL 16), `backend`
(FastAPI + Faster-Whisper), and `frontend` (React build served by nginx).
The frontend proxies `/api/` to the backend, so both are reachable from the
same public host.
