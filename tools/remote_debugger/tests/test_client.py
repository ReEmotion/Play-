import base64
import json
import socket
import struct
import sys
import threading
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playdebug import Client, DebugError, RPC_METHODS, MESSAGE_LIMIT


def frame(value):
    body = json.dumps(value).encode()
    return struct.pack('!I', len(body)) + body


class Server:
    def __init__(self, handler):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.handler = handler
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self):
        conn, _ = self.listener.accept()
        with conn:
            stream = conn.makefile('rb')
            while True:
                header = stream.read(4)
                if len(header) != 4:
                    break
                request = json.loads(stream.read(struct.unpack('!I', header)[0]))
                if request['method'] == 'hello':
                    # Fragment every byte of the handshake.
                    for byte in frame({'type': 'response', 'id': request['id'], 'result': {'version': 1, 'generation': '0x01'}}):
                        conn.sendall(bytes([byte]))
                elif not self.handler(conn, request):
                    break
        self.listener.close()

    def close(self):
        self.thread.join(3)
        assert not self.thread.is_alive()


class ClientTests(unittest.TestCase):
    def test_fragmented_handshake_coalesced_events_and_response(self):
        def handler(conn, r):
            conn.sendall(frame({'type': 'event', 'event': 'stopped', 'data': {'cpu': 'ee'}}) +
                         frame({'type': 'response', 'id': r['id'], 'result': {'state': 'paused'}}))
            return True
        server = Server(handler)
        with Client(port=server.port) as c:
            self.assertEqual(c.status()['state'], 'paused')
            self.assertEqual(c.wait_event('stopped')['data']['cpu'], 'ee')
        server.close()

    def test_structured_error_and_no_retry(self):
        calls = []
        def handler(conn, r):
            calls.append(r['method'])
            conn.sendall(frame({'type': 'response', 'id': r['id'], 'error': {'code': 'TARGET_RUNNING', 'message': 'Pause first'}}))
            return True
        server = Server(handler)
        with Client(port=server.port) as c:
            with self.assertRaises(DebugError) as error:
                c.registers_write(name='pc', value='0x1000')
            self.assertEqual(error.exception.code, 'TARGET_RUNNING')
        self.assertEqual(calls, ['registers.write'])
        server.close()

    def test_oversize_server_and_disconnect(self):
        def handler(conn, r):
            conn.sendall(struct.pack('!I', MESSAGE_LIMIT + 1))
            return False
        server = Server(handler)
        with Client(port=server.port) as c:
            with self.assertRaises(DebugError) as error:
                c.status()
            self.assertEqual(error.exception.code, 'INVALID_MESSAGE')
        server.close()

    def test_artifact_chunks_binary_roundtrip(self):
        content = bytes(range(256)) * 2049
        def handler(conn, r):
            p = r['params']
            chunk = content[p['offset']:p['offset'] + p['size']]
            conn.sendall(frame({'type': 'response', 'id': r['id'], 'result': {'data': base64.b64encode(chunk).decode(), 'size': len(content), 'eof': p['offset'] + len(chunk) == len(content)}}))
            return True
        server = Server(handler)
        with Client(port=server.port) as c:
            self.assertEqual(c.download('test'), content)
        server.close()

    def test_timeout_cancels_queued_without_retry(self):
        calls = []
        def handler(conn, r):
            calls.append(r['method'])
            if r['method'] == 'request.cancel':
                conn.sendall(frame({'type': 'response', 'id': r['id'], 'result': {'canceled': True}}))
            return True
        server = Server(handler)
        with Client(port=server.port) as c:
            with self.assertRaises(DebugError) as error:
                c.registers_write(name='pc', value='0x1000', timeout=.05)
            self.assertTrue(error.exception.details['canceled_before_execution'])
        self.assertEqual(calls, ['registers.write', 'request.cancel'])
        server.close()

    def test_all_methods_have_sdk_entrypoints(self):
        for method in RPC_METHODS:
            self.assertTrue(callable(getattr(Client, method.replace('.', '_'))), method)

    def test_cancellation_before_send_and_during_request(self):
        calls, received, cancel = [], threading.Event(), threading.Event()
        def handler(conn, r):
            calls.append(r['method'])
            if r['method'] == 'request.cancel':
                conn.sendall(frame({'type': 'response', 'id': r['id'], 'result': {'canceled': True}}))
            else:
                received.set()
            return True
        server = Server(handler)
        with Client(port=server.port) as c:
            cancel.set()
            with c.cancellation(cancel), self.assertRaises(DebugError) as error:
                c.registers_write(name='pc', value='0x1000')
            self.assertEqual(error.exception.code, 'CANCELED')
            self.assertEqual(calls, [])
            cancel.clear()
            def cancel_when_received():
                if received.wait(2):
                    cancel.set()
            thread = threading.Thread(target=cancel_when_received)
            thread.start()
            with c.cancellation(cancel), self.assertRaises(DebugError) as error:
                c.registers_write(name='pc', value='0x1000', timeout=10)
            thread.join(3)
            self.assertEqual(error.exception.code, 'CANCELED')
            self.assertTrue(error.exception.details['canceled_before_execution'])
        server.close()
        self.assertEqual(calls, ['registers.write', 'request.cancel'])


if __name__ == '__main__':
    unittest.main()
