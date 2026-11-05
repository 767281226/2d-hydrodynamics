#!/usr/bin/env bash
# Run the case pipeline:  ./run_case.sh case.yaml [--check] [--stage geometry|mesh|terrain|sections|config|run] [--resume] [--id RUN_ID]
set -euo pipefail
PREFIX="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONUTF8=1 GDAL_PAM_ENABLED=NO
exec "$PREFIX/venv/bin/python" -X utf8 -m hydro_pipeline.case_pipeline "$@"
