#!/usr/bin/env sh
set -eu

# Allow overriding gunicorn parameters via env
: "${PORT:=5000}"
: "${GUNICORN_WORKERS:=2}"
: "${GUNICORN_TIMEOUT:=300}"

# Build gunicorn bind
BIND="0.0.0.0:${PORT}"

echo "[entrypoint] Starting gunicorn on ${BIND} (workers=${GUNICORN_WORKERS}, timeout=${GUNICORN_TIMEOUT})"

exec "$@" -w "${GUNICORN_WORKERS}" -b "${BIND}" --timeout "${GUNICORN_TIMEOUT}" --access-logfile - claude_proxy:app

