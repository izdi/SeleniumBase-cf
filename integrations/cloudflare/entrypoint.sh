#!/bin/bash
set -e

echo "[cloudflare] Starting Xvfb on display ${DISPLAY:-:99} ..."
Xvfb "${DISPLAY:-:99}" -screen 0 1920x1080x24 -nolisten tcp &
XVFB_PID=$!

# Wait briefly for Xvfb to initialise
sleep 1
if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "[cloudflare] Xvfb failed to start" >&2
    exit 1
fi

echo "[cloudflare] Xvfb ready (pid=$XVFB_PID)"
echo "[cloudflare] Starting SeleniumBase worker API ..."
exec python3 /SeleniumBase/integrations/cloudflare/worker_api.py
