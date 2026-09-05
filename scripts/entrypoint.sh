#!/bin/sh
set -eu
# Usage: entrypoint.sh [discover|status] [extra args...]
exec osu-pipeline "$@"
