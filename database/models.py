from sqlalchemy import Column, Integer, String, Date, Time
from database.connection import Base


class Task(Base):

    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)

    task_title = Column(String, nullable=False)

    assigned_to = Column(String, nullable=True)

    assigned_date = Column(Date, nullable=True)

    assigned_time = Column(Time, nullable=True)

    deadline_date = Column(Date, nullable=True)

    deadline_time = Column(Time, nullable=True)

    status = Column(String, default="PENDING")

    progress = Column(Integer, default=0)