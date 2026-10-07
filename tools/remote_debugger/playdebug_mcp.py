"""Play! debugger MCP stdio adapter; optional official MCP SDK dependency."""
import argparse
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
import threading
from typing import Any, Literal
import uuid

import anyio
from mcp import types
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError

from playdebug import Client, DebugError, RPC_METHODS, execute

CPU = Literal['ee', 'iop', 'vu0', 'vu1']
MAX_EXPORT_BYTES = 128 * 1024 * 1024
MAX_EXPORTS = 16
READ_ONLY = set(('status registers.read memory.read breakpoints.list disassemble effective_address '
                 'callstack expression.evaluate search.callers search.word search.half '
                 'elf.header elf.programs elf.sections elf.symbols modules kernel.types kernel.objects '
                 'tags.list gs.state logs.read task.status artifact.read '
                 'replay.packets replay.writes replay.kicks replay.context replay.vu1.registers.read '
                 'replay.vu1.memory.read replay.vu1.disassemble replay.vu1.effective_address replay.vu1.gif').split())
DESTRUCTIVE = set(('registers.write memory.write assemble.jal tags.remove tags.load '
                   'replay.vu1.registers.write replay.vu1.memory.write').split())
PARAM_HINTS = {
    'status': 'No params; returns state, target generation, program, CPU PCs and cumulative frames.',
    'pause': 'No params; pause the whole VM and report the confirmed stop.',
    'continue': 'No params; explicitly resume the VM.',
    'registers.read': 'cpu=ee|iop|vu0|vu1.',
    'registers.write': 'cpu, name, value (hex string).',
    'memory.read': 'cpu, address (hex), size (bytes), space=data|instruction.',
    'memory.write': 'cpu, address (hex), data (Base64), space=data|instruction.',
    'disassemble': 'cpu, address (hex), count.',
    'step': 'cpu=ee|iop|vu0|vu1; requires paused target.',
    'gs.state': 'Returns current live GS registers/contexts/vertices; requires pause; no frame advance.',
    'gs.dump': 'Returns a live GS JSON artifact including 4 MiB RAM; requires pause; save with play_artifact_save.',
    'screenshot': 'Returns a PNG artifact; save with play_artifact_save.',
    'frame.capture': 'Returns a task ID without resuming. Poll play_task_status or wait for task_completed; then save its artifact.',
    'replay.open': 'artifact (uploaded frame dump ID), renderer=opengl|vulkan; returns session ID.',
    'replay.context': 'session, context=0|1; reads isolated replay state.',
    'replay.image': 'session, context=0|1, kind=framebuffer|depthbuffer|texture, mip, channel=raw|clut|alpha; returns artifact.',
    'task.status': 'task ID.', 'task.cancel': 'task ID.',
    'artifact.read': 'artifact ID, offset, size<=262144; returns Base64 chunk.',
    'effective_address': 'cpu, optional address (hex; default PC); resolves the memory operand.',
    'callstack': 'cpu; returns stack entries for the paused target.',
    'expression.evaluate': 'cpu, expression (e.g. T0 + 4); uses the existing register expression evaluator.',
    'assemble.jal': 'cpu=ee|iop, address (hex), target (hex); patch a JAL and invalidate JIT.',
    'kernel.types': 'cpu=ee|iop; returns supported object type IDs.',
    'kernel.objects': 'cpu=ee|iop, type (from kernel.types), offset, limit.',
    'tags.set': 'kind=functions|variables|comments, address (hex), text.',
    'tags.remove': 'kind=functions|variables|comments, address (hex).',
    'tags.list': 'kind=functions|variables|comments, offset, limit.',
    'tags.save': 'No params; save current program address tags.',
    'tags.load': 'No params; load current program address tags.',
    'gs.draw': 'Optional enabled (boolean); query or change GS drawing.',
    'logs.read': 'channel=stdout|stderr, offset, size; returns Base64 data and next_offset.',
    'request.cancel': 'id of a remote RPC (see TIMEOUT/CANCELED details.request_id); cancel if not started.',
    'artifact.begin': 'size (bytes), mime; reserve an artifact for frame dump upload.',
    'artifact.append': 'artifact ID, offset, data (Base64); sequential chunks up to 256 KiB.',
    'artifact.release': 'artifact ID; release its storage.',
    'replay.close': 'session ID; destroy the isolated replay.',
    'replay.export': 'session ID; return an artifact in the native frame dump format.',
    'replay.packets': 'session ID, offset, limit; list packet indices, paths and command locations.',
    'replay.writes': 'session ID, packet index, offset, limit; list register writes and descriptions.',
    'replay.kicks': 'session ID, offset, limit; list drawing kicks, command locations and vertices.',
    'replay.seek': 'session ID, command; replay up to the selected register write.',
    'replay.next_kick': 'session ID; move to the next drawing kick.',
    'replay.previous_kick': 'session ID; move to the previous drawing kick.',
    'replay.options': 'session ID; optional display=raw|448p|448i and alpha/depth/blend booleans.',
}
for method in ('breakpoints.add', 'breakpoints.remove'):
    PARAM_HINTS[method] = 'cpu, address (hex); update a breakpoint and invalidate JIT.'
