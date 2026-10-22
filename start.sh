#!/usr/bin/env bash
set -euo pipefail
. .venv/bin/activate
if command -v nvidia-smi >/dev/null 2>&1; then export DW_MODEL_SIZE="${DW_MODEL_SIZE:-base}"; fi
exec python -m api.main
