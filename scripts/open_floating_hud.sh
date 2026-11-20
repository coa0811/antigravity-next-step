#!/bin/sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/run_python.sh" "$SCRIPT_DIR/next_step_floating.py" "$@"
