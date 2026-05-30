#!/usr/bin/env python3
from __future__ import annotations

import argparse
import selectors
import socket
import threading


def _pump(client: socket.socket, upstream: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    sockets = (client, upstream)
    try:
        while True:
            events = selector.select()
            if not events:
                continue
            for key, _ in events:
                source = key.fileobj
                target = key.data
                chunk = source.recv(65536)
                if not chunk:
                    return
                target.sendall(chunk)
    finally:
        selector.close()
        for handle in sockets:
            try:
                handle.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            handle.close()


def _serve_connection(
    client: socket.socket,
    target_host: str,
    target_port: int,
) -> None:
    upstream = socket.create_connection((target_host, target_port))
    _pump(client, upstream)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Expose a bridge-only TCP proxy for host-local Ollama."
    )
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    args = parser.parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.listen_host, args.listen_port))
    server.listen()

    try:
        while True:
            client, _ = server.accept()
            worker = threading.Thread(
                target=_serve_connection,
                args=(client, args.target_host, args.target_port),
                daemon=True,
            )
            worker.start()
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
