#!/bin/sh
set -eu
# Usage: entrypoint.sh [discover|render|status] [extra args...]
# Headless danser needs an X server: start Xvfb on :99 unless one's already up.
export DISPLAY="${DISPLAY:-:99}"
if [ ! -S /tmp/.X11-unix/X99 ]; then
  rm -f /tmp/.X99-lock
  Xvfb :99 -screen 0 1280x720x24 >/tmp/xvfb.log 2>&1 &
fi
exec osu-pipeline "$@"
