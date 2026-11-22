import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

# Load .env from project root
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL is missing from .env")

engine = create_engine(
    DATABASE_URL,
    echo=False
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False
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