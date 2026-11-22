"""Database-backed identity, session and RBAC operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import joinedload, selectinload

from auth.config import AuthSettings, settings as default_settings
from auth.permissions import (
    PERMISSION_DESCRIPTIONS,
    ROLE_ADMIN,
    ROLE_DESCRIPTIONS,
    ROLE_PERMISSIONS,
)
from auth.security import random_token, token_hash, utcnow
from database.connection import SessionLocal
from database.models import AuthSession, Employee, Permission, Role, RolePermission


class AuthenticationError(Exception):
    """No valid authenticated employee for the request."""


class ReuseDetected(AuthenticationError):
    """A revoked refresh token was presented again."""


class RotatedTokenError(AuthenticationError):
    """A revoked, already-rotated refresh token was presented again.

    This is treated as a benign duplicate (for example concurrent browser
    refresh requests racing with the same cookie) rather than token reuse, so
    unrelated active sessions must not be revoked. When the replacement
    session is still active, its identity is attached so the caller can issue
    an access token for it without mutating session state or cookies.
    """

    def __init__(
        self,
        message: str = "The session has already been rotated.",
        *,
        replacement_session_id: str | None = None,
        replacement_employee_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.replacement_session_id = replacement_session_id
        self.replacement_employee_id = replacement_employee_id


@dataclass(frozen=True)
class AuthenticatedEmployee:
    id: int
    name: str
    email: str
    role: str
    permissions: frozenset[str]
    timezone: str = "Asia/Kolkata"
    session_id: str | None = None

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def has_any_permission(self, permissions: Iterable[str]) -> bool:
        return bool(self.permissions & frozenset(permissions))

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "permissions": sorted(self.permissions),
            "timezone": self.timezone,
        }


def _permissions_for(employee: Employee) -> frozenset[str]:
    role = employee.role

    if role is None:
        return frozenset()

    return frozenset(permission.name for permission in role.permissions or ())


def _identity(employee: Employee, session_id: str | None = None) -> AuthenticatedEmployee:
    return AuthenticatedEmployee(
        id=int(employee.id),
        name=employee.name,
        email=employee.email,
        role=employee.role.name if employee.role else "",
        permissions=_permissions_for(employee),
        timezone=getattr(employee, "timezone", None) or "Asia/Kolkata",
        session_id=session_id,
    )


def _employee_query(session):
    return session.query(Employee).options(
        joinedload(Employee.role).selectinload(Role.permissions)
    )


def employee_by_id(employee_id: int) -> AuthenticatedEmployee | None:
    session = SessionLocal()

    try:
        employee = _employee_query(session).filter(
            Employee.id == employee_id,
            Employee.is_active.is_(True),
        ).first()

        return _identity(employee) if employee is not None else None

    finally:
        session.close()


def employee_by_email(email: str) -> AuthenticatedEmployee | None:
    session = SessionLocal()

    try:
        employee = _employee_query(session).filter(
            Employee.email == email.strip().lower()
        ).first()

        return _identity(employee) if employee is not None else None

    finally:
        session.close()


def find_or_link_google_employee(
    *,
    google_sub: str,
    email: str,
    name: str,
    auth_settings: AuthSettings | None = None,
) -> AuthenticatedEmployee | None:
    """Match a Google identity to a pre-provisioned active employee.

    The very first successful login links ``google_sub`` to the existing email
    row. An employee who already has a different Google subject is never
    silently relinked.
    """

    auth_settings = auth_settings or default_settings
    normalized_email = (email or "").strip().lower()
    subject = (google_sub or "").strip()

    if not normalized_email or not subject:
        return None

    if (
        auth_settings.employee_domain
        and not normalized_email.endswith("@" + auth_settings.employee_domain)
    ):
        return None

    session = SessionLocal()

    try:
        by_subject = _employee_query(session).filter(
            Employee.google_sub == subject
        ).first()
        by_email = _employee_query(session).filter(
            func.lower(Employee.email) == normalized_email
        ).first()

        if by_subject is not None:
            # A Google subject is bound to exactly one employee forever. If
            # the verified email now points at a different row, refuse rather
            # than silently reassigning the identity.
            if by_email is not None and by_email.id != by_subject.id:
                return None

            if by_subject.email.strip().lower() != normalized_email:
                return None

            employee = by_subject
        elif by_email is not None:
            employee = by_email

            if employee.google_sub and employee.google_sub != subject:
                return None
        else:
            return None

        if not employee.is_active:
            return None

        if not employee.google_sub:
            employee.google_sub = subject

        if name and not employee.name:
            employee.name = name

        employee.updated_at = utcnow()
        session.commit()
        session.refresh(employee)

        return _identity(employee)

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def session_active(session_id: str, employee_id: int) -> bool:
    if not session_id:
        return False

    session = SessionLocal()

    try:
        row = (
            session.query(AuthSession)
            .options(joinedload(AuthSession.employee))
            .filter(
                AuthSession.id == session_id,
                AuthSession.employee_id == employee_id,
            )
            .first()
        )

        return _is_session_usable_for_access(session, row, utcnow())

    finally:
        session.close()


# Concurrent browser refresh requests can race with the same rotated token.
# If the presented token was revoked very recently and its replacement session
# is still active, treat the replay as a benign duplicate instead of token
# reuse so unrelated active sessions are not revoked. Genuine reuse outside the
# grace window still revokes every active session for the employee.
ROTATION_DUPLICATE_GRACE_SECONDS = 30


def create_session(
    employee_id: int,
    *,
    auth_settings: AuthSettings | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, AuthSession]:
    auth_settings = auth_settings or default_settings
    refresh_token = random_token()
    now = utcnow()

    row = AuthSession(
        id=random_token(32),
        employee_id=employee_id,
        refresh_token_hash=token_hash(refresh_token),
        expires_at=now + timedelta(days=auth_settings.refresh_token_days),
        created_at=now,
        last_used_at=now,
        user_agent=(user_agent or "")[:500] or None,
        ip_address=(ip_address or "")[:100] or None,
    )

    session = SessionLocal()

    try:
        session.add(row)
        session.commit()
        session.refresh(row)

        return refresh_token, row

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def _recent_active_replacement_row(
    session, row: AuthSession, now: datetime
) -> AuthSession | None:
    replacement_id = getattr(row, "replaced_by_session_id", None)

    if not replacement_id or row.revoked_at is None:
        return None

    if now - row.revoked_at > timedelta(seconds=ROTATION_DUPLICATE_GRACE_SECONDS):
        return None

    replacement = (
        session.query(AuthSession)
        .filter(
            AuthSession.id == replacement_id,
            AuthSession.employee_id == row.employee_id,
            AuthSession.revoked_at.is_(None),
        )
        .first()
    )

    if (
        replacement
        and replacement.expires_at > now
        and replacement.employee_id == row.employee_id
    ):
        return replacement

    return None


def _recent_active_replacement(session, row: AuthSession, now: datetime) -> bool:
    return _recent_active_replacement_row(session, row, now) is not None


def _is_session_usable_for_access(session, row: AuthSession | None, now: datetime) -> bool:
    if not row or row.expires_at <= now or not row.employee or not row.employee.is_active:
        return False

    if row.revoked_at is None:
        return True

    # A just-rotated session stays usable for access-token validation during
    # the rotation grace window so a concurrent refresh in another tab/client
    # cannot invalidate an access token that was just issued for it.
    return _recent_active_replacement(session, row, now)


def rotate_refresh_session(
    refresh_token: str,
    *,
    auth_settings: AuthSettings | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, AuthSession]:
    """Validate, revoke and replace one refresh token atomically."""

    auth_settings = auth_settings or default_settings
    now = utcnow()
    hashed = token_hash(refresh_token or "")

    session = SessionLocal()

    try:
        row = (
            session.query(AuthSession)
            .options(joinedload(AuthSession.employee))
            .filter(AuthSession.refresh_token_hash == hashed)
            .with_for_update(of=AuthSession)
            .first()
        )

        if row is None:
            raise AuthenticationError("The session is not valid.")

        if row.revoked_at is not None:
            replacement = _recent_active_replacement_row(session, row, now)
            if replacement is not None:
                # A benign duplicate of an already-rotated request must not
                # rotate again, must not revoke the active session, and must
                # not clear cookies. The caller can issue a fresh access token
                # for the still-active replacement session without mutating it.
                raise RotatedTokenError(
                    "The session has already been rotated.",
                    replacement_session_id=replacement.id,
                    replacement_employee_id=int(replacement.employee_id),
                )

            # A rotated token being replayed outside the benign-duplicate
            # window is evidence of theft. Revoke every session of that
            # employee, not just the replayed row.
            session.query(AuthSession).filter(
                AuthSession.employee_id == row.employee_id,
                AuthSession.revoked_at.is_(None),
            ).update(
                {AuthSession.revoked_at: now},
                synchronize_session=False,
            )
            session.commit()
            raise ReuseDetected("The refresh token has been reused.")

        if row.expires_at <= now:
            row.revoked_at = now
            session.commit()
            raise AuthenticationError("The session has expired.")

        if row.employee is None or not row.employee.is_active:
            row.revoked_at = now
            session.commit()
            raise AuthenticationError("The employee is inactive.")

        row.revoked_at = now
        row.last_used_at = now

        new_token = random_token()
        new_row = AuthSession(
            id=random_token(32),
            employee_id=row.employee_id,
            refresh_token_hash=token_hash(new_token),
            expires_at=now + timedelta(days=auth_settings.refresh_token_days),
            created_at=now,
            last_used_at=now,
            user_agent=(user_agent or row.user_agent or "")[:500] or None,
            ip_address=(ip_address or row.ip_address or "")[:100] or None,
        )

        session.add(new_row)
        session.flush()

        if hasattr(row, "replaced_by_session_id"):
            row.replaced_by_session_id = new_row.id

        session.commit()
        session.refresh(new_row)

        return new_token, new_row

    except AuthenticationError:
        raise

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def revoke_refresh_session(refresh_token: str | None) -> bool:
    if not refresh_token:
        return False

    session = SessionLocal()

    try:
        row = (
            session.query(AuthSession)
            .filter(
                AuthSession.refresh_token_hash == token_hash(refresh_token),
                AuthSession.revoked_at.is_(None),
            )
            .first()
        )

        if row is None:
            return False

        row.revoked_at = utcnow()
        session.commit()

        return True

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def revoke_employee_sessions(employee_id: int) -> int:
    now = utcnow()
    session = SessionLocal()

    try:
        count = (
            session.query(AuthSession)
            .filter(
                AuthSession.employee_id == employee_id,
                AuthSession.revoked_at.is_(None),
            )
            .update(
                {AuthSession.revoked_at: now},
                synchronize_session=False,
            )
        )
        session.commit()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()

    # MCP client credentials are independent of browser/session state. They are
    # revoked only by explicit credential revocation or by employee/account
    # management actions that intentionally remove MCP access.
    return int(count or 0)


def employee_for_session(session_id: str, employee_id: int) -> AuthenticatedEmployee | None:
    if not session_active(session_id, employee_id):
        return None

    session = SessionLocal()

    try:
        employee = _employee_query(session).filter(
            Employee.id == employee_id,
            Employee.is_active.is_(True),
        ).first()

        return _identity(employee, session_id) if employee is not None else None

    finally:
        session.close()


def sync_rbac() -> None:
    """Create the canonical roles, permissions and role mappings."""

    session = SessionLocal()

    try:
        permission_rows = {
            row.name: row
            for row in session.query(Permission).all()
        }

        for name, description in PERMISSION_DESCRIPTIONS.items():
            row = permission_rows.get(name)

            if row is None:
                row = Permission(name=name, description=description)
                session.add(row)
                permission_rows[name] = row
            else:
                row.description = description

        role_rows = {
            row.name: row
            for row in session.query(Role).all()
        }

        for name, description in ROLE_DESCRIPTIONS.items():
            row = role_rows.get(name)

            if row is None:
                row = Role(name=name, description=description)
                session.add(row)
                role_rows[name] = row
            else:
                row.description = description

        session.flush()

        for role_name, permission_names in ROLE_PERMISSIONS.items():
            role = role_rows[role_name]
            wanted = {permission_rows[name].id for name in permission_names}
            existing = {
                link.permission_id
                for link in session.query(RolePermission).filter(
                    RolePermission.role_id == role.id
                )
            }

            for permission_id in wanted - existing:
                session.add(
                    RolePermission(
                        role_id=role.id,
                        permission_id=permission_id,
                    )
                )

            for permission_id in existing - wanted:
                session.query(RolePermission).filter(
                    RolePermission.role_id == role.id,
                    RolePermission.permission_id == permission_id,
                ).delete(synchronize_session=False)

        session.commit()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def bootstrap_admin(
    *,
    email: str,
    name: str = "Administrator",
    google_sub: str | None = None,
) -> AuthenticatedEmployee:
    """Create or promote the initial administrator.

    This is intentionally a command-line/controlled-environment operation;
    there is no public registration endpoint.
    """

    normalized_email = email.strip().lower()

    if not normalized_email or "@" not in normalized_email:
        raise ValueError("A valid administrator email is required.")

    sync_rbac()
    session = SessionLocal()

    try:
        admin_role = session.query(Role).filter(Role.name == ROLE_ADMIN).one()

        employee = session.query(Employee).filter(
            Employee.email == normalized_email
        ).first()

        if employee is None:
            employee = Employee(
                email=normalized_email,
                name=name.strip() or "Administrator",
                role_id=admin_role.id,
                is_active=True,
                google_sub=(google_sub or "").strip() or None,
            )
            session.add(employee)
        else:
            employee.role_id = admin_role.id
            employee.is_active = True

            if name.strip():
                employee.name = name.strip()

            if google_sub:
                employee.google_sub = google_sub.strip()

        employee.updated_at = utcnow()
        session.commit()
        session.refresh(employee)

        return _identity(employee)

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def list_employees() -> list[AuthenticatedEmployee]:
    session = SessionLocal()

    try:
        rows = _employee_query(session).order_by(Employee.name, Employee.id).all()
        return [_identity(row) for row in rows]

    finally:
        session.close()
