#!/usr/bin/env bash
# One-shot transition of this installation from secretary to ummanu (docs/RENAME.md §T3).
#
# Launched by the PO, detached from its own session, which step 2 kills:
#   XDG_RUNTIME_DIR=/run/user/$(id -u) systemd-run --user --unit ummanu-transition --collect \
#     /bin/bash -c "$HOME/secretary/scripts/transition-from-secretary.sh > $HOME/transition.log 2>&1"
# Read the plan first: $HOME/secretary/.venv/bin/secretary transition from-secretary --plan \
#   --instance $HOME/secretary-instance
#
# Arguments go to the Python half (`--sprint sprint:N`, `--allow-extra-merge SHA`). A first argument
# `--rollback` undoes what the journal (~/ummanu-transition.json) says was done.
#
# Steps 1-4 run from the pre-rename tree, wherever step 4 has left it. Step 5 is this script's,
# because it builds the venv the rest runs from: fast-forward the checkout into the rename, point
# origin at the new repository, keep the old venv for rollback and build a new one. Then it execs
# the renamed tree's CLI, which verifies step 5 and resumes at step 6. Every part is idempotent, so
# a rerun of this script resumes where the last one stopped.
#
# Class T (docs/RENAME.md §T5): this file keeps both names on purpose.
set -euo pipefail

OLD_PACKAGE=secretary
NEW_PACKAGE=ummanu
NEW_REMOTE=https://github.com/vladmesh/ummanu.git

main() {
  local home="${TRANSITION_HOME:-$HOME}"
  local instance="${TRANSITION_INSTANCE:-$home/secretary-instance}"
  local old_root="$home/$OLD_PACKAGE" new_root="$home/$NEW_PACKAGE"
  local state="$home/ummanu-transition" journal="$home/ummanu-transition.json"
  local verb=apply
  if [ "${1:-}" = "--rollback" ]; then
    verb=rollback
    shift
  fi
  echo "transition-from-$OLD_PACKAGE: $verb at $(date -u +%FT%TZ), home $home, instance $instance"

  if [ "$verb" = rollback ]; then
    if [ -x "$new_root/.venv/bin/$NEW_PACKAGE" ] && [ -d "$new_root/src/$NEW_PACKAGE/transition" ]; then
      exec "$new_root/.venv/bin/$NEW_PACKAGE" transition "from-$OLD_PACKAGE" --home "$home" \
        --instance "$instance" --rollback "$@"
    fi
    run_old "$home" "$instance" "$old_root" "$new_root" --rollback "$@"
    return
  fi

  if ! step_done "$journal" move; then
    run_old "$home" "$instance" "$old_root" "$new_root" --apply --through move "$@"
    if ! step_done "$journal" move; then
      echo "steps 1-4 did not finish; see the output above" >&2
      exit 1
    fi
  fi
  if ! step_done "$journal" checkout; then
    bootstrap_checkout "$new_root" "$state" "$(journal_fact "$journal" target_sha)"
  fi
  exec "$new_root/.venv/bin/$NEW_PACKAGE" transition "from-$OLD_PACKAGE" --home "$home" \
    --instance "$instance" --apply "$@"
}

# The pre-rename tree: the old checkout path until step 4 moves it, the new path until step 5
# fast-forwards it. Its venv still runs (a moved venv keeps its interpreter); the editable install
# names the old path, so the tree's own src goes on PYTHONPATH.
run_old() {
  local home="$1" instance="$2" old_root="$3" new_root="$4"
  shift 4
  local root
  for root in "$old_root" "$new_root"; do
    if [ -f "$root/src/$OLD_PACKAGE/transition/__init__.py" ] && [ -x "$root/.venv/bin/python3" ]; then
      PYTHONPATH="$root/src" "$root/.venv/bin/python3" -P -m "$OLD_PACKAGE" transition "from-$OLD_PACKAGE" \
        --home "$home" --instance "$instance" "$@"
      return
    fi
  done
  echo "no pre-rename tree with its venv at $old_root or $new_root" >&2
  exit 1
}

journal_fact() {
  python3 - "$1" "$2" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("facts", {}).get(sys.argv[2]) or "")
PY
}

step_done() {
  python3 - "$1" "$2" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        steps = json.load(handle).get("steps", {})
except FileNotFoundError:
    sys.exit(1)
sys.exit(0 if steps.get(sys.argv[2], {}).get("status") == "done" else 1)
PY
}

# Step 5 (docs/RENAME.md §T3.5). The checkout moves to exactly the origin/main step 1 checked; a
# ref that moved since is a refusal, not something to activate.
bootstrap_checkout() {
  local root="$1" state="$2" target="$3"
  local branch head
  if [ -z "$target" ]; then
    echo "the journal names no target commit from step 1" >&2
    exit 1
  fi
  branch="$(git -C "$root" symbolic-ref --short HEAD)"
  if [ "$branch" != main ]; then
    echo "$root is on $branch, not main" >&2
    exit 1
  fi
  head="$(git -C "$root" rev-parse HEAD)"
  if [ "$head" != "$target" ]; then
    git -C "$root" fetch --quiet origin
    if [ "$(git -C "$root" rev-parse origin/main)" != "$target" ]; then
      echo "origin/main moved since step 1 checked $target; refusing to activate anything else" >&2
      exit 1
    fi
    git -C "$root" merge --ff-only --quiet "$target"
  fi
  git -C "$root" remote set-url origin "$NEW_REMOTE"
  # The whole old package goes, ignored __pycache__ leftovers included: an empty directory there
  # would still import as a namespace package.
  rm -rf "$root/src/$OLD_PACKAGE" "$root/src/$OLD_PACKAGE.egg-info"
  if [ ! -x "$root/.venv/bin/$NEW_PACKAGE" ]; then
    mkdir -p "$state"
    # The old venv's scripts carry the old path in their shebangs: kept whole for rollback.
    if [ -d "$root/.venv" ] && [ ! -e "$state/old-venv" ]; then
      mv "$root/.venv" "$state/old-venv"
    fi
    local python extras
    python="$(sed -n 's/^executable = //p' "$state/old-venv/pyvenv.cfg" 2>/dev/null || true)"
    if [ ! -x "${python:-}" ]; then
      python=python3
    fi
    extras="$(tr -d '[:space:]' < "$state/venv-extras" 2>/dev/null || true)"
    "$python" -m venv --clear "$root/.venv"
    "$root/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -e "$root[${extras:-dev}]"
  fi
  if ! env -u PYTHONPATH "$root/.venv/bin/python" -P -c "
try:
    import $OLD_PACKAGE
except ModuleNotFoundError:
    raise SystemExit(0)
raise SystemExit(1)"; then
    echo "the new venv still imports $OLD_PACKAGE; refusing to continue" >&2
    exit 1
  fi
  echo "step 5: $root at $(git -C "$root" rev-parse HEAD), venv $root/.venv, import $OLD_PACKAGE refused"
}

main "$@"
