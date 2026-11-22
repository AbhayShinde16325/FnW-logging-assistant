"""HTTP endpoints for Google sign-in and token lifecycle management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

import auth.config as auth_config
from auth.dependencies import get_current_employee
from auth.google import (
    GoogleAuthError,
    authorization_url,
    exchange_code,
    verify_id_token,
)
from auth.security import (
    constant_time_equal,
    create_access_token,
    create_state_token,
    decode_state_token,
    pkce_pair,
    random_token,
    safe_return_to,
)
from auth.service import (
    AuthenticationError,
    AuthenticatedEmployee,
    ReuseDetected,
    RotatedTokenError,
    create_session,
    employee_for_session,
    find_or_link_google_employee,
    revoke_employee_sessions,
    revoke_refresh_session,
    rotate_refresh_session,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _cookie_options(settings, *, httponly: bool) -> dict:
    options = {
        "httponly": httponly,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_same_site,
        "path": "/",
    }

    if settings.cookie_domain:
        options["domain"] = settings.cookie_domain

    return options


def _set_session_cookies(
    response: Response,
    *,
    refresh_token: str,
    csrf_token: str,
    settings,
) -> None:
    response.set_cookie(
        settings.refresh_cookie_name,
        refresh_token,
        max_age=settings.refresh_token_days * 24 * 60 * 60,
        **_cookie_options(settings, httponly=True),
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        max_age=settings.refresh_token_days * 24 * 60 * 60,
        **_cookie_options(settings, httponly=False),
    )


def _clear_session_cookies(response: Response, settings) -> None:
    for name in (settings.refresh_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(
            name,
            path="/",
            domain=settings.cookie_domain,
            secure=settings.cookie_secure,
            samesite=settings.cookie_same_site,
        )


def _csrf_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="The request could not be verified.",
    )


def _check_csrf(request: Request, settings) -> None:
    cookie_value = request.cookies.get(settings.csrf_cookie_name)
    header_value = request.headers.get("X-CSRF-Token")

    if not constant_time_equal(cookie_value, header_value):
        raise _csrf_error()


def _redirect_error(message: str, settings) -> RedirectResponse:
    separator = "&" if "?" in settings.post_login_redirect else "?"
    return RedirectResponse(
        f"{settings.post_login_redirect}{separator}auth_error={message}",
        status_code=status.HTTP_302_FOUND,
    )


def _client_details(request: Request) -> tuple[str | None, str | None]:
    forwarded = request.headers.get("X-Forwarded-For", "")

    if forwarded:
        ip_address = forwarded.split(",", 1)[0].strip()
    elif request.client:
        ip_address = request.client.host
    else:
        ip_address = None

    return request.headers.get("User-Agent"), ip_address


@router.get("/google/login")
def google_login(
    request: Request,
    return_to: str | None = None,
    format: str | None = None,
):
    settings = auth_config.settings

    if not settings.google_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google sign-in is not configured.",
        )

    verifier, challenge = pkce_pair()
    state = random_token(32)
    nonce = random_token(32)
    target = safe_return_to(
        return_to,
        settings.post_login_redirect,
        settings.frontend_origins,
    )
    state_token = create_state_token(
        state=state,
        nonce=nonce,
        code_verifier=verifier,
        return_to=target,
        settings=settings,
    )
    auth_url = authorization_url(
        state=state,
        nonce=nonce,
        code_challenge=challenge,
        settings=settings,
    )

    if format == "json":
        response = JSONResponse({"authorization_url": auth_url})
    else:
        response = RedirectResponse(auth_url, status_code=status.HTTP_302_FOUND)

    response.set_cookie(
        settings.oauth_state_cookie_name,
        state_token,
        max_age=600,
        **_cookie_options(settings, httponly=True),
    )

    return response


@router.get("/google/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None):
    settings = auth_config.settings
    state_cookie = request.cookies.get(settings.oauth_state_cookie_name)

    if request.query_params.get("error"):
        response = _redirect_error("google_cancelled", settings)
        response.delete_cookie(
            settings.oauth_state_cookie_name,
            path="/",
            domain=settings.cookie_domain,
            secure=settings.cookie_secure,
            samesite=settings.cookie_same_site,
        )
        return response

    if not code or not state:
        response = JSONResponse(
            {"detail": "The Google sign-in response is incomplete."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
        response.delete_cookie(
            settings.oauth_state_cookie_name,
            path="/",
            domain=settings.cookie_domain,
            secure=settings.cookie_secure,
            samesite=settings.cookie_same_site,
        )
        return response

    if not state_cookie:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The Google sign-in session is missing or expired.",
        )

    try:
        state_payload = decode_state_token(state_cookie, settings)
    except Exception:
        response = JSONResponse(
            {"detail": "The Google sign-in state is invalid or expired."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
        response.delete_cookie(
            settings.oauth_state_cookie_name,
            path="/",
            domain=settings.cookie_domain,
            secure=settings.cookie_secure,
            samesite=settings.cookie_same_site,
        )
        return response

    if not constant_time_equal(state_payload.get("state"), state):
        response = JSONResponse(
            {"detail": "The Google sign-in state does not match."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
        response.delete_cookie(
            settings.oauth_state_cookie_name,
            path="/",
            domain=settings.cookie_domain,
            secure=settings.cookie_secure,
            samesite=settings.cookie_same_site,
        )
        return response

    try:
        token_response = exchange_code(
            code=code,
            code_verifier=state_payload["code_verifier"],
            settings=settings,
        )
        claims = verify_id_token(
            id_token=token_response["id_token"],
            nonce=state_payload["nonce"],
            settings=settings,
        )
    except GoogleAuthError:
        response = _redirect_error("google_verification_failed", settings)
        response.delete_cookie(
            settings.oauth_state_cookie_name,
            path="/",
            domain=settings.cookie_domain,
            secure=settings.cookie_secure,
            samesite=settings.cookie_same_site,
        )
        return response

    employee = find_or_link_google_employee(
        google_sub=str(claims["sub"]),
        email=str(claims["email"]),
        name=str(claims.get("name") or claims["email"].split("@", 1)[0]),
        auth_settings=settings,
    )

    if employee is None:
        response = _redirect_error("not_provisioned", settings)
        response.delete_cookie(
            settings.oauth_state_cookie_name,
            path="/",
            domain=settings.cookie_domain,
            secure=settings.cookie_secure,
            samesite=settings.cookie_same_site,
        )
        return response

    user_agent, ip_address = _client_details(request)
    refresh_token, session_row = create_session(
        employee.id,
        auth_settings=settings,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    csrf_token = random_token(32)

    response = RedirectResponse(
        safe_return_to(
            state_payload.get("return_to"),
            settings.post_login_redirect,
            settings.frontend_origins,
        ),
        status_code=status.HTTP_302_FOUND,
    )
    _set_session_cookies(
        response,
        refresh_token=refresh_token,
        csrf_token=csrf_token,
        settings=settings,
    )
    response.delete_cookie(
        settings.oauth_state_cookie_name,
        path="/",
        domain=settings.cookie_domain,
    )

    return response


@router.post("/refresh")
def refresh(request: Request):
    settings = auth_config.settings
    _check_csrf(request, settings)
    refresh_token = request.cookies.get(settings.refresh_cookie_name)

    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The session has expired.",
        )

    user_agent, ip_address = _client_details(request)

    try:
        new_token, session_row = rotate_refresh_session(
            refresh_token,
            auth_settings=settings,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        employee = employee_for_session(session_row.id, session_row.employee_id)

        if employee is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="The session is no longer active.",
            )

        access_token, expires_at = create_access_token(
            employee_id=employee.id,
            email=employee.email,
            role=employee.role,
            permissions=employee.permissions,
            session_id=session_row.id,
            settings=settings,
        )
        csrf_token = random_token(32)

        response = JSONResponse(
            {
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": settings.access_token_minutes * 60,
                "expires_at": f"{expires_at.isoformat()}Z",
                "csrf_token": csrf_token,
                "employee": employee.to_payload(),
            }
        )
        _set_session_cookies(
            response,
            refresh_token=new_token,
            csrf_token=csrf_token,
            settings=settings,
        )

        return response
    except ReuseDetected:
        response = JSONResponse(
            {"detail": "The session was revoked."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        _clear_session_cookies(response, settings)
        return response
    except RotatedTokenError as error:
        replacement_session_id = getattr(error, "replacement_session_id", None)
        replacement_employee_id = getattr(error, "replacement_employee_id", None)

        if replacement_session_id and replacement_employee_id:
            employee = employee_for_session(
                str(replacement_session_id),
                int(replacement_employee_id),
            )

            if employee is not None:
                # A benign duplicate refresh raced with a successful rotation:
                # issue a fresh access token for the still-active replacement
                # session without rotating again or changing cookies.
                access_token, expires_at = create_access_token(
                    employee_id=employee.id,
                    email=employee.email,
                    role=employee.role,
                    permissions=employee.permissions,
                    session_id=employee.session_id or str(replacement_session_id),
                    settings=settings,
                )
                csrf_token = request.cookies.get(settings.csrf_cookie_name) or ""

                return JSONResponse(
                    {
                        "access_token": access_token,
                        "token_type": "bearer",
                        "expires_in": settings.access_token_minutes * 60,
                        "expires_at": f"{expires_at.isoformat()}Z",
                        "csrf_token": csrf_token,
                        "employee": employee.to_payload(),
                    }
                )

        # If the replacement is no longer usable, treat it as an expired
        # session and clear cookies so the client signs in again.
        response = JSONResponse(
            {"detail": "The session has expired."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        _clear_session_cookies(response, settings)
        return response
    except AuthenticationError:
        response = JSONResponse(
            {"detail": "The session has expired."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        _clear_session_cookies(response, settings)
        return response


@router.post("/logout")
def logout(request: Request):
    settings = auth_config.settings
    _check_csrf(request, settings)
    revoke_refresh_session(request.cookies.get(settings.refresh_cookie_name))

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookies(response, settings)

    return response


@router.post("/logout-all")
def logout_all(
    employee: AuthenticatedEmployee | None = Depends(get_current_employee),
):
    if employee is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    revoke_employee_sessions(employee.id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookies(response, auth_config.settings)

    return response


@router.get("/me")
def me(employee: AuthenticatedEmployee | None = Depends(get_current_employee)):
    if employee is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )

    return {"employee": employee.to_payload()}
