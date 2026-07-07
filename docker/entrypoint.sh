#!/bin/sh
# ACBC GivEnergy Dashboard container entrypoint.
#
# The /data volume is often bind-mounted from a host directory owned by the host
# user, so its ownership is unpredictable. When we start as root we make the data
# dir writable by the unprivileged app user and then drop to it. If we are already
# running unprivileged (chown fails), we just run as-is.
set -e

DATA_DIR="${ACBC_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR" 2>/dev/null || true

if [ "$(id -u)" = "0" ] && chown -R acbc:acbc "$DATA_DIR" 2>/dev/null; then
    exec gosu acbc "$@"
fi

exec "$@"
