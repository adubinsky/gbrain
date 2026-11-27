#!/usr/bin/env python3
"""Time gbrain's hot SQL against Postgres.

Two modes:

  synthetic   Build a throwaway brain (pages, chunks, 1024-d embeddings, HNSW,
              GIN) and time the statements search / get_page / ingest run.
              Refuses any host containing supabase.co so this cannot load
              data into a hosted project.

  probe       Read-only timings against an existing gbrain database
              (Supabase or local). Prints medians only. Does not print
              slugs, page text, or the connection string.

  report      Turn one JSON file per setup into a markdown table.

Embedding API time is not included. Every number is database + network.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import numpy as np
import psycopg2

DIM_DEFAULT = 1024
PAGES_DEFAULT = 4000
CHUNKS_PER_PAGE = 3
RARE = 'zephyrwidget'
COMMON = 'meeting'
WORDS = [
    'atlas', 'bridge', 'canvas', 'delta', 'ember', 'field', 'garden', 'harbor',
    'index', 'journal', 'kernel', 'ledger', 'meadow', 'north', 'orbit', 'paper',
    'quartz', 'river', 'signal', 'timber', 'umbra', 'valley', 'willow', 'year',
]


def _host_port(url: str) -> tuple[str, int | None]:
    parsed = urlparse(url)
    return parsed.hostname or '', parsed.port


def _refuse_remote_seed(url: str) -> None:
    host, _ = _host_port(url)
    if 'supabase.co' in host or 'pooler.supabase.com' in host:
        raise SystemExit('refusing to seed a Supabase host; synthetic mode is for a local database named gbrain_bench')
    name = urlparse(url).path.lstrip('/')
    if name.split('?')[0] != 'gbrain_bench':
        raise SystemExit(f'refusing to seed database {name!r}; create a database named gbrain_bench')


def connect(url: str, statement_timeout: str = '60s'):
    if not re.fullmatch(r'[0-9A-Za-z]+', statement_timeout):
        raise ValueError('bad statement_timeout')
    conn = psycopg2.connect(url, connect_timeout=15)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = '{statement_timeout}'")
    return conn


def _pct(samples: list[float], p: float) -> float:
    if not samples:
        return float('nan')
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, math.ceil(p / 100 * len(ordered)) - 1))
    return ordered[idx]


def time_ms(fn, n: int = 25, warmup: int = 5) -> dict:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return {
        'n': n,
        'median_ms': round(statistics.median(samples), 2),
        'p95_ms': round(_pct(samples, 95), 2),
        'min_ms': round(min(samples), 2),
        'max_ms': round(max(samples), 2),
    }


class Pool:
    def __init__(self, url: str, n: int = 4):
        self.conns = [connect(url) for _ in range(n)]
        self.ex = ThreadPoolExecutor(max_workers=n)

    def close(self) -> None:
        self.ex.shutdown(wait=True)
        for conn in self.conns:
            conn.close()

    def parallel(self, fns: list) -> None:
        futs = [self.ex.submit(fn) for fn in fns]
        for fut in futs:
            fut.result()


def _vec_literals(mat: np.ndarray) -> list[str]:
    buf_lines = np.char.mod('%.5f', mat)
    return ['[' + ','.join(row) + ']' for row in buf_lines]


def seed(url: str, pages: int, dim: int) -> None:
    _refuse_remote_seed(url)
    conn = connect(url, '0')
    cur = conn.cursor()
    cur.execute('CREATE EXTENSION IF NOT EXISTS vector')
    cur.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
    cur.execute('CREATE EXTENSION IF NOT EXISTS pgcrypto')
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sources (
          id text PRIMARY KEY,
          archived_at timestamptz
        )""")
    cur.execute("INSERT INTO sources (id) VALUES ('default') ON CONFLICT DO NOTHING")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pages (
          id serial PRIMARY KEY,
          source_id text NOT NULL DEFAULT 'default' REFERENCES sources(id),
          slug text NOT NULL,
          title text NOT NULL,
          type text NOT NULL DEFAULT 'note',
          compiled_truth text NOT NULL,
          frontmatter jsonb NOT NULL DEFAULT '{}'::jsonb,
          search_vector tsvector,
          deleted_at timestamptz,
          updated_at timestamptz NOT NULL DEFAULT now(),
          created_at timestamptz NOT NULL DEFAULT now(),
          effective_date timestamptz,
          UNIQUE (source_id, slug)
        )""")
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS content_chunks (
          id serial PRIMARY KEY,
          page_id integer NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
          chunk_index integer NOT NULL,
          chunk_text text NOT NULL,
          embedding vector({dim}),
          search_vector tsvector,
          modality text NOT NULL DEFAULT 'text',
          model text NOT NULL DEFAULT 'bench',
          UNIQUE (page_id, chunk_index)
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS links (
          id serial PRIMARY KEY,
          from_page_id integer NOT NULL,
          to_page_id integer NOT NULL,
          link_type text NOT NULL DEFAULT 'mentions'
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
          key text PRIMARY KEY,
          value text NOT NULL
        )""")
    cur.execute('SELECT count(*) FROM pages')
    have = cur.fetchone()[0]
    cur.execute('SELECT count(*) FROM content_chunks')
    have_chunks = cur.fetchone()[0]
    if have >= pages and have_chunks >= pages * CHUNKS_PER_PAGE:
        print(f'seed: {have} pages already present, skipping load', file=sys.stderr)
        _indexes(cur)
        conn.close()
        return
    if have or have_chunks:
        cur.execute('TRUNCATE links, content_chunks, pages RESTART IDENTITY CASCADE')

    print(f'seed: inserting {pages} pages', file=sys.stderr)
    rng = random.Random(1024)
    np.random.seed(1024)
    page_rows = []
    for i in range(pages):
        slug = f'notes/page-{i:05d}'
        topic = WORDS[i % len(WORDS)]
        title = f'{topic} note {i}'
        body_words = [WORDS[rng.randrange(len(WORDS))] for _ in range(80)]
        body_words[0] = COMMON
        if i % 100 == 0:
            body_words[1] = RARE
        body = ' '.join(body_words)
        page_rows.append(('default', slug, title, body))
    from psycopg2.extras import execute_values
    execute_values(
        cur,
        'INSERT INTO pages (source_id, slug, title, compiled_truth) VALUES %s',
        page_rows,
        page_size=500,
    )
    cur.execute("""
        UPDATE pages SET search_vector =
          setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
          setweight(to_tsvector('english', coalesce(compiled_truth, '')), 'B')
    """)

    cur.execute('SELECT id, compiled_truth FROM pages ORDER BY id')
    loaded = cur.fetchall()
    print(f'seed: embedding {len(loaded) * CHUNKS_PER_PAGE} chunks at dim {dim}', file=sys.stderr)
    batch = 400
    chunk_i = 0
    pending_page = []
    for page_id, body in loaded:
        parts = body.split()
        for ci in range(CHUNKS_PER_PAGE):
            piece = ' '.join(parts[ci::CHUNKS_PER_PAGE]) or body
            pending_page.append((page_id, ci, piece))
            if len(pending_page) >= batch:
                _insert_chunks(cur, pending_page, dim)
                pending_page = []
                chunk_i += batch
                if chunk_i % 2400 == 0:
                    print(f'seed: {chunk_i} chunks', file=sys.stderr)
    if pending_page:
        _insert_chunks(cur, pending_page, dim)

    cur.execute('SELECT id FROM pages ORDER BY id')
    ids = [row[0] for row in cur.fetchall()]
    link_rows = []
    for src in ids:
        for k in range(2):
            dst = ids[(src + 17 + k * 31) % len(ids)]
            if dst != src:
                link_rows.append((src, dst))
    execute_values(cur, 'INSERT INTO links (from_page_id, to_page_id) VALUES %s', link_rows, page_size=1000)
    for key, value in (
        ('embedding_dimensions', str(dim)),
        ('embedding_model', 'voyage:voyage-4'),
        ('search.mode', 'balanced'),
        ('engine', 'postgres'),
    ):
        cur.execute(
            'INSERT INTO config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
            (key, value),
        )
    _indexes(cur)
    conn.close()
    print('seed: done', file=sys.stderr)


