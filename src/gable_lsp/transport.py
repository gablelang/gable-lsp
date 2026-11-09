"""Transports: standard input/output (default) and TCP sockets.

Both use the same ``Content-Length`` framing defined by the LSP. stdio is what
every editor launches by default (Neovim, Helix, VS Code, Sublime Text); TCP
is useful for debugging a session, for editors that only speak sockets, and
for attaching to a server that a build system already started::

    gable-lsp --tcp --port 9257

Exactly one client is served per process. When the client disconnects the
process exits; this keeps lifecycle and shutdown semantics identical to the
stdio transport (one editor <-> one server).
"""

from __future__ import annotations

import socket
import sys
from typing import Optional

from .log import get_logger
from .rpc import Endpoint
from .server import Server
from .settings import Settings

logger = get_logger(__name__)


def serve_stdio(settings: Settings) -> int:
    """Serve one client over the process's stdin/stdout (binary mode).

    Protocol frames are the only bytes ever written to stdout; logs go to
    stderr or ``--log-file``.
    """

    endpoint = Endpoint(sys.stdin.buffer, sys.stdout.buffer)
    try:
        return Server(endpoint, settings).run()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 1


def serve_tcp(
    settings: Settings,
    *,
    host: str = "0.0.0.0",
    port: int = 0,
    accept_forever: bool = False,
) -> int:
    """Listen on ``host:port`` and serve one LSP client.

    ``port=0`` asks the OS for a free port; the chosen port is logged to
    stderr so a wrapper script can discover it. ``accept_forever`` keeps the
    listening socket open and serves clients one after another (each in a
    fresh :class:`Server`), which is handy for editors that reconnect.
    """

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
        listener.listen(1)
    except OSError as exc:
        logger.error("cannot listen on %s:%s: %s", host, port, exc)
        return 1
    bound_host, bound_port = listener.getsockname()[:2]
    logger.info("listening for LSP clients on %s:%s", bound_host, bound_port)

    exit_code = 0
    try:
        while True:
            connection, peer = listener.accept()
            logger.info("client connected from %s:%s", *peer[:2])
            with connection:
                try:
                    code = _serve_connection(connection, settings)
                except OSError as exc:
                    logger.warning("connection error: %s", exc)
                    code = 1
                logger.info("session finished with exit code %d", code)
                if not accept_forever:
                    return code
                exit_code = code
    finally:
        listener.close()
    return exit_code


def _serve_connection(connection: socket.socket, settings: Settings) -> int:
    # Buffered binary file objects give readline()/read(n) with the exact
    # semantics MessageReader expects, and keep socket writes atomic per frame
    # thanks to the writer's lock.
    reader = connection.makefile("rb")
    writer = connection.makefile("wb")
    try:
        return Server(Endpoint(reader, writer), settings).run()
    finally:
        try:
            reader.close()
        except OSError:
            pass
        try:
            writer.close()
        except OSError:
            pass
