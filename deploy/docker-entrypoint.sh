#!/bin/sh
set -eu

# Substitute the only application-controlled Nginx value. All native Nginx
# variables ($host, $proxy_add_x_forwarded_for, and so on) stay untouched.
envs="${API_KEY:-}"
export API_KEY="$envs"
envsubst '${API_KEY}' < /etc/parseflow/nginx.conf.template > /etc/nginx/conf.d/parseflow.conf

# Nginx needs root to bind port 80. The FastAPI process itself runs unprivileged.
su -s /bin/sh -c 'cd /app && exec uv run --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1' parseflow &
api_pid=$!

nginx -g 'daemon off;' &
nginx_pid=$!

shutdown() {
    kill -TERM "$api_pid" "$nginx_pid" 2>/dev/null || true
    wait "$api_pid" 2>/dev/null || true
    wait "$nginx_pid" 2>/dev/null || true
}
trap shutdown INT TERM

# If either process terminates, terminate the other and make the container stop.
while kill -0 "$api_pid" 2>/dev/null && kill -0 "$nginx_pid" 2>/dev/null; do
    sleep 1
done
shutdown
wait "$api_pid" 2>/dev/null || true
wait "$nginx_pid" 2>/dev/null || true
exit 1
