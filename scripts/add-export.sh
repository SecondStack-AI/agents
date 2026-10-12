#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 1 ]; then
  echo "usage: $0 <export.zip>" >&2
  exit 2
fi
agent_package_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec uv run --no-project python -I "$agent_package_root/scripts/add_agent_export.py" \
  "$1" --repo "$agent_package_root"
