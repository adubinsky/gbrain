#!/usr/bin/env bash
# pg_dump the local gbrain database. Schedule with com.gbrain.postgres-backup.plist.
#
#   set -a && . ~/.gbrain/backup.env && set +a
#   scripts/local-postgres/backup-nightly.sh
#
# backup.env (mode 0600):
#   DATABASE_URL=postgresql://gbrain@127.0.0.1:5432/gbrain
#   BACKUP_DIR=/Volumes/your-nas/gbrain-backups
#   BACKUP_RETAIN_DAYS=14
#
# BACKUP_DIR may be a NAS mount or a Dropbox/iCloud folder. The password
# belongs in ~/.pgpass. This script does not print the URL.
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" || -z "${BACKUP_DIR:-}" ]]; then
  echo "Set DATABASE_URL and BACKUP_DIR in the environment." >&2
  exit 2
fi
if ! command -v pg_dump >/dev/null; then
  echo "pg_dump is required (postgresql@17 client tools)." >&2
  exit 2
fi

RETAIN="${BACKUP_RETAIN_DAYS:-14}"
mkdir -p "$BACKUP_DIR"
LOCKDIR="${BACKUP_DIR}/.backup.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "Another backup holds $LOCKDIR. Leaving it." >&2
  exit 0
fi
trap 'rmdir "$LOCKDIR"' EXIT

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${BACKUP_DIR}/gbrain-${STAMP}.dump"
umask 077
pg_dump "$DATABASE_URL" --format=custom --no-owner --no-privileges --file="$OUT"

find "$BACKUP_DIR" -maxdepth 1 -type f -name 'gbrain-*.dump' -mtime "+${RETAIN}" -delete
echo "Wrote $OUT" >&2
