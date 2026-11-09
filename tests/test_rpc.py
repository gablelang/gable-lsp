import io
import json
import unittest

from support import ensure_import_path

ensure_import_path()

from gable_lsp.rpc import (  # noqa: E402
    MAX_MESSAGE_BYTES,
    PARSE_ERROR,
    Message,
    MessageReader,
    MessageWriter,
    RpcError,
    encode_message,
)


def framed(*payloads):
    return b"".join(encode_message(p) for p in payloads)


class FramingTest(unittest.TestCase):
    def test_roundtrip(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"x": "é😀"}}
        stream = io.BytesIO(framed(payload))
        self.assertEqual(MessageReader(stream).read_message(), payload)

    def test_content_length_is_byte_length(self):
        payload = {"text": "é😀"}
        data = encode_message(payload)
        header, _, body = data.partition(b"\r\n\r\n")
        self.assertEqual(int(header.split(b":")[1]), len(body))
        # UTF-8 body bytes, not Python character counts.
        self.assertEqual(len(body), len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")))
        # Two characters, but 6 UTF-8 bytes plus quotes: 17 bytes, not 12.
        self.assertEqual(len(body), 17)

    def test_header_names_are_case_insensitive(self):
        body = json.dumps({"a": 1}).encode()
        stream = io.BytesIO(b"content-length: %d\r\n\r\n" % len(body) + body)
        self.assertEqual(MessageReader(stream).read_message(), {"a": 1})

    def test_extra_headers_ignored(self):
        body = json.dumps({"a": 1}).encode()
        stream = io.BytesIO(
            b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n"
            b"Content-Length: %d\r\n\r\n" % len(body) + body
        )
        self.assertEqual(MessageReader(stream).read_message(), {"a": 1})

    def test_clean_eof_returns_none(self):
        self.assertIsNone(MessageReader(io.BytesIO(b"")).read_message())

    def test_eof_after_headers_is_fatal(self):
        reader = MessageReader(io.BytesIO(b"Content-Length: 10\r\n\r\nshort"))
        with self.assertRaises(RpcError) as raised:
            reader.read_message()
        self.assertTrue(raised.exception.fatal)

    def test_invalid_json_body_is_recoverable(self):
        body = b"{not json"
        stream = io.BytesIO(
            b"Content-Length: %d\r\n\r\n" % len(body) + body + framed({"ok": True})
        )
        reader = MessageReader(stream)
        with self.assertRaises(RpcError) as raised:
            reader.read_message()
        self.assertEqual(raised.exception.code, PARSE_ERROR)
        self.assertFalse(raised.exception.fatal)
        self.assertEqual(reader.read_message(), {"ok": True})

    def test_missing_content_length_is_fatal(self):
        with self.assertRaises(RpcError) as raised:
            MessageReader(io.BytesIO(b"Content-Type: x\r\n\r\n")).read_message()
        self.assertTrue(raised.exception.fatal)

    def test_non_object_payload_is_invalid_request(self):
        body = b"[1, 2, 3]"
        stream = io.BytesIO(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        with self.assertRaises(RpcError):
            MessageReader(stream).read_message()

    def test_oversized_content_length_rejected(self):
        header = b"Content-Length: %d\r\n\r\n" % (MAX_MESSAGE_BYTES + 1)
        with self.assertRaises(RpcError) as raised:
            MessageReader(io.BytesIO(header)).read_message()
        self.assertTrue(raised.exception.fatal)

    def test_negative_content_length_rejected(self):
        with self.assertRaises(RpcError):
            MessageReader(io.BytesIO(b"Content-Length: -4\r\n\r\n")).read_message()

    def test_multiple_messages_in_one_stream(self):
        stream = io.BytesIO(framed({"id": 1}, {"id": 2}, {"id": 3}))
        reader = MessageReader(stream)
        self.assertEqual([reader.read_message() for _ in range(3)], [{"id": 1}, {"id": 2}, {"id": 3}])
        self.assertIsNone(reader.read_message())


class WriterTest(unittest.TestCase):
    def test_write_and_read_back(self):
        buffer = io.BytesIO()
        writer = MessageWriter(buffer)
        self.assertTrue(writer.write_message({"jsonrpc": "2.0", "method": "x"}))
        buffer.seek(0)
        self.assertEqual(MessageReader(buffer).read_message(), {"jsonrpc": "2.0", "method": "x"})

    def test_broken_pipe_reports_false(self):
        class Broken(io.BytesIO):
            def write(self, data):
                raise BrokenPipeError

            def flush(self):
                raise BrokenPipeError

        self.assertFalse(MessageWriter(Broken()).write_message({"a": 1}))


class MessageTest(unittest.TestCase):
    def test_classification(self):
        request = Message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertTrue(request.is_request)
        self.assertFalse(request.is_notification)
        self.assertEqual(request.params, {})

        notification = Message({"jsonrpc": "2.0", "method": "exit"})
        self.assertTrue(notification.is_notification)
        self.assertFalse(notification.is_request)

        response = Message({"jsonrpc": "2.0", "id": 1, "result": None})
        self.assertFalse(response.is_request)
        self.assertFalse(response.is_notification)


if __name__ == "__main__":
    unittest.main()
