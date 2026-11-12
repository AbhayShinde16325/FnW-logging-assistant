"""Contract tests for the model-independent SQL generation skill files."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS_MD = ROOT / "AGENTS.md"
SKILL_MD = ROOT / ".agents" / "skills" / "sql-generation" / "SKILL.md"
SCHEMA_MD = ROOT / "DATABASE_SCHEMA.md"
EXECUTION_PY = ROOT / "database" / "sql_execution.py"


def test_agent_instructions_exist_and_state_confirmation_rules():
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "DATABASE_SCHEMA.md" in text
    assert "UPDATE" in text
    assert "DELETE" in text
    assert "DROP TABLE" in text
    assert "confirmation" in text.lower()
    assert "database.sql_execution" in text


def test_skill_file_exists_and_is_provider_independent():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "DATABASE_SCHEMA.md" in text
    assert "CONFIRM UPDATE" in text
    assert "CONFIRM DELETE" in text
    assert "CONFIRM DROP TABLE tasks" in text
    assert "TRUNCATE" in text
    assert "ALTER" in text
    assert "CREATE DATABASE" in text

    lowered = text.lower()
    for provider in ("cohere", "opencode", "openai", "anthropic"):
        assert provider not in lowered


def test_schema_doc_keeps_schema_and_states_new_policy():
    text = SCHEMA_MD.read_text(encoding="utf-8")
    assert "tasks" in text
    assert "task_title" in text
    assert "assigned_to" in text
    assert "DROP TABLE tasks" in text
    assert "DELETE FROM tasks" in text
    assert "confirmation" in text.lower()


def test_execution_layer_module_exists():
    assert EXECUTION_PY.exists()
    text = EXECUTION_PY.read_text(encoding="utf-8")
    assert "run_sql_request" in text
    assert "CONFIRM DELETE" in text