PARAM_HINTS['breakpoints.list'] = 'cpu, offset, limit; list breakpoint addresses.'
for method in ('search.word', 'search.half', 'search.callers'):
    PARAM_HINTS[method] = 'cpu, begin/end (hex, end exclusive; span<=256 KiB), value (hex), offset, limit.'
for method in ('analysis.run', 'analysis.functions', 'analysis.vu'):
    PARAM_HINTS[method] = 'cpu, begin/end (hex, end exclusive); VU ranges are 8-byte aligned.'
for method in ('elf.header', 'elf.programs', 'elf.sections', 'elf.symbols', 'modules'):
    PARAM_HINTS[method] = 'cpu=ee|iop; list results accept offset/limit; inspect the already-loaded program.'
for method in RPC_METHODS:
    if method.startswith('replay.vu1.'):
        live_method = method.removeprefix('replay.vu1.')
        PARAM_HINTS[method] = 'session ID; operate on captured VU1 in isolated replay. ' + PARAM_HINTS.get(live_method, 'No other required params.')
GUIDE = '''Play! remote debugger MCP
Call play_connect once; hello pauses the target. Calls share one persistent control connection.
play_continue resumes; play_pause and play_step stop it. Disconnect preserves execution state.
Do not automatically retry a mutation after TIMEOUT/CANCELED or reconnect automatically.
Addresses and 64/128-bit registers are hex strings; binary data is Base64. CPU defaults to ee.
Most register/memory/analysis/live GS operations require pause; TARGET_RUNNING is explicit.
Every protocol method is a play_<method_with_underscores> tool taking a params object.
play_rpc invokes any RPC; play_cli accepts existing CLI commands and argument strings.
Use play_gs_dump_save / play_screenshot_save or play_artifact_save for files; they return a
path and readable MCP resource URI. play_wait_event does not block other control calls.
Frame capture does not resume implicitly. Poll task.status; cancellation only guarantees
that an operation which has not started will not execute. Started mutations may complete.
Replay sessions and remote task/artifact IDs belong to this connection and target generation.
'''


def result(value, error=False, extra=None):
    wrapped = {'error' if error else 'result': value}
    return types.CallToolResult(content=[types.TextContent(text=json.dumps(wrapped, ensure_ascii=False))] + (extra or []),
                                structured_content=wrapped, is_error=error)


def failure(error):
    return result({'code': getattr(error, 'code', 'CLIENT_ERROR'), 'message': str(error),
                   'details': getattr(error, 'details', {})}, error=True)


