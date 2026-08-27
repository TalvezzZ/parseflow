#!/bin/sh
set -eu

# Both processes run as the image's unprivileged parseflow user. Nginx runtime
# state and request buffers live on the container's /tmp tmpfs.
mkdir -p /tmp/nginx-client-body /tmp/nginx-proxy /tmp/nginx-fastcgi /tmp/nginx-uwsgi /tmp/nginx-scgi

cd /app
/app/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 &
api_pid=$!

nginx -g 'daemon off;' &
nginx_pid=$!

shutdown() {
    kill -TERM "$api_pid" "$nginx_pid" 2>/dev/null || true
    wait "$api_pid" 2>/dev/null || true
    wait "$nginx_pid" 2>/dev/null || true
}
trap shutdown INT TERM

while kill -0 "$api_pid" 2>/dev/null && kill -0 "$nginx_pid" 2>/dev/null; do
    sleep 1
done
shutdown
wait "$api_pid" 2>/dev/null || true
wait "$nginx_pid" 2>/dev/null || true
exit 1
