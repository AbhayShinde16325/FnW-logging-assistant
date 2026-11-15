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

## Quick Start (Docker)

On any server with Docker Engine and the Compose v2 plugin:

```bash
# 1. Get the code and create the environment file
git clone https://github.com/AbhayShinde16325/FnW-logging-assistant.git
cd FnW-logging-assistant
cp .env.example .env
```

Set `POSTGRES_PASSWORD` and `COHERE_API_KEY` in `.env`, then start everything:

```bash
# 2. Build and start PostgreSQL, the API, and the web UI
docker compose up -d --build
```

Then open `http://<server-ip>:8080`. See
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
POSTGRES_PASSWORD=change_me
COHERE_API_KEY=your_cohere_api_key_here
```

Docker Compose builds `DATABASE_URL` automatically and points it at the `db`
service, so it does not have to be edited for the Docker deployment.

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
```

If `FRONTEND_PORT` or `BACKEND_PORT` is already in use on the server, change
it in `.env`.

**Important:** Never commit your `.env` file or API keys to GitHub.

## 3. Run with Docker (recommended)

Prerequisites: Docker Engine with the Compose v2 plugin
(`docker compose version`).

Build and start the whole stack (`db`, `backend`, `frontend`) with one command:

```bash
docker compose up -d --build
```

That single command builds the images, starts PostgreSQL, waits for it to be
healthy, starts the API, and then serves the frontend through nginx.

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

## 5. Local Development without Docker

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

Run the test suite:

```bash
python -m pytest tests/ -q
```

The tests in `tests/test_task_intents_db.py` and `tests/test_task_flows_db.py`
run against the real PostgreSQL database and are skipped automatically when it
is not reachable. They only add rows and change the rows they added
themselves: every row carries a unique tag, and no test ever deletes a
record.

## 6. Application Workflow

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


## 7. API Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness check |
| `GET` | `/tasks` | Every stored task |
| `GET` | `/tasks/{task_id}` | One task |
| `POST` | `/tasks/text` | One turn of the text conversation |
| `POST` | `/tasks/chat` | One turn of the chat conversation |
| `POST` | `/tasks/voice` | One turn from an audio file |
| `POST` | `/tasks/voice/turn` | One turn from an audio file (conversation state) |

The conversation is stateless: the client sends the `stage` and the `draft`
it received in the previous answer back with the next message.

The chat and the voice route take two optional fields, `conversation_id` and
`user_id`. They are used for the conversation memory only: a client that does
not send them keeps the old, stateless behaviour.

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
| `reminder` / `confirm_reminder` | The reminder of the new task is being set |
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

## 8. Docker Operations Reference

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
