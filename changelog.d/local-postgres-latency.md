**Reading a page over a remote Postgres overlaps the lookups that do not depend on each other.**

### Changed
- `get_page` and the slug spelling check waited for each independent read before starting the next one. On a transaction-mode pooler those waits are network round trips. The reads now start together. The page you get back is the same.

### Added
- A Postgres latency benchmark (`scripts/local-postgres/bench.py`) and a Mac mini hosting guide (`docs/guides/local-postgres-mac.md`): a synthetic brain, a read-only probe for an existing database, dump/restore from Supabase, nightly `pg_dump`, and Tailscale.
