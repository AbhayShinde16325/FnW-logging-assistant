"""Authenticated self-service MCP client credential endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth.dependencies import get_current_employee
from auth.mcp_credentials import (
    MCPCredentialError,
    create_mcp_credential,
    list_mcp_credentials,
    revoke_mcp_credential,
)
from auth.service import AuthenticatedEmployee

router = APIRouter(prefix="/auth/mcp-credentials", tags=["mcp-credentials"])


def require_authenticated_employee(
    employee: AuthenticatedEmployee | None = Depends(get_current_employee),
) -> AuthenticatedEmployee:
    if employee is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return employee


class CreateMCPCredentialRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    expires_at: datetime | None = None


def _error(error: MCPCredentialError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=str(error))


@router.get("")
def list_credentials(
    employee: AuthenticatedEmployee = Depends(require_authenticated_employee),
):
    records = list_mcp_credentials(employee_id=employee.id)
    return {"credentials": [record.to_payload() for record in records]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_credential(
    request: CreateMCPCredentialRequest,
    employee: AuthenticatedEmployee = Depends(require_authenticated_employee),
):
    try:
        plaintext, record = create_mcp_credential(
            employee_id=employee.id,
            name=request.name,
            created_by_employee_id=employee.id,
            expires_at=request.expires_at,
        )
    except MCPCredentialError as error:
        raise _error(error)

    payload = record.to_payload()
    payload["credential"] = plaintext
    return {"credential": payload}


@router.post("/{credential_id}/revoke")
def revoke_credential(
    credential_id: str,
    employee: AuthenticatedEmployee = Depends(require_authenticated_employee),
):
    try:
        record = revoke_mcp_credential(
            credential_id=credential_id,
            actor_employee_id=employee.id,
        )
    except MCPCredentialError as error:
        raise _error(error)

    return {"credential": record.to_payload()}
