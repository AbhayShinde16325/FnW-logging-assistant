FROM debian:bookworm-slim

ARG TUNNEL_CLIENT_BIN=tunnel/bin/tunnel-client

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system tunnel \
    && useradd --system --gid tunnel --home-dir /home/tunnel --create-home tunnel \
    && mkdir -p /etc/tunnel /run/tunnel /tmp \
    && chmod 1777 /tmp

COPY ${TUNNEL_CLIENT_BIN} /usr/local/bin/tunnel-client
RUN chmod 0755 /usr/local/bin/tunnel-client

# Default profile targets the Docker backend service; can be overridden by mount.
COPY tunnel/config/fnw-chatgpt.compose.yaml /etc/tunnel/fnw-chatgpt.yaml
COPY tunnel/entrypoint.sh /usr/local/bin/tunnel-entrypoint.sh
RUN chmod 0644 /etc/tunnel/fnw-chatgpt.yaml \
    && chmod 0755 /usr/local/bin/tunnel-entrypoint.sh

# Run as root only for startup checks/file copy, then drop to tunnel via gosu.
WORKDIR /etc/tunnel

# CONTROL_PLANE_API_KEY and FnW MCP authorization file are runtime-provided.
# Never bake secrets into this image or git-tracked config.
ENTRYPOINT ["/usr/local/bin/tunnel-entrypoint.sh"]
