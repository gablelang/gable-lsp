"""JSON-RPC 2.0 transport for LSP.

Implements the ``Content-Length`` framed message exchange used by the Language
Server Protocol over stdio and sockets. Framing is deliberately strict on
write (``Content-Length`` only, binary-safe body) and liberal on read
(unknown headers are ignored, the body is decoded as UTF-8 JSON).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import IO, Any, Dict, Optional

# A message larger than this is treated as a framing bug or a hostile peer.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_NOT_INITIALIZED = -32002
REQUEST_CANCELLED = -32800


class RpcError(Exception):
    """An error that maps onto a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None, fatal: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        # A fatal error means the framing itself is broken and the stream can
        # no longer be resynchronised; a body-level JSON error is recoverable.
        self.fatal = fatal


@dataclass
class Message:
    """A decoded JSON-RPC message (request, notification or response)."""

    payload: Dict[str, Any]

    @property
    def id(self) -> Any:
        return self.payload.get("id")

    @property
    def method(self) -> Optional[str]:
        return self.payload.get("method")

    @property
    def params(self) -> Any:
        params = self.payload.get("params")
        return {} if params is None else params

    @property
    def is_request(self) -> bool:
        return self.method is not None and "id" in self.payload

    @property
    def is_notification(self) -> bool:
        return self.method is not None and "id" not in self.payload


def encode_message(payload: Dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


class MessageReader:
    """Reads framed messages from a binary stream."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream

    def _read_line(self) -> Optional[bytes]:
        line = self._stream.readline()
        if not line:
            return None
        return line.rstrip(b"\r\n")

    def read_message(self) -> Optional[Dict[str, Any]]:
        """Read one message. Returns ``None`` on clean end-of-stream."""

        content_length: Optional[int] = None
        while True:
            line = self._read_line()
            if line is None:
                if content_length is None:
                    return None  # EOF between messages: clean shutdown signal.
                raise RpcError(PARSE_ERROR, "unexpected end of stream in header block", fatal=True)
            if line == b"":
                break  # end of headers
            name, sep, value = line.partition(b":")
            if not sep:
                continue  # tolerate junk header lines
            if name.strip().lower() == b"content-length":
                try:
                    content_length = int(value.strip())
                except ValueError as exc:
                    raise RpcError(PARSE_ERROR, "invalid Content-Length header", fatal=True) from exc
        if content_length is None:
            raise RpcError(PARSE_ERROR, "missing Content-Length header", fatal=True)
        if content_length < 0 or content_length > MAX_MESSAGE_BYTES:
            raise RpcError(PARSE_ERROR, f"Content-Length {content_length} out of bounds", fatal=True)
        body = self._stream.read(content_length)
        if body is None or len(body) != content_length:
            raise RpcError(PARSE_ERROR, "unexpected end of stream in message body", fatal=True)
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RpcError(PARSE_ERROR, f"message body is not valid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise RpcError(INVALID_REQUEST, "JSON-RPC payload must be an object")
        return decoded


class MessageWriter:
    """Writes framed messages; safe to call from worker threads."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write_message(self, payload: Dict[str, Any]) -> bool:
        framed = encode_message(payload)
        with self._lock:
            try:
                self._stream.write(framed)
                self._stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                return False
        return True


class Endpoint:
    """Convenience wrapper bundling a reader and a writer."""

    def __init__(self, reader_stream: IO[bytes], writer_stream: IO[bytes]) -> None:
        self.reader = MessageReader(reader_stream)
        self.writer = MessageWriter(writer_stream)
