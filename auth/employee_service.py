"""Employee management operations and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from auth.audit import log_employee_event
from auth.permissions import ROLE_ADMIN, ROLES_MANAGE, role_names
from auth.security import utcnow
from auth.service import AuthenticatedEmployee
from database.connection import SessionLocal
from database.models import AuthSession, Employee, Role


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
STATUS_ALIASES = {
    "active": "active",
    "inactive": "inactive",
    "not_logged_in": "not_logged_in",
    "not-logged-in": "not_logged_in",
    "pending": "not_logged_in",
}


class EmployeeManagementError(Exception):
    status_code = 400


class InvalidEmployeeData(EmployeeManagementError):
    status_code = 400


class EmployeeNotFound(EmployeeManagementError):
    status_code = 404


class DuplicateEmployeeEmail(EmployeeManagementError):
    status_code = 409


class LastActiveAdminError(EmployeeManagementError):
    status_code = 409


class SelfRoleChangeError(EmployeeManagementError):
    status_code = 403


class AdminPrivilegeError(EmployeeManagementError):
    status_code = 403


@dataclass(frozen=True)
class EmployeeRecord:
    id: int
    name: str
    email: str
    role: str
    is_active: bool
    google_sub: str | None
    created_at: datetime | None
    updated_at: datetime | None

    @property
    def has_logged_in(self) -> bool:
        return bool(self.google_sub)

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "is_active": self.is_active,
            "has_logged_in": self.has_logged_in,
            "google_linked": self.has_logged_in,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass(frozen=True)
class EmployeeActionResult:
    employee: EmployeeRecord
    revoked_sessions: int = 0

    def to_payload(self) -> dict:
        return {
            "employee": self.employee.to_payload(),
            "revoked_sessions": self.revoked_sessions,
        }


def _record(employee: Employee) -> EmployeeRecord:
    return EmployeeRecord(
        id=int(employee.id),
        name=employee.name,
        email=employee.email,
        role=employee.role.name if employee.role else "",
        is_active=bool(employee.is_active),
        google_sub=employee.google_sub,
        created_at=employee.created_at,
        updated_at=employee.updated_at,
    )


def _normalize_name(value: str | None) -> str:
    name = (value or "").strip()

    if not name:
        raise InvalidEmployeeData("A full name is required.")

    if len(name) > 255:
        raise InvalidEmployeeData("The full name is too long.")

    return name


def _normalize_email(value: str | None) -> str:
    email = (value or "").strip().lower()

    if not email or not EMAIL_PATTERN.match(email):
        raise InvalidEmployeeData("A valid work email is required.")

    if len(email) > 320:
        raise InvalidEmployeeData("The email address is too long.")

    return email


def _normalize_role(value: str | None) -> str:
    role = (value or "").strip().upper()

    if role not in role_names():
        raise InvalidEmployeeData(
            "Role must be one of: " + ", ".join(role_names()) + "."
        )

    return role


def _role_row(session, name: str) -> Role:
    role = session.query(Role).filter(Role.name == name).first()

    if role is None:
        raise InvalidEmployeeData("The selected role does not exist.")

    return role


def _can_manage_admins(actor: AuthenticatedEmployee) -> bool:
    return actor.has_permission(ROLES_MANAGE)


def _guard_role_grant(actor: AuthenticatedEmployee, role: str) -> None:
    if role == ROLE_ADMIN and not _can_manage_admins(actor):
        raise AdminPrivilegeError(
            "Only an administrator can grant the administrator role."
        )


def _guard_admin_target(actor: AuthenticatedEmployee, employee: Employee) -> None:
    if (
        employee.role is not None
        and employee.role.name == ROLE_ADMIN
        and not _can_manage_admins(actor)
    ):
        raise AdminPrivilegeError(
            "Only an administrator can modify an administrator account."
        )


def _load_employee(session, employee_id: int) -> Employee:
    employee = (
        session.query(Employee)
        .options(joinedload(Employee.role))
        .filter(Employee.id == employee_id)
        .first()
    )

    if employee is None:
        raise EmployeeNotFound("The employee was not found.")

    return employee


def _guard_last_active_admin(session, employee: Employee) -> None:
    if (
        employee.role is None
        or employee.role.name != ROLE_ADMIN
        or not employee.is_active
    ):
        return

    other_admins = (
        session.query(Employee.id)
        .join(Role, Role.id == Employee.role_id)
        .filter(
            Role.name == ROLE_ADMIN,
            Employee.is_active.is_(True),
            Employee.id != employee.id,
        )
        .count()
    )

    if other_admins == 0:
        raise LastActiveAdminError(
            "The last active administrator cannot be deactivated or demoted."
        )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _summary(session) -> dict:
    total = session.query(Employee).count()
    active = session.query(Employee).filter(Employee.is_active.is_(True)).count()
    not_logged_in = session.query(Employee).filter(
        Employee.google_sub.is_(None)
    ).count()

    return {
        "total": total,
        "active": active,
        "inactive": total - active,
        "not_logged_in": not_logged_in,
    }


def list_employees(
    *,
    search: str | None = None,
    role: str | None = None,
    status: str | None = None,
) -> dict:
    session = SessionLocal()

    try:
        summary = _summary(session)
        query = session.query(Employee).options(joinedload(Employee.role))

        search_value = (search or "").strip()

        if search_value:
            pattern = f"%{_escape_like(search_value)}%"
            query = query.filter(
                or_(
                    Employee.name.ilike(pattern, escape="\\"),
                    Employee.email.ilike(pattern, escape="\\"),
                )
            )

        role_value = (role or "").strip().upper()

        if role_value:
            if role_value not in role_names():
                raise InvalidEmployeeData("Unknown role filter.")

            query = query.join(Role, Role.id == Employee.role_id).filter(
                Role.name == role_value
            )

        status_value = (status or "").strip().lower()

        if status_value:
            normalized_status = STATUS_ALIASES.get(status_value)

            if normalized_status is None:
                raise InvalidEmployeeData("Unknown status filter.")

            if normalized_status == "active":
                query = query.filter(Employee.is_active.is_(True))
            elif normalized_status == "inactive":
                query = query.filter(Employee.is_active.is_(False))
            else:
                query = query.filter(Employee.google_sub.is_(None))

        rows = query.order_by(Employee.name, Employee.id).all()

        return {
            "employees": [_record(row).to_payload() for row in rows],
            "roles": list(role_names()),
            **summary,
        }

    finally:
        session.close()


def get_employee(employee_id: int) -> EmployeeRecord:
    session = SessionLocal()

    try:
        return _record(_load_employee(session, employee_id))

    finally:
        session.close()


def create_employee(
    *,
    actor: AuthenticatedEmployee,
    name: str,
    email: str,
    role: str,
) -> EmployeeRecord:
    normalized_name = _normalize_name(name)
    normalized_email = _normalize_email(email)
    normalized_role = _normalize_role(role)
    _guard_role_grant(actor, normalized_role)
    session = SessionLocal()

    try:
        role_row = _role_row(session, normalized_role)
        duplicate = (
            session.query(Employee.id)
            .filter(func.lower(Employee.email) == normalized_email)
            .first()
        )

        if duplicate is not None:
            raise DuplicateEmployeeEmail(
                "An employee with this email already exists."
            )

        employee = Employee(
            name=normalized_name,
            email=normalized_email,
            role_id=role_row.id,
            is_active=True,
            google_sub=None,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(employee)
        session.commit()
        session.refresh(employee)
        result = _record(_load_employee(session, employee.id))

    except IntegrityError:
        session.rollback()
        raise DuplicateEmployeeEmail(
            "An employee with this email already exists."
        )

    except Exception:
        session.rollback()
        raise

    else:
        log_employee_event("employee.created", actor=actor, target=result)
        return result

    finally:
        session.close()


def update_employee(
    *,
    actor: AuthenticatedEmployee,
    employee_id: int,
    name: str | None = None,
    role: str | None = None,
) -> EmployeeRecord:
    session = SessionLocal()

    try:
        employee = _load_employee(session, employee_id)
        _guard_admin_target(actor, employee)
        old_name = employee.name
        old_role = employee.role.name if employee.role else ""
        changed = False

        if name is not None:
            normalized_name = _normalize_name(name)

            if normalized_name != employee.name:
                employee.name = normalized_name
                changed = True

        role_changed = False

        if role is not None:
            normalized_role = _normalize_role(role)

            if normalized_role != old_role:
                if employee.id == actor.id:
                    raise SelfRoleChangeError(
                        "You cannot change your own role."
                    )

                _guard_role_grant(actor, normalized_role)
                role_row = _role_row(session, normalized_role)
                _guard_last_active_admin(session, employee)
                employee.role_id = role_row.id
                role_changed = True
                changed = True

        if changed:
            employee.updated_at = utcnow()
            session.commit()
            session.refresh(employee)
        result = _record(_load_employee(session, employee.id))

    except Exception:
        session.rollback()
        raise

    else:
        if old_name != result.name:
            log_employee_event(
                "employee.name_updated",
                actor=actor,
                target=result,
                details={"from": old_name, "to": result.name},
            )

        if role_changed:
            log_employee_event(
                "employee.role_changed",
                actor=actor,
                target=result,
                details={"from": old_role, "to": result.role},
            )

        return result

    finally:
        session.close()


def _set_active(
    *,
    actor: AuthenticatedEmployee,
    employee_id: int,
    active: bool,
) -> EmployeeActionResult:
    session = SessionLocal()

    try:
        employee = _load_employee(session, employee_id)
        _guard_admin_target(actor, employee)
        changed = employee.is_active is not active

        if not active:
            _guard_last_active_admin(session, employee)

        if changed:
            employee.is_active = active
            employee.updated_at = utcnow()

        revoked = 0

        if not active:
            revoked = (
                session.query(AuthSession)
                .filter(
                    AuthSession.employee_id == employee.id,
                    AuthSession.revoked_at.is_(None),
                )
                .update(
                    {AuthSession.revoked_at: utcnow()},
                    synchronize_session=False,
                )
            )

        session.commit()
        session.refresh(employee)
        result = EmployeeActionResult(
            employee=_record(_load_employee(session, employee.id)),
            revoked_sessions=int(revoked or 0),
        )

    except Exception:
        session.rollback()
        raise

    else:
        if not active:
            from auth.mcp_credentials import revoke_mcp_credentials_for_employee

            revoke_mcp_credentials_for_employee(employee.id)

        if changed:
            log_employee_event(
                "employee.activated" if active else "employee.deactivated",
                actor=actor,
                target=result.employee,
                details={"revoked_sessions": result.revoked_sessions},
            )

        return result

    finally:
        session.close()


def deactivate_employee(
    *,
    actor: AuthenticatedEmployee,
    employee_id: int,
) -> EmployeeActionResult:
    return _set_active(actor=actor, employee_id=employee_id, active=False)


def activate_employee(
    *,
    actor: AuthenticatedEmployee,
    employee_id: int,
) -> EmployeeActionResult:
    return _set_active(actor=actor, employee_id=employee_id, active=True)


def revoke_sessions_for_employee(
    *,
    actor: AuthenticatedEmployee,
    employee_id: int,
) -> EmployeeActionResult:
    session = SessionLocal()

    try:
        employee = _load_employee(session, employee_id)
        _guard_admin_target(actor, employee)
        revoked = (
            session.query(AuthSession)
            .filter(
                AuthSession.employee_id == employee.id,
                AuthSession.revoked_at.is_(None),
            )
            .update(
                {AuthSession.revoked_at: utcnow()},
                synchronize_session=False,
            )
        )
        session.commit()
        result = EmployeeActionResult(
            employee=_record(_load_employee(session, employee.id)),
            revoked_sessions=int(revoked or 0),
        )

    except Exception:
        session.rollback()
        raise

    else:
        from auth.mcp_credentials import revoke_mcp_credentials_for_employee

        revoke_mcp_credentials_for_employee(employee.id)
        log_employee_event(
            "employee.sessions_revoked",
            actor=actor,
            target=result.employee,
            details={"revoked_sessions": result.revoked_sessions},
        )
        return result

    finally:
        session.close()
