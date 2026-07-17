#!/usr/bin/env bash
# Обёртка над orca environment для пэйринга с локальным браузером vladmesh.
# См. SKILL.md рядом. Симлинкается в ~/bin/remote-browser.
set -euo pipefail

ENV_NAME="vladmesh-local"

usage() {
  cat <<EOF
Usage: remote-browser <connect|status|disconnect>

  connect <pairing-code>   orca environment add + smoke-check (orca tab list)
  status                   orca environment show, "not connected" if absent
  disconnect               orca environment rm, ok if already absent
EOF
}

cmd="${1:-}"

case "$cmd" in
  connect)
    code="${2:-}"
    if [[ -z "$code" ]]; then
      echo "Usage: remote-browser connect '<orca://pair?code=...>'" >&2
      exit 1
    fi
    orca environment add --name "$ENV_NAME" --pairing-code "$code"
    echo "Smoke-check: orca tab list --environment $ENV_NAME"
    orca tab list --environment "$ENV_NAME"
    ;;
  status)
    orca environment show --environment "name:$ENV_NAME" 2>/dev/null || echo "not connected ($ENV_NAME)"
    ;;
  disconnect)
    orca environment rm --environment "name:$ENV_NAME" 2>/dev/null || true
    echo "disconnected (or already absent): $ENV_NAME"
    ;;
  *)
    usage
    exit 1
    ;;
esac
