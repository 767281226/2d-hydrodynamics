#!/usr/bin/env bash
# Serve one result bundle on 127.0.0.1:8788 (no browser on the server).
#   ./start_viewer.sh /path/to/<results root>/<run_id>
# From your PC:  ssh -L 8788:127.0.0.1:8788 user@server   then open the printed URL in a local browser.
set -euo pipefail
PREFIX="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ $# -ge 1 ] || { echo "usage: $0 <run directory or bundle directory>"; exit 2; }
exec "$PREFIX/venv/bin/python" -X utf8 -m hydro_pipeline.viewer_server --project-root "$PREFIX/app" --no-browser "$1"