class Bridge:
    def __init__(self, host, port, timeout, output_dir):
        self.host, self.port, self.timeout = host, port, timeout
        self.output_dir = Path(output_dir).resolve()
        self.client = None
        self._operation_lock = threading.RLock()
        self._guard = threading.RLock()
        self._closing = False
        self._requests = set()
        self._exports = {}

    async def work(self, operation, *, connected=True, serial=True):
        token = threading.Event()
        state = {'started': False, 'released': False}
        with self._guard:
            if self._closing:
                return failure(DebugError('DISCONNECTED', 'MCP server is shutting down'))
            if len(self._requests) >= 32:
                return failure(DebugError('QUEUE_FULL', 'MCP operation queue is full'))
            self._requests.add(token)

        def invoke():
            acquired = False
            with self._guard:
                if state['released']:
                    return failure(DebugError('CANCELED', 'Operation canceled before execution'))
                state['started'] = True
            try:
                if serial:
                    while not token.is_set():
                        if self._operation_lock.acquire(timeout=.05):
                            acquired = True
                            break
                if token.is_set():
                    raise DebugError('CANCELED', 'Operation canceled before execution')
                client = self.client
                if connected and client is None:
                    raise DebugError('NOT_CONNECTED', 'Call play_connect first')
                if client is None:
                    value = operation(None, token)
                else:
                    with client.cancellation(token):
                        value = operation(client, token)
                return value if isinstance(value, types.CallToolResult) else result(value)
            except (DebugError, OSError, ValueError) as error:
                return failure(error)
            finally:
                if acquired:
                    self._operation_lock.release()
                with self._guard:
                    self._requests.discard(token)
                    state['released'] = True

        try:
            return await anyio.to_thread.run_sync(invoke, abandon_on_cancel=True)
        except BaseException:
            token.set()
            with self._guard:
                if not state['started']:
                    self._requests.discard(token)
                    state['released'] = True
            raise

    def connect(self, _, token):
        reused = self.client is not None and not self.client._closed
        if not reused:
            if self.client:
                self.client.close()
            client = Client(self.host, self.port, self.timeout, cancellation=token)
            with self._guard:
                if self._closing or token.is_set():
                    client.close()
                    raise DebugError('CANCELED', 'Connection canceled')
                self.client = client
        with self.client.cancellation(token):
            return {'host': self.host, 'port': self.port, 'reused': reused, 'hello': self.client.hello,
                    'status': self.client.status()}

    def disconnect(self, _, token):
        if self.client:
            self.client.close()
            self.client = None
        return {'connected': False}

    def shutdown(self):
        with self._guard:
            self._closing = True
            for token in self._requests:
                token.set()
            client = self.client
        if client:
            client.close()

    def destination(self, filename):
        if not filename or Path(filename).name != filename or filename in ('.', '..') or '\\' in filename or '\0' in filename:
            raise ValueError('filename must be a plain file name')
        with self._guard:
            if len(self._exports) >= MAX_EXPORTS:
                raise DebugError('EXPORT_LIMIT', 'Release an export before saving another')
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir / (uuid.uuid4().hex + '-' + filename)

    def publish(self, path, mime, inline_image=False):
        size = path.stat().st_size
        with self._guard:
            if size > MAX_EXPORT_BYTES or sum(item['size'] for item in self._exports.values()) + size > MAX_EXPORT_BYTES:
                path.unlink(missing_ok=True)
                raise DebugError('EXPORT_LIMIT', 'Saved export quota exceeds 128 MiB')
            kind = 'json' if mime == 'application/json' else 'png' if mime == 'image/png' else 'binary'
            item = {'name': path.name, 'path': str(path), 'mime': mime, 'size': size,
                    'uri': f'playdebug://exports/{kind}/{path.name}'}
            self._exports[path.name] = item
        content = [types.ResourceLink(name=path.name, uri=item['uri'], mime_type=mime, size=size)]
        if inline_image and size <= 4 * 1024 * 1024:
            content.append(types.ImageContent(data=base64.b64encode(path.read_bytes()).decode('ascii'), mime_type=mime))
        return result(item, extra=content)

    def save(self, producer, filename, mime, inline_image=False):
        path = self.destination(filename)
        try:
            data = producer()
            if len(data) > MAX_EXPORT_BYTES:
                raise DebugError('EXPORT_LIMIT', 'Export exceeds 128 MiB')
            with path.open('xb') as output:
                output.write(data)
            return self.publish(path, mime, inline_image)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def read_export(self, kind, name):
        with self._guard:
            item = self._exports.get(name)
            if item is None or item['uri'] != f'playdebug://exports/{kind}/{name}':
                raise ResourceError('Unknown or released export')
            path = Path(item['path'])
            if path.stat().st_size != item['size']:
                raise ResourceError('Saved export changed on disk')
            return path.read_text() if kind == 'json' else path.read_bytes()

    def release_export(self, name):
        with self._guard:
            item = self._exports.pop(name, None)
            if item is None:
                raise DebugError('STALE_REFERENCE', 'Unknown or released export')
            Path(item['path']).unlink(missing_ok=True)
        return {'released': name}


