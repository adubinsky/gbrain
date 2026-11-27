#!/usr/bin/env bash
# Re-run the four-setup synthetic matrix against an already seeded gbrain_bench.
#
#   export BENCH_URL_LOCAL='postgresql://USER@127.0.0.1:5432/gbrain_bench'
#   export BENCH_URL_LATENCY='postgresql://USER@127.0.0.1:15432/gbrain_bench'
#   export BENCH_URL_POOLER='postgresql://USER@127.0.0.1:6432/gbrain_bench'
#   export BENCH_URL_POOLER_LATENCY='postgresql://USER@127.0.0.1:15433/gbrain_bench'
#   scripts/local-postgres/run-matrix.sh
#
# Start latency_proxy.py and PgBouncer yourself (see docs/guides/local-postgres-mac.md
# and pgbouncer.ini.example). Password via ~/.pgpass. Seed once with:
#   python3 scripts/local-postgres/bench.py seed --url "$BENCH_URL_LOCAL"
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${GBRAIN_BENCH_OUT_DIR:-$PWD/gbrain-bench-matrix}"
mkdir -p "$OUT"
: "${BENCH_URL_LOCAL:?}"
: "${BENCH_URL_LATENCY:?}"
: "${BENCH_URL_POOLER:?}"
: "${BENCH_URL_POOLER_LATENCY:?}"

run() {
  local setup="$1" url="$2"
  python3 "$ROOT/scripts/local-postgres/bench.py" synthetic --no-seed --setup "$setup" \
    --url "$url" --out "$OUT/${setup}.json"
}
run local "$BENCH_URL_LOCAL"
run local+latency "$BENCH_URL_LATENCY"
run pooler "$BENCH_URL_POOLER"
run pooler+latency "$BENCH_URL_POOLER_LATENCY"
python3 "$ROOT/scripts/local-postgres/bench.py" report \
  "$OUT/local.json" "$OUT/local+latency.json" "$OUT/pooler.json" "$OUT/pooler+latency.json" \
  --out "$OUT/RESULTS.md"
echo "Wrote $OUT" >&2
