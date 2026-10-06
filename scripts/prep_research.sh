#!/usr/bin/env bash
# scripts/prep_research.sh
# Reusable helper to generate timestamped research context snapshot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

python3 "$REPO_ROOT/scripts/prep_research.py" "$@"
