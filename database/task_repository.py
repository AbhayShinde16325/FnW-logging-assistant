from database.connection import SessionLocal
from database.models import Task


def create_task(task_details):

    db = SessionLocal()

    try:
        # Supports both Pydantic models and dictionaries
        if hasattr(task_details, "model_dump"):
            task_data = task_details.model_dump()
        else:
            task_data = dict(task_details)

        # Ignore keys that have no matching column so the caller can pass
        # forward-compatible data without breaking the insert.
        task_data = {
            key: value
            for key, value in task_data.items()
            if hasattr(Task, key) and key != "id"
        }

        task = Task(**task_data)

        db.add(task)
        db.commit()
        db.refresh(task)

        return task

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


def get_task(task_id):

    db = SessionLocal()

    try:
        return db.query(Task).filter(
            Task.id == task_id
        ).first()

    finally:
        db.close()


def get_all_tasks():

    db = SessionLocal()

    try:
        return db.query(Task).order_by(Task.id).all()

    finally:
        db.close()


def update_task(task_id, updates):

    db = SessionLocal()

    try:
        task = db.query(Task).filter(
            Task.id == task_id
        ).first()

        if not task:
            return None

        if hasattr(updates, "model_dump"):
            updates = updates.model_dump(exclude_unset=True)

        for key, value in updates.items():

            if hasattr(Task, key) and key != "id":
                setattr(task, key, value)

        db.commit()
        db.refresh(task)

        return task

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


def delete_task(task_id):

    db = SessionLocal()

    try:
        task = db.query(Task).filter(
            Task.id == task_id
        ).first()

        if not task:
            return False

        db.delete(task)
        db.commit()

        return True

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()