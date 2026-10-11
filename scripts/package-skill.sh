#!/usr/bin/env bash
# Builds dist/<skill>.zip with SKILL.md at the archive root, ready for Chat's Skill upload.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <skill>" >&2
  exit 2
fi
root="$(cd "$(dirname "$0")/.." && pwd)"
skill="$1"
source_dir="$root/skills/$skill"
if [ ! -f "$source_dir/SKILL.md" ]; then
  echo "no skill at skills/$skill (SKILL.md missing)" >&2
  exit 1
fi
mkdir -p "$root/dist"
archive="$root/dist/$skill.zip"
rm -f "$archive"
(cd "$source_dir" && zip -qr "$archive" . -x '*__pycache__*' -x '*.pyc' -x '.*')
sha256sum "$archive"
