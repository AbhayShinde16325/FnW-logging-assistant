import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

# Load .env from project root. Runtime/container env vars still take precedence.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _is_supabase_host(hostname: str | None) -> bool:
    host = (hostname or "").lower()
    return host.endswith(".supabase.co") or host.endswith(".supabase.com")


def _is_transaction_pooler(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    pool_mode = (query.get("pool_mode") or "").lower()
    if parts.port == 6543:
        return True
    if "pooler.supabase.com" in host and pool_mode == "transaction":
        return True
    return _env_bool("DB_DISABLE_PREPARED_STATEMENTS", False)


def normalize_database_url(url: str) -> str:
    """Normalize PostgreSQL URLs for psycopg3/SQLAlchemy.

    - Converts postgres:// and postgresql:// to postgresql+psycopg://.
    - Leaves explicit driver URLs unchanged.
    - Adds sslmode only when DB_SSLMODE is set, or when URL points to Supabase
      and sslmode is not already present.
    """

    raw = (url or "").strip()
    if not raw:
        raise ValueError("DATABASE_URL is missing or empty.")

    if raw.startswith("postgres://"):
        raw = "postgresql+psycopg://" + raw[len("postgres://") :]
    elif raw.startswith("postgresql://"):
        raw = "postgresql+psycopg://" + raw[len("postgresql://") :]

    parts = urlsplit(raw)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))

    sslmode = os.getenv("DB_SSLMODE")
    if sslmode and sslmode.strip():
        query.setdefault("sslmode", sslmode.strip())
    elif "sslmode" not in query and _is_supabase_host(parts.hostname):
        # Supabase-managed PostgreSQL requires TLS; do not override an explicit
        # user-provided sslmode.
        query["sslmode"] = "require"

    normalized = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
    return normalized


def database_connect_args(database_url: str) -> dict:
    connect_args: dict = {}

    connect_timeout = _env_int("DB_CONNECT_TIMEOUT")
    if connect_timeout is not None and connect_timeout > 0:
        connect_args["connect_timeout"] = connect_timeout

    # Supabase transaction pooler / PgBouncer transaction mode does not support
    # psycopg3 automatic prepared statements.
    if _is_transaction_pooler(database_url):
        connect_args["prepare_threshold"] = None

    return connect_args


def create_db_engine(database_url: str):
    normalized = normalize_database_url(database_url)
    connect_args = database_connect_args(normalized)

    options: dict = {
        "echo": _env_bool("DB_ECHO", False),
        "pool_pre_ping": _env_bool("DB_POOL_PRE_PING", True),
        "connect_args": connect_args,
    }

    if _is_transaction_pooler(normalized):
        # Transaction pooling provides its own pooling; SQLAlchemy client pooling
        # should be disabled/small to avoid stale connections and pooler limits.
        options["poolclass"] = NullPool
    else:
        pool_size = _env_int("DB_POOL_SIZE")
        max_overflow = _env_int("DB_MAX_OVERFLOW")
        pool_timeout = _env_int("DB_POOL_TIMEOUT")
        pool_recycle = _env_int("DB_POOL_RECYCLE")
        if pool_size is not None:
            options["pool_size"] = pool_size
        if max_overflow is not None:
            options["max_overflow"] = max_overflow
        if pool_timeout is not None:
            options["pool_timeout"] = pool_timeout
        if pool_recycle is not None:
            options["pool_recycle"] = pool_recycle

    return create_engine(normalized, **options)


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL is missing. Set DATABASE_URL in the environment or .env "
        "(for example a Supabase PostgreSQL URI)."
    )

engine = create_db_engine(DATABASE_URL)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)

Base = declarative_base()


def _add_column_if_missing(table: str, column: str, definition: str) -> None:
    """Safely add one application column to an existing database."""

    inspector = inspect(engine)

    try:
        columns = {
            value["name"] for value in inspector.get_columns(table)
        }
    except Exception:
        # The table does not exist yet; ``create_all`` already created it.
        return

    if column in columns:
        return

    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(
                text(
                    f"ALTER TABLE {table} "
                    f"ADD COLUMN IF NOT EXISTS {column} {definition}"
                )
            )
        else:
            connection.execute(
                text(
                    f"ALTER TABLE {table} "
                    f"ADD COLUMN {column} {definition}"
                )
            )


def _add_missing_columns():
    """Apply small, safe schema updates to an existing database.

    ``create_all`` creates missing tables but never adds a column to an
    existing one. New application columns are therefore added explicitly.
    """

    _add_column_if_missing(
        "conversation_memory", "response_feedback", "BOOLEAN"
    )
    _add_column_if_missing("tasks", "task_description", "VARCHAR")
    _add_column_if_missing(
        "tasks", "created_by_employee_id", "INTEGER"
    )
    _add_column_if_missing(
        "tasks", "assigned_to_employee_id", "INTEGER"
    )
    _add_column_if_missing(
        "conversation_memory", "employee_id", "INTEGER"
    )
    _add_column_if_missing(
        "employees",
        "timezone",
        "VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata'",
    )
    _add_column_if_missing(
        "sessions",
        "replaced_by_session_id",
        "VARCHAR",
    )


def create_tables():
    # Import models before creating tables
    from database import models

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    print("Database tables created successfully.")