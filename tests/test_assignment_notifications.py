from types import SimpleNamespace
from datetime import date, time, datetime

import services.task_management as tm


class FakeNotifications:
    def __init__(self):
        self.calls = []
        self.TYPE_TASK_ASSIGNED = 'TASK_ASSIGNED'
        self.PRIORITY_HIGH = 'HIGH'
    def utcnow(self):
        return datetime(2026, 11, 27, 10, 5, 30)
    def create_task_event_notification(self, *args, **kwargs):
        if args:
            # positional: task_id, employee_id, notification_type
            kwargs.setdefault('task_id', args[0])
            if len(args) > 1:
                kwargs.setdefault('employee_id', args[1])
            if len(args) > 2:
                kwargs.setdefault('notification_type', args[2])
        self.calls.append(kwargs)
        return SimpleNamespace(id=len(self.calls), **kwargs)


def test_notify_assignment_includes_manager_and_missing_fields(monkeypatch):
    fake = FakeNotifications()
    monkeypatch.setattr(tm, 'notification_service', fake)
    monkeypatch.setattr(tm, 'current_actor', lambda: SimpleNamespace(id=7, name='Shreyansh Dixit', email='s@example.com'))
    task = SimpleNamespace(id=101, task_title='Learn Java Backend', task_description='Learn Java backend.', assigned_to='Krishna', assigned_to_employee_id=55, deadline_date=None, deadline_time=None, progress=0, status='PENDING')
    tm._notify_assignment(task)
    assert len(fake.calls) == 1
    c = fake.calls[0]
    assert c['employee_id'] == 55
    assert c['notification_type'] == 'TASK_ASSIGNED'
    assert 'Shreyansh Dixit assigned you a new task' in c['message']
    assert 'Deadline: Not specified.' in c['message']
    assert 'Missing information:' in c['message']
    assert c['payload']['created_by_name'] == 'Shreyansh Dixit'
    assert c['payload']['has_missing_fields'] is True
    assert 'deadline_date' in c['payload']['missing_fields']
    assert c['payload']['context_source'] == 'task_assignment_notification'


def test_notify_assignment_no_duplicate_on_same_assignee(monkeypatch):
    fake = FakeNotifications()
    monkeypatch.setattr(tm, 'notification_service', fake)
    monkeypatch.setattr(tm, 'current_actor', lambda: SimpleNamespace(id=7, name='Mgr'))
    task = SimpleNamespace(id=101, task_title='T', task_description='D', assigned_to='A', assigned_to_employee_id=55, deadline_date=date(2026,11,30), deadline_time=time(17,0), progress=0, status='PENDING')
    tm._notify_assignment(task, previous_assignee_id=55)
    assert fake.calls == []
