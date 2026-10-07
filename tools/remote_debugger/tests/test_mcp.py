"""Optional MCP tests: run with the interpreter containing requirements-mcp.txt."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp import Client as MCPClient, MCPError, StdioServerParameters
from playdebug import RPC_METHODS
from test_client import Server, frame

CLI = Path(__file__).resolve().parents[1] / 'playdebug.py'
MCP_COMMAND = json.loads(os.environ.get('PLAY_MCP_TEST_COMMAND', 'null'))
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0S8AAAAASUVORK5CYII=')


def response(conn, request, value=None, error=None):
    conn.sendall(frame({'type': 'response', 'id': request['id'],
                        'error' if error else 'result': error or value}))


class MCPTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def session(self, handler):
        server = Server(handler)
        with tempfile.TemporaryDirectory(prefix='play-mcp-test-') as directory:
            options = ['--port', str(server.port), '--output-dir', directory]
            command = MCP_COMMAND + options if MCP_COMMAND else [sys.executable, str(CLI), *options, 'mcp']
            params = StdioServerParameters(command=command[0], args=command[1:])
            try:
                async with MCPClient(params, read_timeout_seconds=10) as client:
                    yield client, Path(directory)
            finally:
                server.close()  # stdin EOF must close the persistent TCP session.

    async def call(self, client, name, args=None):
        value = await client.call_tool(name, args)
        self.assertFalse(value.is_error, value)
        return value.structured_content['result']

    async def test_discovery_cli_persistent_connection_and_error(self):
        state, methods = ['paused'], []
        def handler(conn, request):
            method = request['method']
            methods.append(method)
            if method == 'continue':
                state[0] = 'running'
            if method == 'pause':
                state[0] = 'paused'
            if method == 'registers.write':
                response(conn, request, error={'code': 'TARGET_RUNNING', 'message': 'Pause first'})
            else:
                response(conn, request, {'state': state[0]})
            return True
        async with self.session(handler) as (client, _):
            tools = (await client.list_tools()).tools
            names = {tool.name for tool in tools}
            self.assertTrue({'play_' + name.replace('.', '_') for name in RPC_METHODS} <= names)
            guide = await client.read_resource('playdebug://guide')
            self.assertIn('persistent', guide.contents[0].text)
            missing = await client.call_tool('play_status')
            self.assertEqual(missing.structured_content['error']['code'], 'NOT_CONNECTED')
            self.assertEqual(methods, [])  # Discovery never pauses/connects to the emulator.
            self.assertFalse((await self.call(client, 'play_connect'))['reused'])
            await self.call(client, 'play_cli', {'command': 'continue'})
            self.assertEqual((await self.call(client, 'play_status'))['state'], 'running')
            reused = await self.call(client, 'play_connect')
            self.assertTrue(reused['reused'])
            self.assertEqual(reused['status']['state'], 'running')  # No second hello.
            error = await client.call_tool('play_registers_write', {'params': {'name': 'pc', 'value': '0x1000'}})
            self.assertTrue(error.is_error)
            self.assertEqual(error.structured_content['error']['code'], 'TARGET_RUNNING')
            await self.call(client, 'play_disconnect')
            error = await client.call_tool('play_status')
            self.assertEqual(error.structured_content['error']['code'], 'NOT_CONNECTED')
        self.assertEqual(state[0], 'running')
        self.assertEqual(methods.count('registers.write'), 1)

    async def test_artifact_resources_cli_export_and_path_validation(self):
        payload = json.dumps({'format': 'play-gs-state-v1', 'padding': 'x' * 530000}).encode()
        artifacts = {'gs': payload, 'png': PNG}
        released, offsets = [], []
        def handler(conn, request):
            params, method = request['params'], request['method']
            if method == 'gs.dump':
                value = {'artifact': 'gs', 'size': len(payload)}
            elif method == 'screenshot':
                value = {'artifact': 'png', 'size': len(PNG)}
            elif method == 'artifact.read':
                content = artifacts[params['artifact']]
                offsets.append(params['offset'])
                part = content[params['offset']:params['offset'] + params['size']]
                value = {'data': base64.b64encode(part).decode(), 'size': len(content),
                         'eof': params['offset'] + len(part) == len(content)}
            elif method == 'artifact.release':
                released.append(params['artifact'])
                value = {}
            else:
                value = {'state': 'paused'}
            response(conn, request, value)
            return True
        async with self.session(handler) as (client, root):
            await self.call(client, 'play_connect')
            dump = await self.call(client, 'play_gs_dump_save')
            self.assertEqual(Path(dump['path']).parent, root)
            self.assertEqual(Path(dump['path']).read_bytes(), payload)
            resource = await client.read_resource(dump['uri'])
            self.assertEqual(resource.contents[0].text.encode(), payload)
            screenshot = await client.call_tool('play_screenshot_save')
            self.assertFalse(screenshot.is_error)
            self.assertTrue(any(content.type == 'image' for content in screenshot.content))
            item = screenshot.structured_content['result']
            resource = await client.read_resource(item['uri'])
            self.assertEqual(base64.b64decode(resource.contents[0].blob), PNG)
            cli_dump = await self.call(client, 'play_cli', {'command': 'gs-dump', 'arguments': ['cli.json']})
            self.assertEqual(Path(cli_dump['path']).read_bytes(), payload)
            bad = await client.call_tool('play_artifact_save', {'artifact': 'gs', 'filename': '../escape.json'})
            self.assertTrue(bad.is_error)
            self.assertEqual(released, ['gs', 'png', 'gs'])  # Invalid destination does not consume the ID.
            await self.call(client, 'play_export_release', {'name': dump['name']})
            self.assertFalse(Path(dump['path']).exists())
            with self.assertRaises(MCPError):
                await client.read_resource(dump['uri'], cache_mode='bypass')
        self.assertIn(262144, offsets)
        self.assertIn(524288, offsets)

    async def test_mcp_cancellation_cancels_rpc_and_unsent_mutation(self):
        methods, received, canceled = [], threading.Event(), threading.Event()
        def handler(conn, request):
            methods.append(request['method'])
            if request['method'] == 'search.word':
                received.set()  # Simulate a queued remote operation, no response until canceled.
            elif request['method'] == 'request.cancel':
                response(conn, request, {'canceled': True})
                canceled.set()
            else:
                response(conn, request, {'state': 'paused'})
            return True
        async with self.session(handler) as (client, _):
            await self.call(client, 'play_connect')
            running = asyncio.create_task(client.call_tool('play_search_word', {'timeout': 30}))
            self.assertTrue(await asyncio.to_thread(received.wait, 3))
            queued = asyncio.create_task(client.call_tool('play_registers_write', {'params': {'name': 'pc', 'value': '0x1000'}}))
            await asyncio.sleep(.1)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            self.assertTrue(await asyncio.to_thread(canceled.wait, 3))
            await self.call(client, 'play_status')
        self.assertNotIn('registers.write', methods)
        self.assertEqual(methods.count('search.word'), 1)
        self.assertEqual(methods.count('request.cancel'), 1)

    async def test_event_wait_does_not_block_control(self):
        def handler(conn, request):
            if request['method'] == 'pause':
                conn.sendall(frame({'type': 'event', 'event': 'stopped', 'data': {'reason': 'pause'}}))
            response(conn, request, {'state': 'paused'})
            return True
        async with self.session(handler) as (client, _):
            await self.call(client, 'play_connect')
            waiting = asyncio.create_task(self.call(client, 'play_wait_event', {'name': 'stopped', 'timeout': 3}))
            await asyncio.sleep(.05)
            await self.call(client, 'play_pause')
            self.assertEqual((await waiting)['data']['reason'], 'pause')


class LegacyStdioTests(unittest.TestCase):
    def test_2025_client_and_clean_stdio(self):
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25',
             'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        ]
        # Keep stdin open until both responses arrive; EOF also tests normal shutdown.
        command = MCP_COMMAND or [sys.executable, str(CLI), 'mcp']
        with subprocess.Popen(command, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            for message in messages:
                process.stdin.write(json.dumps(message) + '\n')
                process.stdin.flush()
            first, second = json.loads(process.stdout.readline()), json.loads(process.stdout.readline())
            self.assertEqual(first['result']['protocolVersion'], '2025-11-25')
            self.assertTrue(any(tool['name'] == 'play_gs_dump_save' for tool in second['result']['tools']))
            process.stdin.close()
            process.wait(timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr.read())


if __name__ == '__main__':
    unittest.main()
