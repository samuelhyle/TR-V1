#!/bin/bash
# launchd does not inherit the login shell. Load private runtime settings, then
# let the Python coordinator own the full-run lock, timeouts and health reports.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 78
if [ -f config/local.env ]; then
  set -a
  # shellcheck disable=SC1091
  . config/local.env
  set +a
fi
export TORI_DATA_DIR="${TORI_DATA_DIR:-$PWD/data}"
export TORI_REPORTS_DIR="${TORI_REPORTS_DIR:-$PWD/reports}"
exec "${TORI_PYTHON:-python3}" -m toriresearch.operations
