#!/bin/sh
set -eu
# Usage: entrypoint.sh [discover|render|status] [extra args...]
# Headless danser needs an X server: start Xvfb on :99 unless one's already up.
export DISPLAY="${DISPLAY:-:99}"
if [ ! -S /tmp/.X11-unix/X99 ]; then
  rm -f /tmp/.X99-lock
  Xvfb :99 -screen 0 1280x720x24 >/tmp/xvfb.log 2>&1 &
fi
# danser's internal DB lives next to its binary (ephemeral layer) because the
# binary is not under /usr/bin (XDG is ignored in that case). Point it at the
# persisted volume instead; re-linked every boot to self-heal if danser ever
# replaces the symlink with a real file. Dir must exist (it is bind-mounted).
if [ -d /data/db ]; then
  ln -sf /data/db/danser.db /opt/danser/danser.db
fi
exec osu-pipeline "$@"
