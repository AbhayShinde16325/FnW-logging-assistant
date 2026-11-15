"""Regression tests for the extraction models.

The model is allowed to answer with ``null`` for a nested object. That must
never break the request, otherwise a read or an update simply fails.
"""

from __future__ import annotations

from ai.text_extractor import TaskQuery, TaskUpdate


def test_a_null_find_becomes_an_empty_criteria():
    update = TaskUpdate.model_validate(
        {"find": None, "changes": {"progress": 40}}
    )

    assert update.find.task_id is None
    assert update.find.task_title is None
    assert update.changes.progress == 40


def test_a_null_changes_becomes_an_empty_change_set():
    update = TaskUpdate.model_validate(
        {"find": {"task_id": 3}, "changes": None}
    )

    assert update.find.task_id == 3
    assert update.changes.progress is None
    assert update.changes.status is None


def test_a_null_filters_becomes_an_empty_filter_set():
    query = TaskQuery.model_validate(
        {
            "find": {"task_title": "report"},
            "filters": None,
        }
    )

    assert query.find.task_title == "report"
    assert query.filters.status is None
    assert query.filters.title_contains is None


def test_a_null_find_in_a_query_becomes_an_empty_criteria():
    query = TaskQuery.model_validate({"find": None, "filters": None})

    assert query.find.task_id is None
    assert query.filters is not None
