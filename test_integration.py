"""Manual integration script for the AI extraction + database flow.

This file is intentionally not a pytest test module. It requires configured AI
credentials and a reachable database, so it only runs when executed directly.
"""

from __future__ import annotations


def main() -> None:
    from ai.text_extractor import TaskExtractor
    from database.connection import create_tables
    from database.task_repository import create_task, get_all_tasks

    create_tables()

    extractor = TaskExtractor()

    user_input = """
Update Rahul's existing Python API research task.

The task was previously 50% completed.
He has now completed 80% of the work.

Keep the original assignment date as 10 November 2026 at 10:30 AM.
Keep the original deadline as 15 November 2026 at 5 PM.

Update the existing task instead of creating a new task.
"""

    task_details = extractor.extract_task(user_input)

    print("\nAI EXTRACTED DATA:")
    print(task_details.model_dump(mode="json"))

    saved_task = create_task(task_details)

    print("\nTASK SAVED TO DATABASE:")
    print("ID:", saved_task.id)
    print("Title:", saved_task.task_title)
    print("Assigned to:", saved_task.assigned_to)
    print("Progress:", saved_task.progress)

    print("\nALL DATABASE TASKS:")
    for task in get_all_tasks():
        print(
            task.id,
            task.task_title,
            task.assigned_to,
            task.status,
            task.progress,
        )


if __name__ == "__main__":
    main()
