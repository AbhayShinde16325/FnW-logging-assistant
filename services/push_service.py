"""Web Push delivery with VAPID.

The VAPID private key is read only from server-side environment configuration
or a server-side file. It is never returned by an API or included in browser
code. ``pywebpush`` is imported lazily so the rest of the application (and the
test suite) can run without a configured push provider.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class PushDeliveryError(Exception):
    """A push provider failure.

    ``permanent`` means the subscription endpoint itself is gone (404/410).
    ``auth_failure`` means the push service rejected VAPID authentication
    (401/403), which is a configuration problem rather than an expired device.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        permanent: bool = False,
        auth_failure: bool = False,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.permanent = permanent
        self.auth_failure = auth_failure
        self.retryable = retryable


class PushNotConfigured(PushDeliveryError):
    def __init__(self) -> None:
        super().__init__("Web Push VAPID configuration is missing.")


@dataclass(frozen=True)
class PushSettings:
    public_key: str
    private_key: str
    subject: str

    @property
    def configured(self) -> bool:
        return bool(self.public_key and self.private_key)


@dataclass(frozen=True)
class PushSendResult:
    status_code: int | None = None
    provider: str | None = None
    location: str | None = None


def _private_key_value() -> str:
    raw = os.getenv("VAPID_PRIVATE_KEY", "").strip()
    path = os.getenv("VAPID_PRIVATE_KEY_PATH", "").strip()

    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise PushNotConfigured() from error

    if not raw:
        return ""

    # Docker/.env values are single-line. Accept the common escaped newline
    # form without logging or returning the key.
    return raw.replace("\\n", "\n")


def load_push_settings() -> PushSettings:
    return PushSettings(
        public_key=os.getenv("VAPID_PUBLIC_KEY", "").strip(),
        private_key=_private_key_value(),
        subject=(
            os.getenv("VAPID_SUBJECT", "mailto:admin@example.com").strip()
            or "mailto:admin@example.com"
        ),
    )


def push_configured() -> bool:
    return load_push_settings().configured


def vapid_public_key() -> str:
    return load_push_settings().public_key


def _response_body(response) -> str:
    if response is None:
        return ""

    try:
        text = response.text or ""
    except Exception:
        text = ""

    text = " ".join(str(text).split())
    return text[:300]


def endpoint_origin(endpoint: str | None) -> str | None:
    """Return only scheme://host[:port] for safe logging."""

    if not endpoint:
        return None

    try:
        parts = urlsplit(str(endpoint))
    except Exception:
        return None

    if not parts.scheme or not parts.netloc:
        return None

    return f"{parts.scheme}://{parts.netloc}"


def send_web_push(subscription_info: dict, payload: dict) -> PushSendResult:
    """Deliver one JSON payload to one browser subscription.

    A successful return means the push service accepted the message for
    delivery (typically HTTP 201). It does not prove the browser received the
    event or displayed a notification.
    """

    settings = load_push_settings()

    if not settings.configured:
        raise PushNotConfigured()

    try:
        from pywebpush import WebPushException, webpush
    except ImportError as error:
        raise PushDeliveryError(
            "The pywebpush package is not installed.",
            retryable=False,
        ) from error

    endpoint = (subscription_info or {}).get("endpoint")
    origin = endpoint_origin(endpoint)

    try:
        response = webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=settings.private_key,
            vapid_claims={"sub": settings.subject},
            timeout=10,
        )
    except WebPushException as error:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        body = _response_body(response)
        permanent = status_code in {404, 410}
        auth_failure = status_code in {401, 403}
        retryable = not (permanent or auth_failure or status_code == 400)

        detail = body or str(error).strip() or "no response body"
        detail = " ".join(detail.split())[:300]

        logger.warning(
            "push_delivery_rejected provider_origin=%s status=%s permanent=%s auth_failure=%s retryable=%s detail=%s",
            origin,
            status_code,
            permanent,
            auth_failure,
            retryable,
            detail,
        )

        raise PushDeliveryError(
            f"Web Push delivery failed ({status_code or 'network error'}): {detail}",
            status_code=status_code,
            permanent=permanent,
            auth_failure=auth_failure,
            retryable=retryable,
        ) from error
    except Exception as error:
        detail = " ".join(str(error).split())[:300]
        logger.warning(
            "push_delivery_error provider_origin=%s type=%s detail=%s",
            origin,
            type(error).__name__,
            detail,
        )
        raise PushDeliveryError(
            f"Web Push delivery failed: {error}",
            retryable=True,
        ) from error

    status_code = getattr(response, "status_code", None)
    location = None

    try:
        headers = getattr(response, "headers", None) or {}
        location = headers.get("Location") or headers.get("location")
    except Exception:
        location = None

    logger.info(
        "push_service_accepted provider_origin=%s status=%s",
        origin,
        status_code,
    )

    return PushSendResult(
        status_code=status_code,
        provider=origin,
        location=location,
    )
