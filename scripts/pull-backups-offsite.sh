#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  pull-backups-offsite.sh <ssh-target> <remote-data-dir> <local-backup-dir>

Example:
  pull-backups-offsite.sh vps.example.com /home/dev/secretary-data ~/secretary-backups

The script runs on the offsite machine. It pulls encrypted archives from
<remote-data-dir>/backups/ and then atomically updates last_fetch on the VPS.
USAGE
}

if [[ $# -ne 3 ]]; then
  usage >&2
  exit 2
fi

ssh_target=$1
remote_data_dir=${2%/}
local_backup_dir=$3
remote_backups_dir="$remote_data_dir/backups"
read -r -a ssh_cmd <<< "${SECRETARY_SSH_COMMAND:-ssh}"
read -r -a scp_cmd <<< "${SECRETARY_SCP_COMMAND:-scp}"
if [[ -n "${SECRETARY_SSH_COMMAND:-}" && -z "${RSYNC_RSH:-}" ]]; then
  export RSYNC_RSH="$SECRETARY_SSH_COMMAND"
fi

mkdir -p "$local_backup_dir"

if command -v rsync >/dev/null 2>&1; then
  rsync -av --include='*.tar.age' --exclude='*' \
    "$ssh_target:$remote_backups_dir/" "$local_backup_dir/"
else
  "${ssh_cmd[@]}" "$ssh_target" "find '$remote_backups_dir' -maxdepth 1 -type f -name '*.tar.age' -print" |
    while IFS= read -r remote_archive; do
      "${scp_cmd[@]}" "$ssh_target:$remote_archive" "$local_backup_dir/"
    done
fi

fetched_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
"${ssh_cmd[@]}" "$ssh_target" \
  "set -euo pipefail; mkdir -p '$remote_backups_dir'; tmp=\$(mktemp '$remote_backups_dir/.last_fetch.XXXXXX'); printf '%s\n' '$fetched_at' > \"\$tmp\"; mv \"\$tmp\" '$remote_backups_dir/last_fetch'"