def _insert_chunks(cur, rows: list[tuple[int, int, str]], dim: int) -> None:
    from psycopg2.extras import execute_values
    mat = np.random.standard_normal((len(rows), dim)).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.maximum(norms, 1e-6)
    lits = _vec_literals(mat)
    payload = []
    for (page_id, ci, text), lit in zip(rows, lits):
        payload.append((page_id, ci, text, lit, text))
    execute_values(
        cur,
        """INSERT INTO content_chunks (page_id, chunk_index, chunk_text, embedding, search_vector, modality, model)
           VALUES %s""",
        payload,
        template="(%s,%s,%s,%s::vector,to_tsvector('english', %s),'text','bench')",
        page_size=100,
    )


def _indexes(cur) -> None:
    cur.execute("SET maintenance_work_mem = '512MB'")
    cur.execute('CREATE INDEX IF NOT EXISTS idx_pages_search ON pages USING GIN (search_vector)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_pages_trgm ON pages USING GIN (title gin_trgm_ops)')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_page ON content_chunks (page_id)")
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_embedding
        ON content_chunks USING hnsw (embedding vector_cosine_ops)""")
    cur.execute('CREATE INDEX IF NOT EXISTS idx_chunks_search ON content_chunks USING GIN (search_vector)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_links_to ON links (to_page_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_links_from ON links (from_page_id)')
    cur.execute("""
        CREATE OR REPLACE FUNCTION bench_page_sv() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          NEW.search_vector :=
            setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(NEW.compiled_truth, '')), 'B');
          RETURN NEW;
        END $$""")
    cur.execute('DROP TRIGGER IF EXISTS bench_page_sv ON pages')
    cur.execute("""
        CREATE TRIGGER bench_page_sv BEFORE INSERT OR UPDATE OF title, compiled_truth
        ON pages FOR EACH ROW EXECUTE FUNCTION bench_page_sv()""")
    cur.execute("""
        CREATE OR REPLACE FUNCTION bench_chunk_sv() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          NEW.search_vector := to_tsvector('english', coalesce(NEW.chunk_text, ''));
          RETURN NEW;
        END $$""")
    cur.execute('DROP TRIGGER IF EXISTS bench_chunk_sv ON content_chunks')
    cur.execute("""
        CREATE TRIGGER bench_chunk_sv BEFORE INSERT OR UPDATE OF chunk_text
        ON content_chunks FOR EACH ROW EXECUTE FUNCTION bench_chunk_sv()""")
    cur.execute('ANALYZE pages')
    cur.execute('ANALYZE content_chunks')
    cur.execute('ANALYZE links')


def _one(conn, sql: str, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        try:
            cur.fetchone()
        except psycopg2.ProgrammingError:
            pass


def _plan_summary(conn, sql: str, args=None) -> str:
    with conn.cursor() as cur:
        cur.execute('EXPLAIN ' + sql, args)
        lines = [row[0] for row in cur.fetchall()]
    interesting = [ln.strip()[:160] for ln in lines if any(tok in ln for tok in ('Scan', 'Index Only', 'Bitmap', 'Limit'))]
    return ' | '.join(interesting[:4]) if interesting else (lines[0][:160] if lines else '')


def run_synthetic(url: str, setup: str, pages: int, dim: int, iters: int) -> dict:
    pool = Pool(url, 4)
    conn = pool.conns[0]
    with conn.cursor() as cur:
        cur.execute('SELECT version()')
        version = cur.fetchone()[0]
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()
        vector_version = row[0] if row else None
        cur.execute('SELECT count(*) FROM pages')
        page_n = cur.fetchone()[0]
        cur.execute('SELECT count(*) FROM content_chunks')
        chunk_n = cur.fetchone()[0]
        cur.execute("SELECT slug FROM pages WHERE deleted_at IS NULL ORDER BY id LIMIT 40")
        slugs = [r[0] for r in cur.fetchall()]
        cur.execute('SELECT id FROM pages ORDER BY id LIMIT 20')
        page_ids = [r[0] for r in cur.fetchall()]
        cur.execute('SELECT embedding::text FROM content_chunks WHERE embedding IS NOT NULL LIMIT 1')
        query_vec = cur.fetchone()[0]
        cur.execute("""
            SELECT a.amname
            FROM pg_index i
            JOIN pg_class ic ON ic.oid = i.indexrelid
            JOIN pg_class tc ON tc.oid = i.indrelid
            JOIN pg_am a ON a.oid = ic.relam
            JOIN pg_attribute att ON att.attrelid = tc.oid AND att.attnum = ANY (i.indkey)
            WHERE tc.relname = 'content_chunks' AND att.attname = 'embedding'
        """)
        vector_index = sorted({r[0] for r in cur.fetchall()})

    # Vector arm: one open transaction with the same scan settings gbrain
    # sends before the candidate read (JIT off, ef_search, enable_sort off
    # so the HNSW index is the ordered path). Timed calls send only the SELECT,
    # which is one round trip. The settings statement is not in the timer;
    # gbrain pipelines it with the read.
    vconn = pool.conns[3]
    vconn.autocommit = False

    def apply_vector_settings(jit: str = 'off', ef: str = '80') -> None:
        with vconn.cursor() as cur:
            cur.execute(
                "SELECT set_config('jit', %s, true), set_config('hnsw.ef_search', %s, true), set_config('enable_sort', 'off', true)",
                (jit, ef),
            )

    apply_vector_settings()
    plans = {
        'get_page': _plan_summary(conn, """
            SELECT id, title FROM pages
            WHERE source_id = 'default' AND slug = %s AND deleted_at IS NULL""", (slugs[0],)),
        'keyword_rare': _plan_summary(conn, """
            SELECT cc.page_id FROM content_chunks cc
            JOIN pages p ON p.id = cc.page_id
            WHERE cc.search_vector @@ websearch_to_tsquery('english', %s)
              AND p.deleted_at IS NULL LIMIT 20""", (RARE,)),
        'vector': _plan_summary(vconn, """
            SELECT id FROM content_chunks
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector LIMIT 40""", (query_vec,)),
    }

    slug_i = {'n': 0}

    def next_slug() -> str:
        slug = slugs[slug_i['n'] % len(slugs)]
        slug_i['n'] += 1
        return slug

    def ping():
        _one(conn, 'SELECT 1')

    def get_page():
        _one(conn, """
            SELECT id, slug, title, octet_length(compiled_truth)
            FROM pages WHERE source_id = 'default' AND slug = %s AND deleted_at IS NULL
        """, (next_slug(),))

    def keyword(conn_, term: str):
        _one(conn_, """
            WITH matched AS (
              SELECT cc.page_id, cc.id AS chunk_id,
                     ts_rank(cc.search_vector, websearch_to_tsquery('english', %s))::float8 AS score
              FROM content_chunks cc
              JOIN pages p ON p.id = cc.page_id
              WHERE cc.search_vector @@ websearch_to_tsquery('english', %s)
                AND cc.modality = 'text' AND p.deleted_at IS NULL
            ), best AS (
              SELECT DISTINCT ON (page_id) page_id, chunk_id, score
              FROM matched ORDER BY page_id, score DESC, chunk_id
            )
            SELECT b.page_id, left(cc.chunk_text, 180)
            FROM best b
            JOIN content_chunks cc ON cc.id = b.chunk_id
            ORDER BY b.score DESC, b.page_id
            LIMIT 20
        """, (term, term))

    def title(conn_):
        _one(conn_, """
            SELECT id, slug FROM pages
            WHERE search_vector @@ websearch_to_tsquery('english', %s) AND deleted_at IS NULL
            ORDER BY ts_rank_cd(search_vector, websearch_to_tsquery('english', %s)) DESC
            LIMIT 20
        """, (RARE, RARE))

    def vector_select():
        _one(vconn, """
            SELECT id, page_id FROM content_chunks
            WHERE modality = 'text' AND embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT 40
        """, (query_vec,))

    def post_fusion(conn_):
        _one(conn_, """
            SELECT to_page_id, count(*) FROM links
            WHERE to_page_id = ANY(%s) GROUP BY 1
        """, (page_ids,))
        _one(conn_, """
            SELECT id, coalesce(effective_date, updated_at) FROM pages WHERE id = ANY(%s)
        """, (page_ids,))

    def get_page_before():
        # Five awaited reads: spelling page, spelling snapshot, handler
        # snapshot, held-file lookup, fence lookup. The shape before the
        # overlap change, on a pooler that does not pipeline.
        slug = next_slug()
        _one(conn, 'SELECT id FROM pages WHERE source_id = %s AND slug = %s', ('default', slug))
        _one(conn, 'SELECT id, title FROM pages WHERE source_id = %s AND slug = %s', ('default', slug))
        _one(conn, 'SELECT id, slug, title, octet_length(compiled_truth) FROM pages WHERE source_id = %s AND slug = %s', ('default', slug))
        _one(conn, 'SELECT to_page_id FROM links WHERE to_page_id = %s LIMIT 1', (page_ids[0],))
        _one(conn, 'SELECT key FROM config WHERE key = %s', ('search.mode',))

    def get_page_after():
        # Three waves: spelling probes in parallel, the handler read, then
        # the side reads in parallel. Matches get_page after the overlap.
        slug = next_slug()
        c1, c2 = pool.conns[1], pool.conns[2]
        pool.parallel([
            lambda: _one(c1, 'SELECT id FROM pages WHERE source_id = %s AND slug = %s', ('default', slug)),
            lambda: _one(c2, 'SELECT id, title FROM pages WHERE source_id = %s AND slug = %s', ('default', slug)),
        ])
        _one(conn, 'SELECT id, slug, title, octet_length(compiled_truth) FROM pages WHERE source_id = %s AND slug = %s', ('default', slug))
        pool.parallel([
            lambda: _one(c1, 'SELECT to_page_id FROM links WHERE to_page_id = %s LIMIT 1', (page_ids[0],)),
            lambda: _one(c2, 'SELECT key FROM config WHERE key = %s', ('search.mode',)),
        ])

    def hybrid():
        c1, c2, c3 = pool.conns[1], pool.conns[2], pool.conns[3]
        pool.parallel([
            lambda: keyword(c1, RARE),
            lambda: title(c2),
            lambda: vector_select(),
        ])
        pool.parallel([
            lambda: _one(c1, 'SELECT to_page_id, count(*) FROM links WHERE to_page_id = ANY(%s) GROUP BY 1', (page_ids,)),
            lambda: _one(c2, 'SELECT id FROM pages WHERE id = ANY(%s)', (page_ids,)),
        ])

    def config_serial():
        for key in ('embedding_dimensions', 'embedding_model', 'search.mode', 'engine', 'embedding_dimensions', 'search.mode'):
            _one(conn, 'SELECT value FROM config WHERE key = %s', (key,))

    def config_batched():
        _one(conn, 'SELECT key, value FROM config WHERE key = ANY(%s)', (
            ['embedding_dimensions', 'embedding_model', 'search.mode', 'engine'],))

    ingest_vec = _vec_literals(np.random.standard_normal((3, dim)).astype(np.float32))
    ingest_n = {'n': 0}
    ingested: list[str] = []

    def ingest():
        ingest_n['n'] += 1
        slug = f'bench-ingest-{time.time_ns()}'
        with conn.cursor() as cur:
            cur.execute('BEGIN')
            cur.execute(
                """INSERT INTO pages (source_id, slug, title, compiled_truth)
                   VALUES ('default', %s, %s, %s) RETURNING id""",
                (slug, 'ingest note', f'{COMMON} notes about {RARE} and the weekly review'),
            )
            page_id = cur.fetchone()[0]
            for ci, lit in enumerate(ingest_vec):
                cur.execute(
                    """INSERT INTO content_chunks (page_id, chunk_index, chunk_text, embedding, modality, model)
                       VALUES (%s, %s, %s, %s::vector, 'text', 'bench')""",
                    (page_id, ci, f'{COMMON} chunk {ci} {RARE}', lit),
                )
            cur.execute('COMMIT')
        ingested.append(slug)

    def vector_seqscan():
        with conn.cursor() as cur:
            cur.execute('BEGIN')
            cur.execute("SELECT set_config('enable_indexscan','off',true), set_config('enable_bitmapscan','off',true), set_config('jit','off',true)")
            cur.execute(
                """SELECT id FROM content_chunks
                   ORDER BY embedding <=> %s::vector LIMIT 40""",
                (query_vec,),
            )
            cur.fetchone()
            cur.execute('ROLLBACK')

    cold = []
    for _ in range(5):
        t0 = time.perf_counter()
        extra = connect(url)
        extra.close()
        cold.append((time.perf_counter() - t0) * 1000)

    ops = {
        'connect_cold': {
            'n': len(cold),
            'median_ms': round(statistics.median(cold), 2),
            'p95_ms': round(_pct(cold, 95), 2),
            'min_ms': round(min(cold), 2),
            'max_ms': round(max(cold), 2),
        },
        'ping': time_ms(ping, iters, 5),
        'get_page': time_ms(get_page, iters, 5),
        'get_page_serial': time_ms(get_page_before, max(10, iters // 2), 3),
        'get_page_overlapped': time_ms(get_page_after, max(10, iters // 2), 3),
        'keyword_rare': time_ms(lambda: keyword(conn, RARE), iters, 3),
        'keyword_common': time_ms(lambda: keyword(conn, COMMON), max(8, iters // 3), 2),
        'vector_search': time_ms(vector_select, iters, 3),
        'hybrid_db': time_ms(hybrid, max(8, iters // 3), 2),
        'ingest_page': time_ms(ingest, 8, 1),
        'config_serial_6': time_ms(config_serial, iters, 3),
        'config_batched': time_ms(config_batched, iters, 3),
    }
    if setup == 'local':
        apply_vector_settings('on', '80')
        ops['vector_jit_on'] = time_ms(vector_select, max(8, iters // 3), 2)
        apply_vector_settings('off', '200')
        ops['vector_ef_200'] = time_ms(vector_select, max(8, iters // 3), 2)
        apply_vector_settings('off', '80')
        ops['vector_seqscan'] = time_ms(vector_seqscan, 5, 1)

    if ingested:
        _one(conn, 'DELETE FROM pages WHERE slug = ANY(%s)', (ingested,))
    host, port = _host_port(url)
    pool.close()
    return {
        'setup': setup,
        'host': host,
        'port': port,
        'pages': page_n,
        'chunks': chunk_n,
        'dim': dim,
        'pg_version': version.split(',')[0],
        'pgvector': vector_version,
        'vector_index': vector_index,
        'plans': plans,
        'ops': ops,
        'target_pages': pages,
    }


def run_probe(url: str, iters: int) -> dict:
    """Read-only. Never prints row text."""
    host, port = _host_port(url)
    if port == 6543:
        print('probe: port 6543 is a transaction pooler; timings include that hop', file=sys.stderr)
    conn = connect(url)
    info: dict = {'host': host, 'port': port, 'mode': 'probe'}
    with conn.cursor() as cur:
        cur.execute('SELECT current_setting(\'server_version\'), current_setting(\'shared_buffers\'), current_setting(\'effective_cache_size\'), current_setting(\'work_mem\'), current_setting(\'max_connections\'), current_setting(\'jit\')')
        ver, buffers, cache, work, max_conn, jit = cur.fetchone()
        info.update({
            'server_version': ver, 'shared_buffers': buffers, 'effective_cache_size': cache,
            'work_mem': work, 'max_connections': max_conn, 'jit': jit,
        })
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()
        info['pgvector'] = row[0] if row else None
        cur.execute("""
            SELECT a.atttypmod
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = 'content_chunks' AND a.attname = 'embedding' AND NOT a.attisdropped
        """)
        typ = cur.fetchone()
        info['embedding_typmod'] = typ[0] if typ else None
        cur.execute("""
            SELECT c.relname, c.reltuples::bigint
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname IN ('pages', 'content_chunks')
        """)
        info['reltuples'] = {name: int(n) for name, n in cur.fetchall()}
        cur.execute("""
            SELECT t.relname, a.amname, ic.relname
            FROM pg_index i
            JOIN pg_class ic ON ic.oid = i.indexrelid
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_am a ON a.oid = ic.relam
            WHERE n.nspname = 'public' AND t.relname IN ('pages', 'content_chunks', 'links')
        """)
        indexes = {}
        for table, am, index in cur.fetchall():
            indexes.setdefault(table, []).append({'am': am, 'name': index})
        info['indexes'] = indexes
        hnsw = any(ix['am'] == 'hnsw' for ix in indexes.get('content_chunks', []))
        ivf = any(ix['am'] == 'ivfflat' for ix in indexes.get('content_chunks', []))
        gin = any(ix['am'] == 'gin' for ix in indexes.get('content_chunks', []))
        info['vector_access_method'] = 'hnsw' if hnsw else ('ivfflat' if ivf else 'none')
        info['chunk_gin'] = gin

        cur.execute("""
            SELECT source_id FROM pages WHERE deleted_at IS NULL LIMIT 1
        """)
        src = cur.fetchone()
        info['has_pages'] = src is not None

    ops = {'ping': time_ms(lambda: _one(conn, 'SELECT 1'), iters, 3)}
    if info['has_pages']:
        def meta():
            _one(conn, """
                SELECT id FROM pages WHERE deleted_at IS NULL AND id = (
                  SELECT id FROM pages WHERE deleted_at IS NULL ORDER BY id LIMIT 1)
            """)

        def body():
            _one(conn, """
                SELECT octet_length(compiled_truth) FROM pages
                WHERE id = (SELECT id FROM pages WHERE deleted_at IS NULL ORDER BY id LIMIT 1)
            """)

        ops['get_page_meta'] = time_ms(meta, iters, 3)
        ops['get_page_body_bytes'] = time_ms(body, max(5, iters // 3), 2)

        def keyword():
            _one(conn, """
                SELECT count(*) FROM (
                  SELECT 1 FROM content_chunks cc
                  WHERE cc.search_vector @@ websearch_to_tsquery('english', 'meeting')
                  LIMIT 50
                ) s
            """)

        try:
            ops['keyword_meeting'] = time_ms(keyword, max(5, iters // 2), 2)
        except Exception as err:
            ops['keyword_meeting'] = {'error': type(err).__name__}
            conn.rollback() if not conn.autocommit else None

        if info['vector_access_method'] != 'none' or info.get('embedding_typmod'):
            with conn.cursor() as cur:
                cur.execute('SELECT embedding::text FROM content_chunks WHERE embedding IS NOT NULL LIMIT 1')
                got = cur.fetchone()
            if got:
                qv = got[0]

                def vector():
                    _one(conn, """
                        WITH _cfg AS (
                          SELECT set_config('jit', 'off', true), set_config('hnsw.ef_search', '80', true)
                        )
                        SELECT id FROM content_chunks, _cfg
                        WHERE embedding IS NOT NULL
                        ORDER BY embedding <=> %s::vector
                        LIMIT 20
                    """, (qv,))

                ops['vector_search'] = time_ms(vector, max(5, iters // 2), 2)
                ops['vector_plan'] = _plan_summary(conn, """
                    SELECT id FROM content_chunks
                    ORDER BY embedding <=> %s::vector LIMIT 20""", (qv,))
            else:
                info['vector_note'] = 'no stored embeddings'
        else:
            info['vector_note'] = 'no vector index and no embedding column typmod'

    info['ops'] = ops
    conn.close()
    return info


def _cell(ops: dict, name: str) -> str:
    row = ops.get(name)
    if not row:
        return '—'
    if 'error' in row:
        return row['error']
    return f"{row['median_ms']:.1f} / {row['p95_ms']:.1f}"


OPS_ORDER = [
    ('connect_cold', 'TCP connect + auth'),
    ('ping', 'ping (SELECT 1), one round trip'),
    ('get_page', 'get_page payload, one indexed read'),
    ('get_page_serial', 'get_page shape, 5 serial reads'),
    ('get_page_overlapped', 'get_page shape, overlapped reads'),
    ('keyword_rare', 'keyword search, rare term'),
    ('keyword_common', 'keyword search, common term'),
    ('vector_search', 'vector search, HNSW, JIT off, ef_search 80'),
    ('hybrid_db', 'hybrid DB only (keyword + title + vector, then post-fusion)'),
    ('ingest_page', 'ingest one page + 3 chunks, then delete'),
    ('config_serial_6', '6 serial config reads'),
    ('config_batched', '1 batched config read'),
    ('vector_jit_on', 'vector search with JIT left on (local only)'),
    ('vector_ef_200', 'vector search ef_search 200 (local only)'),
    ('vector_seqscan', 'vector search with the HNSW index disabled (local only)'),
]


def render_report(payloads: list[dict]) -> str:
    setups = [p['setup'] for p in payloads]
    lines = [
        '# Synthetic brain timings',
        '',
        'Median / p95 milliseconds. Embedding API time is not included.',
        'The client and Postgres are on the same machine. `local+latency` adds',
        'about 45 ms RTT (Austin to us-east-2). `pooler` is PgBouncer in',
        'transaction mode. `pooler+latency` is that pooler behind the same RTT,',
        'which is the shape of a Supabase transaction pooler from Austin.',
        '',
        'Corpus: '
        + ', '.join(
            f"{p['setup']} {p.get('pages')} pages / {p.get('chunks')} chunks / dim {p.get('dim')} / {p.get('pg_version')} / pgvector {p.get('pgvector')}"
            for p in payloads
        ),
        '',
        '| Operation | ' + ' | '.join(setups) + ' |',
        '| --- | ' + ' | '.join('---:' for _ in setups) + ' |',
    ]
    for key, label in OPS_ORDER:
        if not any(key in p.get('ops', {}) for p in payloads):
            continue
        cells = ' | '.join(_cell(p.get('ops', {}), key) for p in payloads)
        lines.append(f'| {label} | {cells} |')
    lines.append('')
    lines.append('## Plans (local)')
    for p in payloads:
        if p['setup'] != 'local':
            continue
        lines.append('')
        lines.append(f"Vector index methods: {', '.join(p.get('vector_index') or ['none'])}.")
        for name, plan in (p.get('plans') or {}).items():
            lines.append(f'- `{name}`: {plan}')
    lines.append('')
    lines.append('A production `query` also waits on the embedding API before the vector')
    lines.append('statement. That wait is not in this table. Keyword and title search')
    lines.append('already run beside that call, so the database portion of a query is')
    lines.append('about `max(keyword, embed_api + vector)` plus one post-fusion wave.')
    lines.append('')
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='mode', required=True)

    syn = sub.add_parser('synthetic', help='seed (unless --no-seed) and time a local gbrain_bench database')
    syn.add_argument('--url', required=True)
    syn.add_argument('--setup', default='local')
    syn.add_argument('--pages', type=int, default=PAGES_DEFAULT)
    syn.add_argument('--dim', type=int, default=DIM_DEFAULT)
    syn.add_argument('--iters', type=int, default=21)
    syn.add_argument('--no-seed', action='store_true')
    syn.add_argument('--out', required=True)

    probe = sub.add_parser('probe', help='read-only timings against an existing database')
    probe.add_argument('--url', required=True)
    probe.add_argument('--iters', type=int, default=15)
    probe.add_argument('--out', required=True)

    seed_p = sub.add_parser('seed', help='create the synthetic corpus and return')
    seed_p.add_argument('--url', required=True)
    seed_p.add_argument('--pages', type=int, default=PAGES_DEFAULT)
    seed_p.add_argument('--dim', type=int, default=DIM_DEFAULT)

    rep = sub.add_parser('report', help='markdown table from synthetic JSON files')
    rep.add_argument('json_files', nargs='+')
    rep.add_argument('--out', required=True)

    args = parser.parse_args()
    if args.mode == 'seed':
        seed(args.url, args.pages, args.dim)
        return
    if args.mode == 'synthetic':
        if not args.no_seed:
            seed(args.url, args.pages, args.dim)
        payload = run_synthetic(args.url, args.setup, args.pages, args.dim, args.iters)
    elif args.mode == 'probe':
        payload = run_probe(args.url, args.iters)
    else:
        payloads = [json.load(open(path)) for path in args.json_files]
        text = render_report(payloads)
        with open(args.out, 'w') as fh:
            fh.write(text)
        sys.stdout.write(text)
        return
    with open(args.out, 'w') as fh:
        json.dump(payload, fh, indent=2)
        fh.write('\n')
    # stdout is timings only, never the URL
    if args.mode == 'probe':
        print(json.dumps({k: payload[k] for k in payload if k != 'ops'}, indent=2))
        print(json.dumps(payload['ops'], indent=2))
    else:
        print(json.dumps({'setup': payload['setup'], 'ops': payload['ops'], 'plans': payload['plans']}, indent=2))


if __name__ == '__main__':
    main()
