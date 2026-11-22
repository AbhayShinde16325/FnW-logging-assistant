const API = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000").replace(
  /\/$/,
  ""
);

export const API_BASE = API;
export const AUTH_ME_API = `${API}/auth/me`;
export const AUTH_REFRESH_API = `${API}/auth/refresh`;
export const AUTH_CSRF_API = `${API}/auth/csrf`;
export const AUTH_LOGIN_API = `${API}/auth/google/login`;
export const AUTH_LOGOUT_API = `${API}/auth/logout`;
export const AUTH_LOGOUT_ALL_API = `${API}/auth/logout-all`;

const CSRF_COOKIE_NAME = "taskflow_csrf";
let csrfToken = "";

export function readCookie(name) {
  if (typeof document === "undefined") return "";

  const prefix = `${name}=`;
  const match = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));

  return match ? decodeURIComponent(match.slice(prefix.length)) : "";
}

export function setCsrfToken(token) {
  if (typeof token === "string" && token) {
    csrfToken = token;
  }
}

export function clearCsrfToken() {
  csrfToken = "";
}

function currentCsrfToken() {
  return csrfToken || readCookie(CSRF_COOKIE_NAME);
}

const csrfHeaders = () => {
  const token = currentCsrfToken();
  return token ? { "X-CSRF-Token": token } : {};
};

export function beginGoogleLogin(returnTo = window.location.href) {
  window.location.assign(
    `${AUTH_LOGIN_API}?return_to=${encodeURIComponent(returnTo)}`
  );
}

const REFRESH_LOCK_NAME = "taskflow-refresh-session";

export async function bootstrapCsrfToken() {
  const response = await fetch(AUTH_CSRF_API, {
    method: "GET",
    credentials: "include",
  });
  const data = await response.json().catch(() => ({}));

  if (response.ok && data?.csrf_token) {
    setCsrfToken(data.csrf_token);
    return data.csrf_token;
  }

  throw new Error(data?.detail || "Unable to initialize the session.");
}

async function postRefreshRequest() {
  const doRefresh = () =>
    fetch(AUTH_REFRESH_API, {
      method: "POST",
      credentials: "include",
      headers: csrfHeaders(),
    });

  let response = await doRefresh();
  let data = await response.json().catch(() => ({}));

  // After a full-page OAuth redirect the CSRF cookie may exist but the
  // in-memory token and cross-origin cookie read are unavailable. Bootstrap a
  // fresh CSRF token once, then retry the refresh.
  if (
    response.status === 403 &&
    /could not be verified/i.test(String(data?.detail || ""))
  ) {
    try {
      await bootstrapCsrfToken();
      response = await doRefresh();
      data = await response.json().catch(() => ({}));
    } catch {
      return {
        ok: false,
        status: 403,
        data: { detail: "The request could not be verified." },
      };
    }
  }

  if (response.ok && data?.csrf_token) {
    setCsrfToken(data.csrf_token);
  }

  return {
    ok: response.ok,
    status: response.status,
    data,
  };
}

export async function refreshSessionRequest() {
  if (typeof navigator !== "undefined" && navigator.locks?.request) {
    return navigator.locks.request(REFRESH_LOCK_NAME, () =>
      postRefreshRequest()
    );
  }

  return postRefreshRequest();
}

export async function logoutRequest(accessToken, allSessions = false) {
  const url = allSessions ? AUTH_LOGOUT_ALL_API : AUTH_LOGOUT_API;

  await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: {
      ...csrfHeaders(),
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
  }).catch(() => {});
  clearCsrfToken();
}

export function authErrorMessage(code) {
  const messages = {
    google_cancelled: "Google sign-in was cancelled.",
    google_verification_failed: "Google could not verify your identity.",
    not_provisioned:
      "Your Google account is not provisioned. Ask an administrator to add your work email.",
  };

  return messages[code] || "Sign-in could not be completed.";
}
