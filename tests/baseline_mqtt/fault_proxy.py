"""TCP proxy between the device and the broker that drops the broker's first PUBACK and closes the connection,
so the device never learns that the broker accepted the message (the lost-confirmation fault of test A1).
Later connections pass through unchanged.  Usage: fault_proxy.py LISTEN_PORT UPSTREAM_PORT"""

import asyncio
import json
import sys
import time

LISTEN, UPSTREAM = int(sys.argv[1]), int(sys.argv[2])
state = {'dropped': False}


def log(**kw):
    print(json.dumps({'t': round(time.time(), 3), 'who': 'proxy', **kw}), flush=True)


async def read_packet(reader):
    head = await reader.readexactly(1)
    length, mult, raw = 0, 1, b''
    while True:
        b = await reader.readexactly(1)
        raw += b
        length += (b[0] & 0x7F) * mult
        if not b[0] & 0x80:
            break
        mult *= 128
    body = await reader.readexactly(length)
    return head + raw + body, head[0] >> 4


async def handle(c_reader, c_writer):
    u_reader, u_writer = await asyncio.open_connection('127.0.0.1', UPSTREAM)

    async def up():
        try:
            while data := await c_reader.read(65536):
                u_writer.write(data)
                await u_writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            u_writer.close()

    async def down():
        try:
            while True:
                packet, ptype = await read_packet(u_reader)
                if ptype == 4 and not state['dropped']:
                    state['dropped'] = True
                    log(event='puback_dropped_connection_closed')
                    c_writer.close()
                    u_writer.close()
                    return
                c_writer.write(packet)
                await c_writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            c_writer.close()

    await asyncio.gather(up(), down())


async def main():
    server = await asyncio.start_server(handle, '127.0.0.1', LISTEN)
    log(event='listening', port=LISTEN, upstream=UPSTREAM)
    async with server:
        await server.serve_forever()


asyncio.run(main())
