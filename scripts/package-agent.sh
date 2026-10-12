#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 1 ]; then
  echo "usage: $0 <agent>" >&2
  exit 2
fi
agent_package_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$agent_package_root/dist"
exec uv run --no-project python -I "$agent_package_root/tools/agent_package_format.py" \
  pack "$agent_package_root" --agent "$1" --output "$agent_package_root/dist/$1.zip"
