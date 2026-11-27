#!/bin/sh
set -eu

# Named volumes are created as root. Prepare the model cache, then run the
# application as the unprivileged "app" user.
mkdir -p /home/app/.cache
chown -R app:app /home/app/.cache

# Bootstrap MCP credential file may be bind-mounted read-only (0600 root).
# Copy it to an app-readable path without printing or logging its contents.
if [ -n "${MCP_BOOTSTRAP_CREDENTIAL_FILE:-}" ] && [ -f "${MCP_BOOTSTRAP_CREDENTIAL_FILE}" ]; then
    MCP_BOOTSTRAP_RUNTIME_FILE="/run/fnw-mcp-bootstrap-credential"
    cp "${MCP_BOOTSTRAP_CREDENTIAL_FILE}" "${MCP_BOOTSTRAP_RUNTIME_FILE}"
    chown app:app "${MCP_BOOTSTRAP_RUNTIME_FILE}"
    chmod 0400 "${MCP_BOOTSTRAP_RUNTIME_FILE}"
    export MCP_BOOTSTRAP_CREDENTIAL_FILE="${MCP_BOOTSTRAP_RUNTIME_FILE}"
fi

case "${RUN_MIGRATIONS:-0}" in
  1|true|TRUE|yes|YES|on|ON)
    echo "RUN_MIGRATIONS enabled; running alembic upgrade head."
    gosu app alembic upgrade head
    ;;
  *)
    echo "RUN_MIGRATIONS disabled; skipping Alembic migrations."
    ;;
esac

exec gosu app "$@"
