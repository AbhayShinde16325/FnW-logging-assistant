
from ai.text_extractor import TaskExtractor

from database.connection import create_tables
from database.task_repository import (
    create_task,
    update_task,
    delete_task,
    get_all_tasks
)


# Create database tables
create_tables()


def detect_operation(user_input):
    """
    Detect whether the user wants to CREATE, UPDATE or DELETE.
    """

    text = user_input.lower()

    delete_keywords = [
        "delete",
        "remove",
        "erase",
        "cancel task",
        "discard task"
    ]

    update_keywords = [
        "update",
        "modify",
        "change",
        "revise",
        "increase progress",
        "decrease progress",
        "mark as completed"
    ]

    if any(keyword in text for keyword in delete_keywords):
        return "DELETE"

    if any(keyword in text for keyword in update_keywords):
        return "UPDATE"

    return "CREATE"


def display_task(task):

    print("\nTASK DETAILS")
    print("-" * 40)

    print({
        "id": task.id,
        "task_title": task.task_title,
        "assigned_to": task.assigned_to,
        "assigned_date": str(task.assigned_date),
        "assigned_time": str(task.assigned_time),
        "deadline_date": str(task.deadline_date),
        "deadline_time": str(task.deadline_time),
        "status": task.status,
        "progress": task.progress
    })


def main():

    extractor = TaskExtractor()

    user_input = input("\nEnter your task description: ")

    try:

        # Step 1: Detect operation
        operation = detect_operation(user_input)

        print("\nDetected operation:", operation)

        # Step 2: Retrieve existing tasks
        if operation in ["UPDATE", "DELETE"]:

            existing_tasks = get_all_tasks()

            if not existing_tasks:
                print("No existing tasks found.")
                return

            print("\nExisting tasks:")

            for task in existing_tasks:
                print(
                    f"ID: {task.id} | "
                    f"Title: {task.task_title} | "
                    f"Assigned to: {task.assigned_to} | "
                    f"Progress: {task.progress}%"
                )

            task_id = int(input("\nEnter the task ID: "))

            # Step 3: DELETE operation
            if operation == "DELETE":

                confirmation = input(
                    f"Are you sure you want to delete task {task_id}? (yes/no): "
                ).lower()

                if confirmation != "yes":
                    print("Task deletion cancelled.")
                    return

                deleted = delete_task(task_id)

                if deleted:
                    print("\nTASK DELETED SUCCESSFULLY")
                else:
                    print("\nTask not found.")

                return

        # Step 4: Extract task details
        task_details = extractor.extract_task(user_input)

        print("\nAI EXTRACTED DATA:")
        print(task_details.model_dump(mode="json"))

        # Step 5: CREATE operation
        if operation == "CREATE":

            saved_task = create_task(task_details)

            print("\nNEW TASK CREATED SUCCESSFULLY")

            display_task(saved_task)

        # Step 6: UPDATE operation
        elif operation == "UPDATE":

            updated_task = update_task(
                task_id,
                task_details.model_dump()
            )

            if updated_task:
                print("\nTASK UPDATED SUCCESSFULLY")
                display_task(updated_task)
            else:
                print("\nTask not found.")

    except ValueError as error:
        print("Validation error:", error)

    except Exception as error:
        print("Application error:", error)


if __name__ == "__main__":
    main()
