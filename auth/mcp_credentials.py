"""Dedicated, revocable MCP client credentials.

Credentials authenticate MCP requests only. The plaintext is returned once at
creation time and never stored; only a SHA-256 hash is persisted. Validation
binds the credential to an active employee and resolves the same RBAC/task
actor context used by existing MCP tools.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from auth.security import utcnow
from database.connection import SessionLocal
from database.models import Employee, MCPClientCredential, Role

logger = logging.getLogger("taskflow.audit")

MCP_CREDENTIAL_PREFIX = "fnw_mcp_"
# 32 random bytes => 256 bits of entropy.
MCP_CREDENTIAL_RANDOM_BYTES = 32
MCP_CREDENTIAL_PREFIX_LENGTH = 20
MCP_CREDENTIAL_MAX_NAME_LENGTH = 255
BOOTSTRAP_CREDENTIAL_NAME = "bootstrap-tunnel-credential"


class MCPCredentialError(Exception):
    status_code = 400


class MCPCredentialNotFound(MCPCredentialError):
    status_code = 404


class MCPCredentialForbidden(MCPCredentialError):
    status_code = 403


class MCPCredentialValidationError(MCPCredentialError):
    status_code = 401

    def __init__(self, message: str = "The MCP credential is invalid.", *, reason: str = "invalid"):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class MCPCredentialRecord:
    id: str
    employee_id: int
    name: str
    token_prefix: str
    created_at: datetime | None
    created_by_employee_id: int | None
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= utcnow())

    @property
    def is_active(self) -> bool:
        return not self.is_revoked and not self.is_expired

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "employee_id": self.employee_id,
            "name": self.name,
            "token_prefix": self.token_prefix,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by_employee_id": self.created_by_employee_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }


def _log_event(action: str, *, actor_id: int | None, credential: MCPCredentialRecord | None = None, details: dict | None = None) -> None:
    payload = {
        "action": action,
        "actor_id": actor_id,
        "credential_id": getattr(credential, "id", None),
        "employee_id": getattr(credential, "employee_id", None),
    }
    if details:
        payload["details"] = details
    logger.info("mcp_credential_audit %s", json.dumps(payload, default=str))


def _record(row: MCPClientCredential) -> MCPCredentialRecord:
    return MCPCredentialRecord(
        id=str(row.id),
        employee_id=int(row.employee_id),
        name=row.name,
        token_prefix=row.token_prefix,
        created_at=row.created_at,
        created_by_employee_id=(
            int(row.created_by_employee_id)
            if row.created_by_employee_id is not None
            else None
        ),
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
    )


def normalize_name(value: str | None) -> str:
    name = (value or "").strip()

    if not name:
        raise MCPCredentialError("A credential name is required.")

    if len(name) > MCP_CREDENTIAL_MAX_NAME_LENGTH:
        raise MCPCredentialError("The credential name is too long.")

    return name


def generate_credential_secret() -> str:
    # token_urlsafe consumes random bytes; 32 bytes provides >= 32 bytes entropy.
    return MCP_CREDENTIAL_PREFIX + secrets.token_urlsafe(MCP_CREDENTIAL_RANDOM_BYTES)


def hash_credential(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _is_mcp_credential_token(token: str | None) -> bool:
    return bool(token and token.startswith(MCP_CREDENTIAL_PREFIX))


def _prefix(token: str) -> str:
    return token[:MCP_CREDENTIAL_PREFIX_LENGTH]


def _employee_query(session):
    return session.query(Employee).options(
        joinedload(Employee.role).selectinload(Role.permissions)
    )


def create_mcp_credential(
    *,
    employee_id: int,
    name: str,
    created_by_employee_id: int | None = None,
    expires_at: datetime | None = None,
) -> tuple[str, MCPCredentialRecord]:
    normalized_name = normalize_name(name)

    if expires_at is not None and expires_at <= utcnow():
        raise MCPCredentialError("The credential expiry must be in the future.")

    plaintext = generate_credential_secret()
    now = utcnow()
    credential_id = str(uuid.uuid4())

    row = MCPClientCredential(
        id=credential_id,
        employee_id=int(employee_id),
        name=normalized_name,
        token_prefix=_prefix(plaintext),
        token_hash=hash_credential(plaintext),
        created_at=now,
        created_by_employee_id=(
            int(created_by_employee_id)
            if created_by_employee_id is not None
            else int(employee_id)
        ),
        expires_at=expires_at,
        revoked_at=None,
        last_used_at=None,
    )

    session = SessionLocal()

    try:
        employee = session.query(Employee).filter(Employee.id == int(employee_id)).first()

        if employee is None:
            raise MCPCredentialNotFound("The employee was not found.")

        if not employee.is_active:
            raise MCPCredentialForbidden("The employee account is inactive.")

        session.add(row)
        session.commit()
        session.refresh(row)
        record = _record(row)
    except Exception:
        session.rollback()
        raise
    else:
        _log_event("mcp_credential.created", actor_id=row.created_by_employee_id, credential=record)
        return plaintext, record
    finally:
        session.close()


def list_mcp_credentials(*, employee_id: int) -> list[MCPCredentialRecord]:
    session = SessionLocal()

    try:
        rows = (
            session.query(MCPClientCredential)
            .filter(MCPClientCredential.employee_id == int(employee_id))
            .order_by(MCPClientCredential.created_at.desc(), MCPClientCredential.id.desc())
            .all()
        )
        return [_record(row) for row in rows]
    finally:
        session.close()


def _load_credential(session, credential_id: str) -> MCPClientCredential:
    row = (
        session.query(MCPClientCredential)
        .filter(MCPClientCredential.id == str(credential_id))
        .first()
    )

    if row is None:
        raise MCPCredentialNotFound("The MCP credential was not found.")

    return row


def revoke_mcp_credential(
    *,
    credential_id: str,
    actor_employee_id: int,
    target_employee_id: int | None = None,
) -> MCPCredentialRecord:
    session = SessionLocal()

    try:
        row = _load_credential(session, credential_id)

        if target_employee_id is not None and int(row.employee_id) != int(target_employee_id):
            raise MCPCredentialNotFound("The MCP credential was not found.")

        if target_employee_id is None and int(row.employee_id) != int(actor_employee_id):
            raise MCPCredentialForbidden("You do not have access to this credential.")

        if row.revoked_at is None:
            row.revoked_at = utcnow()

        session.commit()
        session.refresh(row)
        record = _record(row)
    except Exception:
        session.rollback()
        raise
    else:
        _log_event("mcp_credential.revoked", actor_id=int(actor_employee_id), credential=record)
        return record
    finally:
        session.close()


def revoke_mcp_credentials_for_employee(employee_id: int) -> int:
    session = SessionLocal()

    try:
        count = (
            session.query(MCPClientCredential)
            .filter(
                MCPClientCredential.employee_id == int(employee_id),
                MCPClientCredential.revoked_at.is_(None),
            )
            .update({MCPClientCredential.revoked_at: utcnow()}, synchronize_session=False)
        )
        session.commit()
        return int(count or 0)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _validation_failure(reason: str, message: str) -> MCPCredentialValidationError:
    # Never log token, hash, prefix, or authorization header values.
    logger.warning(
        "mcp_credential_validation_failed %s",
        json.dumps({"reason": reason}, default=str),
    )
    return MCPCredentialValidationError(message, reason=reason)


def _bootstrap_config() -> tuple[str | None, int]:
    file_path = (os.getenv("MCP_BOOTSTRAP_CREDENTIAL_FILE") or "").strip()
    inline = (os.getenv("MCP_BOOTSTRAP_CREDENTIAL") or "").strip()
    source = file_path or inline or None
    employee_raw = (os.getenv("MCP_BOOTSTRAP_EMPLOYEE_ID") or "1").strip()
    try:
        employee_id = int(employee_raw)
    except (TypeError, ValueError) as error:
        raise MCPCredentialError(
            "MCP_BOOTSTRAP_EMPLOYEE_ID must be an integer."
        ) from error
    if employee_id <= 0:
        raise MCPCredentialError("MCP_BOOTSTRAP_EMPLOYEE_ID must be positive.")
    return source, employee_id


def _read_bootstrap_token() -> str | None:
    file_path = (os.getenv("MCP_BOOTSTRAP_CREDENTIAL_FILE") or "").strip()
    if file_path:
        try:
            raw = open(file_path, "r", encoding="utf-8").read()
        except FileNotFoundError as error:
            raise MCPCredentialError(
                "MCP bootstrap credential file was not found."
            ) from error
        except OSError as error:
            raise MCPCredentialError(
                "MCP bootstrap credential file could not be read."
            ) from error
    else:
        raw = os.getenv("MCP_BOOTSTRAP_CREDENTIAL") or ""

    token = raw.strip()
    if not token:
        return None
    # Allow "Bearer <token>" file/value format used by tunnel-client.
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        return None
    if not _is_mcp_credential_token(token):
        raise MCPCredentialError(
            "MCP bootstrap credential must use the fnw_mcp_ prefix."
        )
    return token


def _bootstrap_existing_decision(
    row: MCPClientCredential, *, expected_employee_id: int, now: datetime
) -> MCPCredentialRecord:
    if int(row.employee_id) != int(expected_employee_id):
        logger.error(
            "mcp_credential_bootstrap_rejected %s",
            json.dumps(
                {
                    "reason": "employee_mapping_mismatch",
                    "expected_employee_id": int(expected_employee_id),
                    "actual_employee_id": int(row.employee_id),
                    "credential_id": str(row.id),
                },
                default=str,
            ),
        )
        raise MCPCredentialError(
            "MCP bootstrap credential is mapped to a different employee."
        )

    if row.revoked_at is not None:
        logger.warning(
            "mcp_credential_bootstrap_ignored %s",
            json.dumps(
                {
                    "reason": "credential_revoked",
                    "credential_id": str(row.id),
                    "employee_id": int(row.employee_id),
                },
                default=str,
            ),
        )
        return _record(row)

    if row.expires_at is not None and row.expires_at <= now:
        logger.warning(
            "mcp_credential_bootstrap_ignored %s",
            json.dumps(
                {
                    "reason": "credential_expired",
                    "credential_id": str(row.id),
                    "employee_id": int(row.employee_id),
                },
                default=str,
            ),
        )
        return _record(row)

    logger.info(
        "mcp_credential_bootstrap_ok %s",
        json.dumps(
            {
                "reason": "already_registered",
                "credential_id": str(row.id),
                "employee_id": int(row.employee_id),
            },
            default=str,
        ),
    )
    return _record(row)


def reconcile_bootstrap_mcp_credential(
    *, session_factory=SessionLocal
) -> MCPCredentialRecord | None:
    """Idempotently register the mounted bootstrap MCP credential.

    Runs after migrations and before MCP serving. Never logs or returns the
    plaintext token. Never reactivates/recreates revoked or expired credentials.
    Never reassigns a credential hash to a different employee.
    """

    source, expected_employee_id = _bootstrap_config()
    if source is None:
        return None

    token = _read_bootstrap_token()
    if token is None:
        return None

    digest = hash_credential(token)
    prefix = _prefix(token)
    now = utcnow()
    session = session_factory()

    try:
        employee = (
            session.query(Employee)
            .filter(Employee.id == int(expected_employee_id))
            .first()
        )
        if employee is None:
            raise MCPCredentialError("MCP bootstrap employee was not found.")
        if not employee.is_active:
            raise MCPCredentialError("MCP bootstrap employee is inactive.")

        existing = (
            session.query(MCPClientCredential)
            .filter(MCPClientCredential.token_hash == digest)
            .first()
        )
        if existing is not None:
            return _bootstrap_existing_decision(
                existing, expected_employee_id=expected_employee_id, now=now
            )

        row = MCPClientCredential(
            id=str(uuid.uuid4()),
            employee_id=int(expected_employee_id),
            name=BOOTSTRAP_CREDENTIAL_NAME,
            token_prefix=prefix,
            token_hash=digest,
            created_at=now,
            created_by_employee_id=int(expected_employee_id),
            expires_at=None,
            revoked_at=None,
            last_used_at=None,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raced = (
                session.query(MCPClientCredential)
                .filter(MCPClientCredential.token_hash == digest)
                .first()
            )
            if raced is None:
                raise
            return _bootstrap_existing_decision(
                raced, expected_employee_id=expected_employee_id, now=utcnow()
            )

        session.refresh(row)
        record = _record(row)
        _log_event(
            "mcp_credential.bootstrap_registered",
            actor_id=int(expected_employee_id),
            credential=record,
        )
        logger.info(
            "mcp_credential_bootstrap_ok %s",
            json.dumps(
                {
                    "reason": "registered",
                    "credential_id": record.id,
                    "employee_id": record.employee_id,
                },
                default=str,
            ),
        )
        return record
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def resolve_employee_for_mcp_credential(token: str):
    """Validate an MCP credential token and return its active employee.

    Returns ``(AuthenticatedEmployee, credential_id)`` or raises
    ``MCPCredentialValidationError``. Never raises with secret material.
    """

    if not _is_mcp_credential_token(token):
        raise MCPCredentialValidationError(
            "The MCP credential is invalid.",
            reason="missing_fnw_mcp_prefix",
        )

    digest = hash_credential(token)
    now = utcnow()
    session = SessionLocal()

    try:
        row = (
            session.query(MCPClientCredential)
            .options(
                joinedload(MCPClientCredential.employee)
                .joinedload(Employee.role)
                .selectinload(Role.permissions)
            )
            .filter(MCPClientCredential.token_hash == digest)
            .first()
        )

        if row is None:
            raise _validation_failure(
                "credential_not_found",
                "The MCP credential is invalid, expired, or revoked.",
            )

        # Constant-time comparison as defense in depth (DB lookup is primary).
        if not hmac.compare_digest(row.token_hash, digest):
            raise _validation_failure(
                "credential_hash_mismatch",
                "The MCP credential is invalid, expired, or revoked.",
            )

        if row.revoked_at is not None:
            raise _validation_failure(
                "credential_revoked",
                "The MCP credential is invalid, expired, or revoked.",
            )

        if row.expires_at is not None and row.expires_at <= now:
            raise _validation_failure(
                "credential_expired",
                "The MCP credential is invalid, expired, or revoked.",
            )

        employee = row.employee

        if employee is None:
            raise _validation_failure(
                "employee_missing",
                "The employee account is inactive.",
            )

        if not employee.is_active:
            raise _validation_failure(
                "employee_inactive",
                "The employee account is inactive.",
            )

        # Import locally to avoid circular imports at module load time.
        from auth.service import _identity

        identity = _identity(employee)
        row.last_used_at = now
        session.commit()

        _log_event(
            "mcp_credential.used",
            actor_id=identity.id,
            credential=_record(row),
        )

        return identity, str(row.id)
    except MCPCredentialValidationError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        logger.exception(
            "mcp_credential_validation_failed %s",
            json.dumps({"reason": "unexpected_error"}, default=str),
        )
        raise MCPCredentialValidationError(
            "The MCP credential could not be validated.",
            reason="unexpected_error",
        )
    finally:
        session.close()