def create_server(host='127.0.0.1', port=28011, timeout=10, output_dir=None):
    bridge = Bridge(host, port, timeout, output_dir or Path(tempfile.gettempdir()) / 'play-debug-artifacts')

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield bridge
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(bridge.shutdown)

    server = MCPServer('Play! Debugger', version='1.0.0', instructions=GUIDE, lifespan=lifespan)

    @server.tool(annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def play_connect() -> types.CallToolResult:
        """Connect to the configured Play! debugger and pause via hello; keep the session until disconnect. Repeated calls reuse it."""
        return await bridge.work(bridge.connect, connected=False)

    @server.tool(annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def play_disconnect() -> types.CallToolResult:
        """Disconnect the controller, cancel its queued work/capture, and retain target execution state."""
        return await bridge.work(bridge.disconnect, connected=False)

    @server.tool(annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False))
    async def play_rpc(method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> types.CallToolResult:
        """Call any remote debugger RPC over the persistent session. Use hex addresses/wide registers and Base64 bytes; never retry a timed-out mutation."""
        return await bridge.work(lambda client, _: client.call(method, params, timeout=timeout))

    def add_rpc(method):
        async def call(params: dict[str, Any] | None = None, timeout: float | None = None) -> types.CallToolResult:
            return await bridge.work(lambda client, _: client.call(method, params, timeout=timeout))
        server.add_tool(call, name='play_' + method.replace('.', '_'),
                        description=f'Play! RPC {method}. ' + PARAM_HINTS.get(method, 'Protocol keyword arguments in params; lists support offset/limit. See playdebug://methods.'),
                        annotations=types.ToolAnnotations(read_only_hint=method in READ_ONLY, destructive_hint=method in DESTRUCTIVE,
                                                          idempotent_hint=method in READ_ONLY, open_world_hint=False))
    for method in RPC_METHODS:
        add_rpc(method)

    @server.tool(annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=False))
    async def play_wait_event(name: str | None = None, timeout: float = 10) -> types.CallToolResult:
        """Consume a matching stop/program/task/artifact event; other control tools remain available while waiting."""
        return await bridge.work(lambda client, _: client.wait_event(name, timeout), serial=False)

    @server.tool()
    async def play_artifact_save(artifact: str, filename: str = 'artifact.bin', mime: str = 'application/octet-stream', release: bool = True) -> types.CallToolResult:
        """Download a remote artifact in bounded chunks, save a local file/resource, and optionally release the remote ID."""
        def save(client, _):
            path = bridge.destination(filename)
            try:
                data = client.download(artifact)
                if len(data) > MAX_EXPORT_BYTES:
                    raise DebugError('EXPORT_LIMIT', 'Export exceeds 128 MiB')
                with path.open('xb') as output:
                    output.write(data)
                return bridge.publish(path, mime)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            finally:
                if release:
                    client._release_artifact(artifact)
        return await bridge.work(save)

    @server.tool()
    async def play_gs_dump_save(filename: str = 'gs-state.json') -> types.CallToolResult:
        """Save server-generated live GS JSON including 4 MiB RAM. Requires pause; does not advance CPU/frames."""
        return await bridge.work(lambda client, _: bridge.save(client.gs_dump, filename, 'application/json'))

    @server.tool()
    async def play_screenshot_save(filename: str = 'screenshot.png', inline_image: bool = True) -> types.CallToolResult:
        """Save the current PNG screenshot; include an MCP image preview when at most 4 MiB. Works paused with an existing picture."""
        return await bridge.work(lambda client, _: bridge.save(client.screenshot, filename, 'image/png', inline_image))

    @server.tool()
    async def play_replay_open_file(path: str, renderer: Literal['opengl', 'vulkan'] = 'vulkan') -> types.CallToolResult:
        """Upload an existing local frame dump and open an isolated replay; does not load a program into the live VM."""
        def open_file(client, _):
            if Path(path).stat().st_size > MAX_EXPORT_BYTES:
                raise ValueError('Frame dump upload exceeds 128 MiB')
            artifact = client.upload_dump(path)
            try:
                return client.replay_open(artifact=artifact['artifact'], renderer=renderer)
            finally:
                client._release_artifact(artifact['artifact'])
        return await bridge.work(open_file)

    @server.tool(annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False))
    async def play_export_release(name: str) -> types.CallToolResult:
        """Delete one file generated by this MCP process and release its readable resource/quota."""
        return await bridge.work(lambda _, token: bridge.release_export(name), connected=False)

    @server.tool(annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False))
    async def play_cli(command: str, arguments: list[str] | None = None, cpu: CPU = 'ee', session: str | None = None) -> types.CallToolResult:
        """Run an existing debugger CLI command (no OS shell) over the persistent session. arguments are CLI key=value strings; file outputs use plain names."""
        def call(client, _):
            values = list(arguments or [])
            exporting = command in ('screenshot', 'gs-dump', 'capture') and values and '=' not in values[0]
            if command == 'replay-open' and values and '=' not in values[0] and Path(values[0]).stat().st_size > MAX_EXPORT_BYTES:
                raise ValueError('Frame dump upload exceeds 128 MiB')
            path = bridge.destination(values[0]) if exporting else None
            if path:
                values[0] = str(path)
            try:
                value = execute(client, command, values, cpu, session)
                if path:
                    mime = 'image/png' if command == 'screenshot' else 'application/json' if command == 'gs-dump' else 'application/x-play-frame-dump'
                    return bridge.publish(path, mime, command == 'screenshot')
                return value
            except BaseException:
                if path:
                    path.unlink(missing_ok=True)
                raise
        return await bridge.work(call)

    @server.resource('playdebug://guide', mime_type='text/plain')
    def guide() -> str:
        return GUIDE

    @server.resource('playdebug://methods', mime_type='application/json')
    def methods() -> str:
        return json.dumps({'methods': RPC_METHODS, 'parameter_hints': PARAM_HINTS}, indent=2)

    def add_exports(kind, mime):
        def read(name: str) -> str | bytes:
            return bridge.read_export(kind, name)
        server.resource(f'playdebug://exports/{kind}/{{name}}', name=f'{kind}_exports', mime_type=mime)(read)
    for kind, mime in [('json', 'application/json'), ('png', 'image/png'), ('binary', 'application/octet-stream')]:
        add_exports(kind, mime)
    return server, bridge


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=28011)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--output-dir', type=Path, default=Path(tempfile.gettempdir()) / 'play-debug-artifacts')
    args = parser.parse_args(argv)
    if not 0 < args.port < 65536 or not 0 < args.timeout <= 120:
        parser.error('port must be 1..65535 and timeout must be >0 and <=120')
    server, bridge = create_server(args.host, args.port, args.timeout, args.output_dir)
    try:
        server.run(transport='stdio')
    finally:
        bridge.shutdown()


if __name__ == '__main__':
    main()
