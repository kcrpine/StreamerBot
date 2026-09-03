#!/bin/bash
set -euo pipefail

POT_PROVIDER_PORT=${POT_PROVIDER_PORT:-4416}
YOUTUBE_BRIDGE_PORT=${YOUTUBE_BRIDGE_PORT:-4417}
export POT_PROVIDER_URL=${POT_PROVIDER_URL:-http://127.0.0.1:${POT_PROVIDER_PORT}/get_pot}
export YOUTUBE_BRIDGE_HOST=${YOUTUBE_BRIDGE_HOST:-127.0.0.1}
export YOUTUBE_BRIDGE_PORT
export STREAMERBOT_BOTS_ROOT=${STREAMERBOT_BOTS_ROOT:-/bots}

POT_PID=""
BRIDGE_PID=""

cleanup() {
    [ -n "$BRIDGE_PID" ] && kill "$BRIDGE_PID" 2>/dev/null || true
    [ -n "$POT_PID" ] && kill "$POT_PID" 2>/dev/null || true
    wait "$BRIDGE_PID" "$POT_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

node /opt/bgutil-provider/server/build/main.js --port "$POT_PROVIDER_PORT" &
POT_PID=$!

node /home/streamer/StreamerBot/youtube_bridge/server.mjs &
BRIDGE_PID=$!

wait -n "$POT_PID" "$BRIDGE_PID"
