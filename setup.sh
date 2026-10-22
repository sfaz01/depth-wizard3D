#!/usr/bin/env bash
# One-time setup for Linux / macOS.
set -euo pipefail
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
if command -v nvidia-smi >/dev/null 2>&1; then
  pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
else
  pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
fi
pip install -r requirements.txt
echo "Done. Optional free copilot: install Ollama, then 'ollama pull qwen2.5:3b'. Start with ./start.sh"
