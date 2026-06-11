#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

# Playwright MCP installed locally (not via npx) so agent tasks start instantly.
if [ ! -x node_modules/.bin/playwright-mcp ]; then
  echo "installing Playwright MCP locally…"
  npm install --silent
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "!!  Created .env from .env.example — EDIT IT AND SET A REAL PASSWORD  !!"
fi

set -a
source .env
set +a

PORT="${PORT:-8080}"
if curl -s --max-time 2 -o /dev/null "localhost:${PORT}/login"; then
  echo "!!  agent-browser is already running on port ${PORT}."
  echo "!!  Stop it first: scripts/cleanup.sh"
  exit 1
fi

# --ws-per-message-deflate false: JPEG frames are already compressed, so deflate
# only burns CPU and adds latency. --no-access-log: drop per-request log overhead.
exec .venv/bin/uvicorn server.app:app --host 0.0.0.0 --port "${PORT}" \
  --ws-per-message-deflate false --no-access-log
