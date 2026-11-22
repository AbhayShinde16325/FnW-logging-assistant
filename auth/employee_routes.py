"""Administrative employee management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from auth.dependencies import get_current_employee
from auth.employee_service import (
    EmployeeManagementError,
    activate_employee,
    create_employee,
    deactivate_employee,
    get_employee,
    list_employees,
    revoke_sessions_for_employee,
    update_employee,
)
from auth.mcp_credentials import (
    MCPCredentialError,
    list_mcp_credentials,
    revoke_mcp_credential,
)
from auth.permissions import EMPLOYEES_MANAGE
from auth.service import AuthenticatedEmployee


router = APIRouter(prefix="/employees", tags=["employees"])


def require_employee_management(
    employee: AuthenticatedEmployee | None = Depends(get_current_employee),
) -> AuthenticatedEmployee:
    """Require a real authenticated principal with ``employees.manage``.

    Management endpoints must never fall back to anonymous access, even when
    the legacy ``AUTH_REQUIRED=0`` mode is enabled for unrelated task tests.
    """

    if employee is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not employee.has_permission(EMPLOYEES_MANAGE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage employees.",
        )

    return employee


class EmployeeCreateRequest(BaseModel):
    name: str
    email: str
    role: str


class EmployeeUpdateRequest(BaseModel):
    name: str | None = None
    role: str | None = None


def _error(error: EmployeeManagementError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail=str(error),
    )


@router.get("")
def employees(
    search: str | None = Query(default=None),
    role: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    employee: AuthenticatedEmployee = Depends(require_employee_management),
):
    try:
        return list_employees(search=search, role=role, status=status_filter)
    except EmployeeManagementError as error:
        raise _error(error)


@router.get("/{employee_id}")
def employee_detail(
    employee_id: int,
    employee: AuthenticatedEmployee = Depends(require_employee_management),
):
    try:
        return {"employee": get_employee(employee_id).to_payload()}
    except EmployeeManagementError as error:
        raise _error(error)


@router.post("", status_code=status.HTTP_201_CREATED)
def create(
    request: EmployeeCreateRequest,
    employee: AuthenticatedEmployee = Depends(require_employee_management),
):
    try:
        created = create_employee(
            actor=employee,
            name=request.name,
            email=request.email,
            role=request.role,
        )
    except EmployeeManagementError as error:
        raise _error(error)

    return {"employee": created.to_payload()}


@router.patch("/{employee_id}")
def update(
    employee_id: int,
    request: EmployeeUpdateRequest,
    employee: AuthenticatedEmployee = Depends(require_employee_management),
):
    if request.name is None and request.role is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide a name or a role to update.",
        )

    try:
        updated = update_employee(
            actor=employee,
            employee_id=employee_id,
            name=request.name,
            role=request.role,
        )
    except EmployeeManagementError as error:
        raise _error(error)

    return {"employee": updated.to_payload()}


@router.post("/{employee_id}/deactivate")
def deactivate(
    employee_id: int,
    employee: AuthenticatedEmployee = Depends(require_employee_management),
):
    try:
        return deactivate_employee(
            actor=employee,
            employee_id=employee_id,
        ).to_payload()
    except EmployeeManagementError as error:
        raise _error(error)


@router.post("/{employee_id}/activate")
def activate(
    employee_id: int,
    employee: AuthenticatedEmployee = Depends(require_employee_management),
):
    try:
        return activate_employee(
            actor=employee,
            employee_id=employee_id,
        ).to_payload()
    except EmployeeManagementError as error:
        raise _error(error)


@router.post("/{employee_id}/revoke-sessions")
def revoke_sessions(
    employee_id: int,
    employee: AuthenticatedEmployee = Depends(require_employee_management),
):
    try:
        return revoke_sessions_for_employee(
            actor=employee,
            employee_id=employee_id,
        ).to_payload()
    except EmployeeManagementError as error:
        raise _error(error)


@router.get("/{employee_id}/mcp-credentials")
def list_employee_mcp_credentials(
    employee_id: int,
    employee: AuthenticatedEmployee = Depends(require_employee_management),
):
    try:
        get_employee(employee_id)
        records = list_mcp_credentials(employee_id=employee_id)
    except EmployeeManagementError as error:
        raise _error(error)

    return {"credentials": [record.to_payload() for record in records]}


@router.post("/{employee_id}/mcp-credentials/{credential_id}/revoke")
def revoke_employee_mcp_credential(
    employee_id: int,
    credential_id: str,
    employee: AuthenticatedEmployee = Depends(require_employee_management),
):
    try:
        get_employee(employee_id)
        record = revoke_mcp_credential(
            credential_id=credential_id,
            actor_employee_id=employee.id,
            target_employee_id=employee_id,
        )
    except EmployeeManagementError as error:
        raise _error(error)
    except MCPCredentialError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error))

    return {"credential": record.to_payload()}
