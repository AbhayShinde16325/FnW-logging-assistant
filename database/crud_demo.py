from datetime import date, time

from database.connection import create_tables
from database.task_repository import (
    create_task,
    get_task,
    get_all_tasks,
    update_task,
    delete_task
)


# Create database table
create_tables()


# -------------------------
# CREATE
# -------------------------

new_task = {
    "task_title": "Python API Research",
    "assigned_to": "Rahul",
    "assigned_date": date(2026, 11, 10),
    "assigned_time": time(10, 30),
    "deadline_date": date(2026, 11, 15),
    "deadline_time": time(17, 0),
    "status": "IN_PROGRESS",
    "progress": 50
}

created_task = create_task(new_task)

print("\nTASK CREATED")
print("ID:", created_task.id)
print("Title:", created_task.task_title)


# -------------------------
# READ ONE TASK
# -------------------------

task = get_task(created_task.id)

print("\nTASK DETAILS")
print(task.task_title)
print(task.assigned_to)
print(task.deadline_date)
print(task.progress)


# -------------------------
# READ ALL TASKS
# -------------------------

tasks = get_all_tasks()

print("\nALL TASKS")

for task in tasks:
    print(
        task.id,
        task.task_title,
        task.status,
        task.progress
    )


# -------------------------
# UPDATE
# -------------------------

updated_task = update_task(
    created_task.id,
    {
        "progress": 75,
        "status": "IN_PROGRESS"
    }
)

print("\nTASK UPDATED")
print("Progress:", updated_task.progress)


# -------------------------
# DELETE
# -------------------------

deleted = delete_task(created_task.id)

if deleted:
    print("\nTASK DELETED SUCCESSFULLY")
else:
    print("\nTASK NOT FOUND")