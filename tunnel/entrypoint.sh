#!/bin/sh
set -eu

PROFILE_FILE="${TUNNEL_PROFILE_FILE:-/etc/tunnel/fnw-chatgpt.yaml}"
AUTH_FILE="${FNW_MCP_AUTHORIZATION_FILE:-/run/tunnel/fnw-mcp-authorization}"
AUTH_RUNTIME_FILE="/tmp/fnw-mcp-authorization"
RUNTIME_PROFILE="/tmp/fnw-chatgpt.runtime.yaml"

if [ -z "${CONTROL_PLANE_API_KEY:-}" ]; then
  echo "CONTROL_PLANE_API_KEY is required for mcp-tunnel; refusing to start an unauthenticated tunnel." >&2
  echo "Set CONTROL_PLANE_API_KEY in .env or runtime env, then run: docker compose up -d mcp-tunnel" >&2
  exit 64
fi

if [ ! -f "$PROFILE_FILE" ]; then
  echo "Tunnel profile not found: $PROFILE_FILE" >&2
  exit 66
fi

if [ ! -f "$AUTH_FILE" ]; then
  echo "FnW MCP authorization file not found: $AUTH_FILE" >&2
  echo "Provide a read-only file containing: Bearer fnw_mcp_..." >&2
  exit 66
fi

# The bind-mounted secret may be root-owned 0600 on the host. Copy it to a
# tunnel-readable path without printing or logging its contents.
rm -f "$AUTH_RUNTIME_FILE" "$RUNTIME_PROFILE"
cp "$AUTH_FILE" "$AUTH_RUNTIME_FILE"
chown tunnel:tunnel "$AUTH_RUNTIME_FILE"
chmod 0400 "$AUTH_RUNTIME_FILE"

# Ensure the profile points at the runtime-readable authorization file.
# The compose profile already uses /run/tunnel/fnw-mcp-authorization; if an
# operator overrides with a different mounted path, replace it safely.
if [ "$AUTH_FILE" != "$AUTH_RUNTIME_FILE" ]; then
  sed "s|$AUTH_FILE|$AUTH_RUNTIME_FILE|g" "$PROFILE_FILE" > "$RUNTIME_PROFILE"
  chown tunnel:tunnel "$RUNTIME_PROFILE"
  chmod 0400 "$RUNTIME_PROFILE"
  PROFILE_FILE="$RUNTIME_PROFILE"
fi

exec gosu tunnel tunnel-client run --profile-file "$PROFILE_FILE"
