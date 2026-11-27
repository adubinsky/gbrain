# Synthetic brain timings

Median milliseconds. p95 is in the JSON files next to this note. Embedding API time is not included.

Corpus: 4,000 pages, 12,000 chunks, 1,024-dimension vectors (gbrain's default embedding width). Postgres 17.12, pgvector 0.8.7, HNSW on `content_chunks.embedding`, GIN on the chunk and page `tsvector`s. `shared_buffers` 128 MB, `work_mem` 4 MB. The client and the server were on the same machine (x86_64 Linux, not an Apple Silicon Mac). `local+latency` is a TCP proxy that adds about 45 ms RTT, a stand-in for Austin to `us-east-2`. `pooler` is PgBouncer in transaction mode. `pooler+latency` is that pooler behind the same proxy.

The vector statement is the index walk gbrain uses: JIT off, `hnsw.ef_search` 80, `enable_sort` off, then `ORDER BY embedding <=> $q LIMIT 40`. EXPLAIN was an HNSW index scan. Keyword search was a GIN bitmap index scan. Page read was the unique `(source_id, slug)` index.

Each timed call waited for the server before sending the next statement. gbrain pipelines statements that share one transaction, including through a transaction pooler, so a multi-statement write it sends as one transaction is closer to a single round trip plus server time. The ingest row is the unpipelined client: several inserts waited in series. Read it as "how many round trips a chatty write pays," not as gbrain's ingest path. The page-read and config rows are separate waited calls, which pipelining cannot collapse.

| Operation | local | local+latency | pooler | pooler+latency |
| --- | ---: | ---: | ---: | ---: |
| TCP connect + auth | 4.95 | 241.44 | 0.26 | 143.33 |
| ping (`SELECT 1`) | 0.03 | 47.31 | 0.05 | 47.84 |
| get_page, one indexed read | 0.05 | 47.56 | 0.07 | 48.06 |
| get_page shape, 5 serial reads | 0.22 | 238.09 | 0.30 | 238.35 |
| get_page shape, overlapped reads | 0.28 | 143.88 | 0.45 | 144.12 |
| keyword search, rare term | 0.57 | 48.74 | 0.58 | 48.77 |
| keyword search, common term | 7.75 | 56.88 | 8.26 | 57.58 |
| vector search, HNSW, JIT off | 1.73 | 50.94 | 1.76 | 51.04 |
| hybrid, database only | 2.08 | 101.08 | 2.35 | 100.25 |
| ingest, unpipelined inserts | 4.81 | 295.26 | 5.94 | 297.53 |
| 6 serial config reads | 0.22 | 286.31 | 0.37 | 287.89 |
| 1 batched config read | 0.05 | 47.74 | 0.07 | 47.82 |
| vector search, JIT on (local) | 1.71 |  |  |  |
| vector search, ef_search 200 (local) | 3.03 |  |  |  |
| vector search, HNSW disabled (local) | 33.24 |  |  |  |

Plans on the local connection:

- `get_page`: Index Scan using `pages_source_id_slug_key`
- `keyword_rare`: Bitmap Index Scan on `idx_chunks_search`
- `vector`: Index Scan using `idx_chunks_embedding`

A TLS handshake from this same machine, not an embedding request and not from Austin, was 55 ms to `api.voyageai.com` and 41 ms to `api.openai.com`. That is a floor under a real embedding call. It is not in the table.

Raw samples: `results/local.json`, `results/local+latency.json`, `results/pooler.json`, `results/pooler+latency.json`.
