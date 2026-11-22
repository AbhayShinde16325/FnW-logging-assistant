"""Compatibility import for the Google sign-in endpoints.

The implementation is split into ``auth.google`` (OIDC helpers) and
``auth.routes`` (HTTP routes) so the security-sensitive pieces have focused
tests.
"""

from auth.google import (  # noqa: F401
    GoogleAuthError,
    authorization_url,
    exchange_code,
    verify_id_token,
)
from auth.routes import router  # noqa: F401
