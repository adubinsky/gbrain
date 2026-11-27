#!/usr/bin/env python3
"""TCP proxy that adds a fixed RTT.

Each direction delays the first byte of a burst by RTT/2, then forwards
further bytes that arrive within 2ms without another delay. One Postgres
query is one burst each way, so a query pays about one RTT.

Usage: latency_proxy.py --listen 127.0.0.1:15432 --upstream 127.0.0.1:5432 --rtt-ms 45
"""
from __future__ import annotations

import argparse
import asyncio


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, delay_s: float) -> None:
    try:
        while True:
            data = await reader.read(1 << 20)
            if not data:
                break
            if delay_s:
                await asyncio.sleep(delay_s)
            writer.write(data)
            await writer.drain()
            while True:
                try:
                    more = await asyncio.wait_for(reader.read(1 << 20), timeout=0.002)
                except asyncio.TimeoutError:
                    break
                if not more:
                    writer.close()
                    return
                writer.write(more)
                await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter, upstream: str, delay_s: float) -> None:
    host, port_s = upstream.rsplit(':', 1)
    try:
        up_r, up_w = await asyncio.open_connection(host, int(port_s))
    except Exception:
        client_w.close()
        return
    await asyncio.gather(_pipe(client_r, up_w, delay_s), _pipe(up_r, client_w, delay_s))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', default='127.0.0.1:15432')
    parser.add_argument('--upstream', required=True)
    parser.add_argument('--rtt-ms', type=float, default=45.0)
    args = parser.parse_args()
    host, port_s = args.listen.rsplit(':', 1)
    delay_s = max(args.rtt_ms, 0) / 2000.0

    async def serve() -> None:
        server = await asyncio.start_server(
            lambda r, w: _handle(r, w, args.upstream, delay_s), host, int(port_s))
        async with server:
            await server.serve_forever()

    asyncio.run(serve())


if __name__ == '__main__':
    main()
