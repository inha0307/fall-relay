#!/usr/bin/env python3
"""TCP gate between the benchmark process and its server link (a real link loss on one device).

Listens on --listen and relays each connection to --target (the SSH reverse tunnel to the bridge). While the file
--cut exists the link is dead: new connections are accepted and then left without an answer until the client gives
up (as on a dead link), and connections that were open when the cut began are closed. Every state change and every
connection is logged as JSON lines to --log. No root is needed, and nothing outside this process is changed.
"""

import argparse
import json
import os
import socket
import threading
import time


def log(path, **data):
    with open(path, "a") as fh:
        fh.write(json.dumps({"wall": time.time(), **data}) + "\n")


def relay(src, dst, cut_flag):
    try:
        while True:
            data = src.recv(65536)
            if not data or cut_flag.is_set():
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listen", type=int, required=True)
    ap.add_argument("--target", type=int, required=True)
    ap.add_argument("--cut", required=True, help="the link is dead while this file exists")
    ap.add_argument("--log", required=True)
    ap.add_argument("--hold-s", type=float, default=240.0, help="how long a connection accepted during a cut is held")
    args = ap.parse_args()
    cut_flag = threading.Event()
    opened = []  # sockets of live relayed connections, closed when a cut begins
    lock = threading.Lock()

    def watcher():
        state = False
        while True:
            now = os.path.exists(args.cut)
            if now != state:
                state = now
                if now:
                    cut_flag.set()
                    with lock:
                        victims, opened[:] = list(opened), []
                    for s in victims:
                        try:
                            s.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    log(args.log, kind="cut_start", closed_connections=len(victims))
                else:
                    cut_flag.clear()
                    log(args.log, kind="cut_end")
            time.sleep(0.2)

    threading.Thread(target=watcher, daemon=True).start()

    def serve(client, address):
        started = time.time()
        if cut_flag.is_set():
            # Dead link: no answer at all until the client gives up (or the cut ends and we drop it).
            client.settimeout(0.5)
            deadline = time.monotonic() + args.hold_s
            while time.monotonic() < deadline:
                try:
                    if not client.recv(1):
                        break
                except socket.timeout:
                    if not cut_flag.is_set():
                        break
                except OSError:
                    break
            client.close()
            log(args.log, kind="held", port=address[1], seconds=round(time.time() - started, 3))
            return
        try:
            upstream = socket.create_connection(("127.0.0.1", args.target), timeout=5)
        except OSError as exc:
            client.close()
            log(args.log, kind="upstream_failed", error=str(exc))
            return
        with lock:
            opened.extend([client, upstream])
        t = threading.Thread(target=relay, args=(upstream, client, cut_flag), daemon=True)
        t.start()
        relay(client, upstream, cut_flag)
        t.join(timeout=5)
        for s in (client, upstream):
            try:
                s.close()
            except OSError:
                pass
        with lock:
            for s in (client, upstream):
                if s in opened:
                    opened.remove(s)
        log(args.log, kind="relayed", port=address[1], seconds=round(time.time() - started, 3))

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", args.listen))
    server.listen(64)
    log(args.log, kind="listening", listen=args.listen, target=args.target, pid=os.getpid())
    while True:
        client, address = server.accept()
        threading.Thread(target=serve, args=(client, address), daemon=True).start()


if __name__ == "__main__":
    main()
