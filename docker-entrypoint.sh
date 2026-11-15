#!/bin/sh
set -eu

# Named volumes are created as root. Prepare the model cache, then run the
# application as the unprivileged "app" user.
mkdir -p /home/app/.cache
chown -R app:app /home/app/.cache

exec gosu app "$@"
