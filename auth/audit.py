"""Structured audit logging for employee management actions."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("taskflow.audit")


def log_employee_event(
    action: str,
    *,
    actor,
    target=None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write one employee-management audit record.

    The event is emitted through the application logger so deployments can
    retain it with their normal log pipeline. Only identifiers already held by
    the application are included; no tokens, secrets or passwords are logged.
    """

    payload = {
        "action": action,
        "actor_id": getattr(actor, "id", None),
        "actor_email": getattr(actor, "email", None),
        "target_id": getattr(target, "id", None),
        "target_email": getattr(target, "email", None),
    }

    if details:
        payload["details"] = details

    logger.info("employee_audit %s", json.dumps(payload, default=str))
