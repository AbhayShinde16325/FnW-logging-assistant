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

Create a `.env` file:

```env
DATABASE_URL=postgresql+psycopg://postgres:YOUR_PASSWORD@localhost:5432/fnw_task_logger
COHERE_API_KEY=YOUR_COHERE_API_KEY
```

Use the same PostgreSQL password configured in Docker Compose.

**Important:** Never commit your `.env` file or API keys to GitHub.

## 3. Docker Setup

Docker Desktop is installed on Windows with Ubuntu WSL2 integration enabled.

Build the Docker services:

```bash
docker compose build
```

Start PostgreSQL:

```bash
docker compose up -d
```

Verify running containers:

```bash
docker ps
```

Check PostgreSQL logs:

```bash
docker compose logs -f db
```

## 4. Database Access

Connect to PostgreSQL inside Docker:

```bash
docker exec -it fnw-postgres psql -U postgres -d fnw_task_logger
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

## 5. Running the Application

Activate the virtual environment:

```bash
source venv/bin/activate
```

Run database CRUD tests:

```bash
python -m database.crud_demo
```

Run the AI and database integration test:

```bash
python test_integration.py
```

Run the main application:

```bash
python app.py
```

Enter a natural-language task description when prompted.

Example:

```text
Assign Priya Sharma a task to develop a REST API
for the employee management system.

Deadline: November 18, 2026, at 5 PM.
Status: IN_PROGRESS.
Progress: 40%.
```

## 6. Application Workflow

1. **Input:** Employee provides a task description through text or audio.
2. **Speech-to-Text:** Faster-Whisper converts audio input into text.
3. **Information Extraction:** Cohere extracts structured task information.
4. **Validation:** Pydantic validates the extracted task details.
5. **Database Operations:** SQLAlchemy handles CREATE, READ, UPDATE and DELETE operations.
6. **Storage:** PostgreSQL stores the task records.

## 7. Stop Docker Services

Stop containers:

```bash
docker compose down
```

To remove containers and the database volume (permanently deletes stored database data):

```bash
docker compose down -v
```

---

**Note:** The current Docker Compose configuration starts PostgreSQL. The Python backend currently runs locally through the virtual environment, not as a Docker container. The audio workflow assumes Faster-Whisper is integrated into the application; ensure the transcription component is configured before testing audio input.