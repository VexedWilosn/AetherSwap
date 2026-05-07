#!/usr/bin/env bash
set -euo pipefail

mkdir -p /app/config /app/log

if [ "${ENABLE_NOVNC:-1}" = "1" ]; then
  rm -f "/tmp/.X${DISPLAY#:}-lock"

  Xvfb "${DISPLAY:-:99}" -screen 0 "${SCREEN_GEOMETRY:-1280x800x24}" -nolisten tcp >/tmp/xvfb.log 2>&1 &
  sleep 0.5

  fluxbox >/tmp/fluxbox.log 2>&1 &
  x11vnc -display "${DISPLAY:-:99}" -forever -shared -nopw -rfbport "${VNC_PORT:-5900}" -listen 0.0.0.0 >/tmp/x11vnc.log 2>&1 &
  websockify --web=/usr/share/novnc/ "${NOVNC_PORT:-6080}" "localhost:${VNC_PORT:-5900}" >/tmp/novnc.log 2>&1 &
fi

exec "$@"
