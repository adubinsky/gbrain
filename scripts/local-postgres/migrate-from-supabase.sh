#!/usr/bin/env bash
# Dump a Supabase (or any) gbrain database and restore it locally.
#
#   set -a && . ~/.gbrain/migrate.env && set +a
#   scripts/local-postgres/migrate-from-supabase.sh
#
# migrate.env (mode 0600) sets SOURCE_DATABASE_URL and TARGET_DATABASE_URL.
# The source must be a direct connection or the session pooler (port 5432).
# Port 6543 is transaction mode and is the wrong source for pg_dump.
# Passwords stay in the environment or ~/.pgpass. This script does not print URLs.
set -euo pipefail

if [[ -z "${SOURCE_DATABASE_URL:-}" || -z "${TARGET_DATABASE_URL:-}" ]]; then
  echo "Set SOURCE_DATABASE_URL and TARGET_DATABASE_URL in the environment." >&2
  exit 2
fi

source_port="$(python3 - << 'PY'
import os
from urllib.parse import urlparse
p = urlparse(os.environ["SOURCE_DATABASE_URL"])
print(p.port or "")
PY
)"
if [[ "$source_port" == "6543" ]]; then
  echo "SOURCE_DATABASE_URL uses port 6543 (transaction pooler). Use the direct host or the session pooler on port 5432." >&2
  exit 2
fi

if ! command -v pg_dump >/dev/null || ! command -v pg_restore >/dev/null; then
  echo "pg_dump and pg_restore are required (postgresql@17 client tools)." >&2
  exit 2
fi

DUMP="${GBRAIN_DUMP_PATH:-$HOME/.gbrain/gbrain-migrate.dump}"
LOG="${DUMP}.log"
mkdir -p "$(dirname "$DUMP")"
umask 077

echo "Creating extensions on the target." >&2
psql "$TARGET_DATABASE_URL" -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS vector;' \
  -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm;' \
  -c 'CREATE EXTENSION IF NOT EXISTS pgcrypto;'

echo "Dumping the public schema to $DUMP" >&2
pg_dump "$SOURCE_DATABASE_URL" \
  --format=custom \
  --no-owner \
  --no-privileges \
  --schema=public \
  --file="$DUMP"

echo "Restoring. Hosted-role messages are leftovers from Supabase, not missing pages." >&2
set +e
pg_restore --no-owner --no-acl --dbname="$TARGET_DATABASE_URL" --jobs="${GBRAIN_RESTORE_JOBS:-4}" "$DUMP" >"$LOG" 2>&1
rc=$?
set -e
if [[ "$rc" -ne 0 ]]; then
  echo "pg_restore exited $rc. Read $LOG. Lines about supabase roles are expected. Confirm the public tables before you point gbrain at the target." >&2
fi

psql "$TARGET_DATABASE_URL" -v ON_ERROR_STOP=1 -c 'ANALYZE;'
echo "Restore finished. Dump: $DUMP  Log: $LOG" >&2
echo "Next: gbrain config set database_url '<target>' && gbrain doctor" >&2
