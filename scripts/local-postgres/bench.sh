#!/usr/bin/env bash
# Time gbrain's hot SQL.
#
#   GBRAIN_BENCH_URL='postgresql://user@host:5432/db' scripts/local-postgres/bench.sh probe
#   GBRAIN_BENCH_URL='postgresql://user@127.0.0.1:5432/gbrain_bench' scripts/local-postgres/bench.sh synthetic
#
# Put the password in ~/.pgpass (mode 0600), not in the URL. probe is read-only
# and talks to an existing gbrain database. synthetic builds a 4,000-page
# corpus and refuses any Supabase host and any database not named gbrain_bench.
set -euo pipefail

MODE="${1:-probe}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${GBRAIN_BENCH_OUT:-$PWD/gbrain-bench-${MODE}.json}"
URL="${GBRAIN_BENCH_URL:-}"

if [[ -z "$URL" ]]; then
  echo "Set GBRAIN_BENCH_URL to a Postgres URL. Put the password in ~/.pgpass, not in the URL." >&2
  exit 2
fi

case "$MODE" in
  probe)
    python3 "$ROOT/scripts/local-postgres/bench.py" probe --url "$URL" --out "$OUT"
    ;;
  synthetic)
    python3 "$ROOT/scripts/local-postgres/bench.py" synthetic --url "$URL" --setup local --out "$OUT"
    ;;
  *)
    echo "usage: bench.sh probe|synthetic" >&2
    exit 2
    ;;
esac

echo "Wrote $OUT" >&2
