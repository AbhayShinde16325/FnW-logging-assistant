"""FastAPI authentication and permission dependencies."""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import auth.config as auth_config
from auth.service import (
    AuthenticatedEmployee,
    employee_for_session,
)
from auth.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str = "Authentication required.") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_employee(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedEmployee | None:
    """Resolve the access token to an active employee.

    When authentication is disabled for local tests/development, no token is
    accepted as a principal; endpoints then keep their legacy behaviour. In
    production (``AUTH_REQUIRED=1``) every protected endpoint requires a token.
    """

    settings = auth_config.settings

    if credentials is None or not credentials.credentials:
        if settings.required:
            raise _unauthorized()
        return None

    try:
        payload = decode_access_token(credentials.credentials, settings)
        employee_id = int(payload.get("sub") or 0)
        session_id = str(payload.get("sid") or "")
    except Exception:
        raise _unauthorized("The access token is invalid or expired.")

    employee = employee_for_session(session_id, employee_id)

    if employee is None:
        raise _unauthorized("The session is no longer active.")

    return employee


def require_permission(permission: str):
    def _dependency(
        employee: AuthenticatedEmployee | None = Depends(get_current_employee),
    ) -> AuthenticatedEmployee | None:
        if employee is None:
            if auth_config.settings.required:
                raise _unauthorized()
            return None

        if not employee.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action.",
            )

        return employee

    return _dependency


def require_any_permission(*permissions: str):
    def _dependency(
        employee: AuthenticatedEmployee | None = Depends(get_current_employee),
    ) -> AuthenticatedEmployee | None:
        if employee is None:
            if auth_config.settings.required:
                raise _unauthorized()
            return None

        if not employee.has_any_permission(permissions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action.",
            )

        return employee

    return _dependency
