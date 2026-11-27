# Postgres on a Mac mini

gbrain's search and page reads are small SQL statements. On a database in another region, almost all of the wait is the network round trip, repeated once per statement the server has to answer before the next one starts. A Postgres 17 + pgvector instance on the same Mac as the agents removes that wait. The embedding API does not move with it: a `query` still calls the embedding provider before vector search.

**Say to your agent:** *"Move my gbrain database off Supabase onto Postgres on this Mac, and show me the plan before you change anything."*

**Say to your agent:** *"Time my brain against Supabase and against the local database, and tell me which part is the network."*

The measured numbers and the method are in [scripts/local-postgres/RESULTS.md](../../scripts/local-postgres/RESULTS.md).

## What is slow

From a client on the same machine as Postgres 17.12 and pgvector 0.8.7, with 4,000 pages, 12,000 chunks, and 1,024-dimension vectors (gbrain's current default embedding width):

| Operation | Local | About 45 ms RTT | PgBouncer transaction mode | Pooler + 45 ms RTT |
| --- | ---: | ---: | ---: | ---: |
| One indexed page read | 0.05 ms | 48 ms | 0.07 ms | 48 ms |
| Page read, 5 serial lookups | 0.22 ms | 238 ms | 0.30 ms | 238 ms |
| Page read, lookups overlapped | 0.28 ms | 144 ms | 0.45 ms | 144 ms |
| Keyword search, rare word | 0.57 ms | 49 ms | 0.58 ms | 49 ms |
| Keyword search, common word | 7.8 ms | 57 ms | 8.3 ms | 58 ms |
| Vector search (HNSW, JIT off) | 1.7 ms | 51 ms | 1.8 ms | 51 ms |
| Hybrid search, database only | 2.1 ms | 101 ms | 2.4 ms | 100 ms |
| 6 config reads, one after another | 0.22 ms | 286 ms | 0.37 ms | 288 ms |
| Those config keys in one query | 0.05 ms | 48 ms | 0.07 ms | 48 ms |
| Vector search, HNSW disabled | 33 ms |  |  |  |

45 ms is a stand-in for Austin to `us-east-2`. The pooler column is PgBouncer in transaction mode, the same mode as Supabase port 6543. Embedding HTTP time is not in the table.

What that ranks:

1. **Distance.** An indexed read the server finishes in well under 2 ms takes about 48 ms once the round trip is 45 ms. A page fetch that still does several lookups in series takes about 240 ms. Hybrid search's database work is two waves (the arms, then the follow-up reads), about 100 ms. The pooler added under 1 ms. It is not the cause.
2. **How many round trips the operation waits out.** Overlapping the independent page lookups cuts the 45 ms case from 238 ms to 144 ms. Reading six config keys one by one costs 286 ms; one query costs 48 ms. gbrain already loads the config table once per request. This change does the same for the page read's independent lookups.
3. **A missing vector index, once the brain is large.** With the HNSW index disabled, the same 12,000-chunk search took 33 ms instead of 1.7 ms. That grows with the number of chunks. At a few thousand pages it is still smaller than one round trip. At a few hundred thousand chunks it becomes the slow part. The plan for the indexed query was an HNSW index scan; keyword search used the GIN index. Both indexes are already in gbrain's schema.
4. **The embedding API, for `query` only.** Keyword search does not call it. Vector search runs after it. A TLS handshake from the benchmark machine (not from Austin, and not an embedding) was 55 ms to the Voyage API host and 41 ms to the OpenAI API host. The provider's own compute is on top of that. A Mac mini does not remove it.
5. **A cold connection.** Opening a TLS connection to Postgres at 45 ms RTT took about 240 ms. An idle client that has to reconnect pays that before the first query. A paused Supabase project is a separate, longer cold start; the dashboard shows it.

JIT on versus off was a tie on this simple vector statement (1.7 ms). Raising `hnsw.ef_search` from 80 to 200 moved it from 1.7 ms to 3.0 ms. Neither is why a remote brain feels slow. gbrain already turns JIT off for search, because the full search statement on a large brain can spend most of its time compiling.

A small Supabase compute size is not what made these indexed reads slow: this Postgres had `shared_buffers` at 128 MB and `work_mem` at 4 MB, and the server time stayed in the single-digit milliseconds. Check the dashboard anyway. If CPU or disk IO is busy, the tier matters, and it matters more as the chunk count grows. If those graphs are idle while search feels slow, the region is the cause.

## Recommendation

Run Postgres on the Mac mini if agents on that machine are waiting on search, get, or ingest. Those calls drop from tens or hundreds of milliseconds to a few milliseconds of database time. Keep the embedding provider where it is, and expect `query` to still wait on it.

Staying on Supabase is reasonable when the agents are not on that Mac, or when you want hosted backups and you can tolerate about one round trip per lookup. Use the checklist below before changing anything. Raising the compute size does not remove a 45 ms round trip.

## Supabase checklist

In the project dashboard, before you migrate:

- **Compute.** Settings → Compute and Disk. Note the size (Nano, Micro, Small, …). Open the CPU and disk IO charts for a period when search felt slow. Idle CPU and idle disk with slow searches means the network, not the tier.
- **Pooler.** Connect → Connection string. Port **6543** is transaction mode. Port **5432** on the pooler host is session mode. The direct host is `db.<project-ref>.supabase.co` port 5432. gbrain turns prepared statements and cross-statement pipelining off on 6543, and it pipelines statements inside a single transaction either way. For a long-lived `gbrain serve`, session mode or the direct host lets startup timeouts reach the backend. It does not remove the round trip. `pg_dump` needs session mode or the direct host; transaction mode often rejects the dump.
- **Disk.** A small disk or a disk at its IO ceiling shows up as read waits in the disk chart. The vector index wants to stay in memory. `shared_buffers` on a small tier can be 128 MB or less.
- **Region.** A project in `us-east-2` (Ohio) is about 35–50 ms from Austin. There is no closer Supabase region that changes the picture the way localhost does.
- **Pause.** A free project that paused pays a cold start that no index fixes. The probe script's first connection time includes reconnect, not the pause itself.
- **Vector index.** From the Mac, the read-only probe below prints whether `content_chunks.embedding` has an HNSW index, an IVFFlat index, or none, and the column width. `gbrain doctor --only vector_plan` checks that search actually uses the index. If the access method is `none`, create the HNSW index from the machine that can run `CREATE INDEX`, not from a laptop on a flaky link. Doctor prints the statement.

Do not put the database password in a chat, a shell history you share, or a command line that ends up in a process list. Use a `~/.pgpass` file (mode 0600) or a 0600 env file.

## Run Postgres on the Mac

Pick one install. All three are Postgres 17 with pgvector. The published timings used native Postgres 17.12 and pgvector 0.8.7 on Linux, because that machine had no Docker. The statements are the same.

### Docker

From the repo:

```bash
cp scripts/local-postgres/env.example scripts/local-postgres/.env
# edit POSTGRES_PASSWORD
docker compose -f scripts/local-postgres/docker-compose.yml up -d
docker compose -f scripts/local-postgres/docker-compose.yml exec postgres \
  psql -U gbrain -d gbrain -c 'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS pgcrypto;'
```

The image `pgvector/pgvector:pg17` publishes an Apple Silicon build. The port is bound to `127.0.0.1` only. If Docker is short on memory, lower `shared_buffers` in the compose file (1 GB is a starting point when the Mac has memory to spare, not a requirement).

### Homebrew

```bash
brew install postgresql@17 pgvector
brew services start postgresql@17
createuser -s gbrain || true
createdb -O gbrain gbrain
psql -d gbrain -c 'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS pgcrypto;'
```

If `CREATE EXTENSION vector` cannot find the library, `brew services restart postgresql@17` after `brew install pgvector`. The extension builds for the Postgres that `pg_config` points at; put `postgresql@17` first on `PATH` (`brew --prefix postgresql@17`).

### Postgres.app

Install the Postgres.app release that includes PostgreSQL 17. Open the app, create a database named `gbrain`, and in its `psql`:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
```

Recent builds ship pgvector. `\dx` should list `vector`.

### Point gbrain at it

```bash
gbrain config set database_url 'postgresql://gbrain@127.0.0.1:5432/gbrain'
gbrain doctor
```

Restart `gbrain serve` (quit the agent session that holds a stdio server, then start it again). A password belongs in `~/.pgpass`, not in the URL you paste into a transcript.

```
127.0.0.1:5432:gbrain:gbrain:the-password
```

`chmod 600 ~/.pgpass`.

## Move the data off Supabase

**Say to your agent:** *"Dump my Supabase gbrain database and restore it into the local Postgres. Don't print the connection string."*

Stop writers first so the dump is the brain you mean to keep: quit agents that have `gbrain serve` open, and stop autopilot if it is running. `pg_dump` is a consistent snapshot; writes that commit after it starts are not in the file.

The source URL must be the **direct** connection or the **session** pooler (port 5432). Port 6543 is the wrong source for a dump.

Put the URLs in a 0600 file and point the script at it. The script reads `SOURCE_DATABASE_URL` and `TARGET_DATABASE_URL` from the environment. It does not take them as arguments.

```bash
umask 077
cat > ~/.gbrain/migrate.env << 'EOF'
SOURCE_DATABASE_URL=postgresql://postgres.your-project-ref@aws-0-us-east-2.pooler.supabase.com:5432/postgres
TARGET_DATABASE_URL=postgresql://gbrain@127.0.0.1:5432/gbrain
EOF
chmod 600 ~/.gbrain/migrate.env
set -a && . ~/.gbrain/migrate.env && set +a
scripts/local-postgres/migrate-from-supabase.sh
```

Use the host and user the dashboard shows. The `postgres.<project-ref>` user is the pooler form. The direct host looks like `db.<project-ref>.supabase.co`. If the direct host only has an IPv6 address and your network cannot use it, use the session pooler on port 5432.

The script creates `vector`, `pg_trgm`, and `pgcrypto` on the target, dumps the `public` schema with `pg_dump --format=custom --no-owner --no-privileges`, restores with `pg_restore --no-owner --no-acl`, and runs `ANALYZE`. Messages about hosted roles (`supabase_admin` and similar) are leftovers from the platform. The public tables are the brain.

Then:

```bash
gbrain config set database_url 'postgresql://gbrain@127.0.0.1:5432/gbrain'
gbrain doctor
```

Keep the Supabase project until doctor is clean and a search you know the answer to returns it. Downgrade or pause the project only after that. The dump file is a full copy of the brain; store it like a backup, not in the brain repo.

## Nightly backups

**Say to your agent:** *"Set a nightly pg_dump of the local gbrain database onto my backup disk."*

```bash
umask 077
cat > ~/.gbrain/backup.env << 'EOF'
DATABASE_URL=postgresql://gbrain@127.0.0.1:5432/gbrain
BACKUP_DIR=/Volumes/your-nas/gbrain-backups
BACKUP_RETAIN_DAYS=14
EOF
chmod 600 ~/.gbrain/backup.env
```

`BACKUP_DIR` can be a NAS mount or a Dropbox / iCloud folder (`~/Library/CloudStorage/Dropbox/gbrain-backups`). The script writes `gbrain-<timestamp>.dump` and deletes dumps older than the retain window. A Markdown export is not a substitute: the database holds embeddings, history, and rows that are not in the files.

Try it once by hand:

```bash
set -a && . ~/.gbrain/backup.env && set +a
scripts/local-postgres/backup-nightly.sh
```

Schedule it with launchd. Copy `scripts/local-postgres/com.gbrain.postgres-backup.plist` to `~/Library/LaunchAgents/`, replace `YOURUSER` and both `/ABSOLUTE/PATH` values (the script, and `backup.env`), then:

```bash
launchctl load ~/Library/LaunchAgents/com.gbrain.postgres-backup.plist
```

The default schedule is 03:15 local time. Logs go to `~/Library/Logs/gbrain-backup.log`.

## Agents on other machines (Tailscale)

**Say to your agent:** *"Publish this brain on my tailnet so my other agents can use it. Don't open Postgres to the internet."*

The database stays on `127.0.0.1`. Agents elsewhere connect to gbrain, not to Postgres:

```bash
gbrain mcp expose
```

That is the supported path (Tailscale, then a client grant). The steps, the admin token, and what not to expose are in [Use your brain from anywhere over MCP](remote-mcp.md). A raw `DATABASE_URL` pointed at the Mac from another machine skips gbrain's auth and row rules.

Use a direct Postgres connection over Tailscale only when a second gbrain process on another machine must talk to this database. Then:

- Install Tailscale on the Mac and note its tailnet IP (`tailscale ip -4`).
- In `postgresql.conf`, set `listen_addresses` to `127.0.0.1` and that tailnet IP. Do not use `0.0.0.0` or `*`.
- In `pg_hba.conf`, allow the tailnet (`100.64.0.0/10` for Tailscale's CGNAT range) with `scram-sha-256` for the `gbrain` role only. No `trust` except on the local socket.
- Restart Postgres. From the other machine, `psql` over the tailnet IP should work, and a connection to the Mac's public IP on 5432 should fail.
- Do not turn on Tailscale Funnel for port 5432.

## Benchmark both databases

Read-only. It pings, times a page read and a keyword lookup, times one vector search if embeddings exist, and prints server settings (`shared_buffers`, JIT, whether the vector index is HNSW, IVFFlat, or missing). It does not print page text or the URL.

```bash
# Supabase (session or direct URL, port 5432). Password via ~/.pgpass.
GBRAIN_BENCH_URL='postgresql://postgres.your-project-ref@db.your-project-ref.supabase.co:5432/postgres' \
  scripts/local-postgres/bench.sh probe

# The local database, after the move.
GBRAIN_BENCH_URL='postgresql://gbrain@127.0.0.1:5432/gbrain' \
  scripts/local-postgres/bench.sh probe
```

Compare `ping` first. If ping is ~40–50 ms against Supabase and under 1 ms locally, the rest of the gap is that round trip times the number of lookups. If ping is fast and `vector_search` is slow, look at `vector_access_method` and the compute charts.

To reproduce the synthetic corpus (a database named `gbrain_bench` on localhost only; the script refuses a Supabase host):

```bash
GBRAIN_BENCH_URL='postgresql://gbrain@127.0.0.1:5432/gbrain_bench' \
  scripts/local-postgres/bench.sh synthetic
```

Create the empty database first. The synthetic load writes 4,000 pages. Do not point it at the database you just restored.

## Knobs that already exist

No new setting. These are the ones the timings bear on:

| Knob | What it does |
| --- | --- |
| Connection port 6543 | Transaction pooler. gbrain disables prepared statements and cross-statement pipelining. Inside one transaction it still pipelines. Fine for short autocommit reads; use 5432 for `pg_dump` and for a long-lived server if startup timeouts are not arriving. |
| `GBRAIN_POOL_SIZE` | Connections per process. Supabase's pooler has a small client cap. Lower this before raising it. |
| `GBRAIN_PREPARE=0` | Same prepared-statement ban as port 6543, on any port. |
| `GBRAIN_PG_TXN_PIPELINE=0` | Sends each statement in a transaction as its own round trip. Leave it unset. |
| `gbrain doctor --only vector_plan` | Warns when vector search is not using the HNSW index. |
| `hnsw.ef_search` | gbrain sets this per search. 200 instead of 80 cost about 1 ms here. Not the remote wait. |
