"""Play! protocol v1 client. Python 3.9+; standard library only.

Run: python3 tools/remote_debugger/playdebug.py --help
Mutations are never retried. A timeout on a started operation has unknown outcome.
"""
import argparse
import base64
import cmd
import collections
from contextlib import contextmanager
import json
import shlex
import socket
import struct
import threading
import time
from pathlib import Path

MESSAGE_LIMIT = 8 * 1024 * 1024
BLOCK_LIMIT = 256 * 1024
RPC_METHODS = (
    'status pause continue step registers.read registers.write memory.read memory.write '
    'breakpoints.list breakpoints.add breakpoints.remove disassemble effective_address callstack expression.evaluate '
    'search.word search.half search.callers analysis.run analysis.functions analysis.vu assemble.jal '
    'elf.header elf.programs elf.sections elf.symbols modules kernel.types kernel.objects '
    'tags.list tags.set tags.remove tags.save tags.load screenshot gs.draw gs.state gs.dump frame.capture logs.read '
    'task.status task.cancel request.cancel artifact.read artifact.begin artifact.append artifact.release '
    'replay.open replay.close replay.export replay.packets replay.writes replay.kicks replay.seek '
    'replay.next_kick replay.previous_kick replay.context replay.options replay.image '
    'replay.vu1.registers.read replay.vu1.registers.write replay.vu1.memory.read replay.vu1.memory.write '
    'replay.vu1.disassemble replay.vu1.effective_address replay.vu1.step replay.vu1.gif replay.vu1.analysis.vu'
).split()


class DebugError(Exception):
    def __init__(self, code, message, details=None):
        self.code, self.message, self.details = code, message, details or {}
        super().__init__(f'{code}: {message}')


class _Pending:
    def __init__(self):
        self.event = threading.Event()
        self.response = None


class Client:
    """One control session; connecting performs hello and pauses the entire VM.

    Named RPC helpers (e.g. elf_header(cpu='ee')) accept protocol keyword params.
    call() is available for every advertised capability. Events are buffered (1024).
    """
    def __init__(self, host='127.0.0.1', port=28011, timeout=10.0, *, cancellation=None):
        self.timeout = timeout
        self.generation = None
        self.hello = None
        self._cancellation = threading.local()
        self._cancellation.event = cancellation
        self._check_cancellation()
        self._socket = socket.create_connection((host, port), timeout)
        self._socket.settimeout(None)
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._pending = {}
        self._events = collections.deque()
        self._next_id = 1
        self._closed = False
        self._failure = None
        self._reader = threading.Thread(target=self._receive, daemon=True, name='PlayDebug receiver')
        self._reader.start()
        try:
            self.hello = self.call('hello', version=1)
            if self.hello.get('version') != 1:
                raise DebugError('VERSION_MISMATCH', 'Server did not negotiate version 1')
        except BaseException:
            self.close()
            raise
        finally:
            self._cancellation.event = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._fail(DebugError('DISCONNECTED', 'Client closed'))
        if threading.current_thread() != self._reader:
            self._reader.join(timeout=2)

    @contextmanager
    def cancellation(self, event):
        """Cancel this thread's calls using a threading.Event; mutations are not retried."""
        previous = getattr(self._cancellation, 'event', None)
        self._cancellation.event = event
        try:
            yield
        finally:
            self._cancellation.event = previous

    def _check_cancellation(self):
        event = getattr(self._cancellation, 'event', None)
        if event is not None and event.is_set():
            raise DebugError('CANCELED', 'Operation canceled before sending a request', {'canceled_before_execution': True})

    def _release_artifact(self, artifact):
        # Cleanup must still run when the surrounding download was canceled.
        with self.cancellation(None):
            self.call('artifact.release', artifact=artifact)

    def _fail(self, error):
        with self._condition:
            if not self._closed:
                self._failure = error
                self._closed = True
                try:
                    self._socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._socket.close()
            for p in self._pending.values():
                p.event.set()
            self._condition.notify_all()

    def _exact(self, size):
        data = bytearray()
        while len(data) < size:
            part = self._socket.recv(size - len(data))
            if not part:
                raise DebugError('DISCONNECTED', 'Server closed the connection')
            data.extend(part)
        return bytes(data)

    def _receive(self):
        try:
            while True:
                size, = struct.unpack('!I', self._exact(4))
                if not 0 < size <= MESSAGE_LIMIT:
                    raise DebugError('INVALID_MESSAGE', 'Server message length is out of range')
                message = json.loads(self._exact(size).decode('utf-8'))
                if not isinstance(message, dict):
                    raise DebugError('INVALID_MESSAGE', 'Server message is not an object')
                with self._condition:
                    if message.get('type') == 'response':
                        pending = self._pending.get(message.get('id'))
                        if pending is not None:
                            pending.response = message
                            pending.event.set()
                    elif message.get('type') == 'event':
                        if message.get('event') == 'connection_rejected':
                            error = message.get('error', {})
                            raise DebugError(error.get('code', 'CONTROLLER_BUSY'), error.get('message', 'Controller busy'))
                        if len(self._events) >= 1024:
                            raise DebugError('EVENT_OVERFLOW', 'Consume events before the bounded queue fills')
                        self._events.append(message)
                        self._condition.notify_all()
                    else:
                        raise DebugError('INVALID_MESSAGE', 'Unknown server message type')
        except (OSError, ValueError, DebugError) as error:
            self._fail(error if isinstance(error, DebugError) else DebugError('DISCONNECTED', str(error)))

    def call(self, method, params=None, *, timeout=None, _cancel_on_timeout=True, **kwargs):
        if method != 'request.cancel':
            self._check_cancellation()
        params = dict(params or {})
        params.update(kwargs)
        timeout = self.timeout if timeout is None else timeout
        if timeout <= 0:
            raise ValueError('timeout must be positive')
        params.setdefault('timeout_ms', min(120000, max(1, int(timeout * 1000))))
        if self.generation is not None and method not in ('hello', 'status', 'request.cancel'):
            params.setdefault('generation', self.generation)
        pending = _Pending()
        with self._lock:
            if self._closed:
                raise self._failure
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = pending
        try:
            body = json.dumps({'id': request_id, 'method': method, 'params': params}, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
            if len(body) > MESSAGE_LIMIT:
                raise ValueError('Request exceeds 8 MiB')
            with self._send_lock:
                self._socket.sendall(struct.pack('!I', len(body)) + body)
            cancellation = getattr(self._cancellation, 'event', None) if method != 'request.cancel' else None
            deadline = time.monotonic() + timeout
            was_canceled = False
            while not pending.event.wait(min(.05, max(0, deadline - time.monotonic())) if cancellation else timeout):
                was_canceled = cancellation is not None and cancellation.is_set()
                if was_canceled or time.monotonic() >= deadline:
                    break
            if pending.response is None and not self._closed:
                canceled = False
                if _cancel_on_timeout:
                    try:
                        canceled = self.call('request.cancel', id=request_id, timeout=1, _cancel_on_timeout=False).get('canceled', False)
                    except (DebugError, OSError):
                        pass
                code = 'CANCELED' if was_canceled else 'TIMEOUT'
                message = 'Request canceled; do not retry a mutation' if was_canceled else 'Request timed out; do not retry a mutation'
                raise DebugError(code, message, {'request_id': request_id, 'canceled_before_execution': canceled})
            with self._lock:
                response = pending.response
                if response is None:
                    raise self._failure or DebugError('DISCONNECTED', 'Connection lost')
            if 'error' in response:
                error = response['error']
                raise DebugError(error['code'], error['message'], error.get('details'))
            if 'result' not in response:
                raise DebugError('INVALID_MESSAGE', 'Missing result or error')
            result = response['result']
            if isinstance(result, dict) and 'generation' in result:
                self.generation = result['generation']
            return result
        except OSError as error:
            self._fail(DebugError('DISCONNECTED', str(error)))
            raise self._failure from error
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def wait_event(self, name=None, timeout=None, predicate=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self._condition:
            while True:
                self._check_cancellation()
                for event in self._events:
                    if (name is None or event['event'] == name) and (predicate is None or predicate(event)):
                        self._events.remove(event)
                        if 'generation' in event.get('data', {}):
                            self.generation = event['data']['generation']
                        return event
                if self._closed:
                    raise self._failure
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DebugError('TIMEOUT', 'No matching event before deadline')
                self._condition.wait(min(remaining, .05) if getattr(self._cancellation, 'event', None) else remaining)

    def status(self):
        return self.call('status')

    def pause(self):
        return self.call('pause')

    def resume(self):
        return self.call('continue')

    def step(self, cpu='ee', **params):
        return self.call('step', cpu=cpu, **params)

    def registers(self, cpu='ee'):
        return self.call('registers.read', cpu=cpu)

    def memory_read(self, address, size=64, cpu='ee', space='data', **params):
        return base64.b64decode(self.call('memory.read', address=_address(address), size=size, cpu=cpu, space=space, **params)['data'], validate=True)

    def memory_write(self, address, data, cpu='ee', space='data', **params):
        if len(data) > BLOCK_LIMIT:
            raise ValueError('Memory write exceeds 256 KiB')
        return self.call('memory.write', address=_address(address), data=base64.b64encode(data).decode('ascii'), cpu=cpu, space=space, **params)

    def download(self, artifact, destination=None):
        if isinstance(artifact, dict):
            artifact = artifact['artifact']
        output = bytearray()
        while True:
            chunk = self.call('artifact.read', artifact=artifact, offset=len(output), size=BLOCK_LIMIT)
            data = base64.b64decode(chunk['data'], validate=True)
            if len(data) > BLOCK_LIMIT:
                raise DebugError('INVALID_MESSAGE', 'Artifact block exceeds limit')
            output.extend(data)
            if chunk['eof']:
                break
            if not data:
                raise DebugError('INVALID_MESSAGE', 'Empty incomplete artifact block')
        if len(output) != chunk['size']:
            raise DebugError('INVALID_MESSAGE', 'Artifact size mismatch')
        if destination is not None:
            Path(destination).write_bytes(output)
        return bytes(output)

    def iter_search(self, kind, value, begin, end, cpu='ee'):
        """Scan the complete requested range in bounded server operations."""
        if kind not in ('word', 'half', 'callers'):
            raise ValueError('kind must be word, half or callers')
        begin = int(begin, 0) if isinstance(begin, str) else begin
        end = int(end, 0) if isinstance(end, str) else end
        while begin < end:
            next_address = min(begin + BLOCK_LIMIT, end)
            offset = 0
            while True:
                page = self.call('search.' + kind, begin=hex(begin), end=hex(next_address), value=_address(value), cpu=cpu, offset=offset)
                yield from page['items']
                if page['next'] is None:
                    break
                offset = page['next']
            begin = next_address

    def upload_dump(self, source):
        data = Path(source).read_bytes() if isinstance(source, (str, Path)) else bytes(source)
        artifact = self.call('artifact.begin', size=len(data), mime='application/x-play-frame-dump')
        try:
            for offset in range(0, len(data), BLOCK_LIMIT):
                self.call('artifact.append', artifact=artifact['artifact'], offset=offset, data=base64.b64encode(data[offset:offset + BLOCK_LIMIT]).decode('ascii'))
            artifact['size'] = len(data)
            return artifact
        except BaseException:
            try:
                self._release_artifact(artifact['artifact'])
            except DebugError:
                pass
            raise

    def screenshot(self, destination=None):
        artifact = self.call('screenshot')
        try:
            return self.download(artifact, destination)
        finally:
            self._release_artifact(artifact['artifact'])

    def gs_dump(self, destination=None, **params):
        """Download the paused live GS snapshot as JSON, including 4 MiB RAM."""
        artifact = self.call('gs.dump', **params)
        try:
            return self.download(artifact, destination)
        finally:
            self._release_artifact(artifact['artifact'])

    def wait_task(self, task, timeout=None):
        task_id = task['task'] if isinstance(task, dict) else task
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            result = self.call('task.status', task=task_id)
            if result['state'] == 'complete':
                return result
            if result['state'] == 'failed':
                error = result['error']
                raise DebugError(error['code'], error['message'])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.call('task.cancel', task=task_id)
                raise DebugError('TIMEOUT', 'Task wait expired; cancellation requested')
            # Events wake promptly; status also handles events consumed elsewhere.
            try:
                self.wait_event(timeout=min(remaining, .25), predicate=lambda e: e.get('data', {}).get('task') == task_id)
            except DebugError as error:
                if error.code != 'TIMEOUT':
                    raise

    def capture(self, destination=None, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        task = self.call('frame.capture', timeout=timeout)
        try:
            result = self.wait_task(task, timeout)
        except DebugError as error:
            if error.code == 'CANCELED':
                with self.cancellation(None):
                    try:
                        self.call('task.cancel', task=task['task'])
                    except DebugError:
                        pass
            raise
        try:
            return self.download(result['artifact'], destination)
        finally:
            self._release_artifact(result['artifact'])


def _address(value):
    return hex(value) if isinstance(value, int) else value


def _rpc_helper(method):
    def helper(self, **params):
        return self.call(method, **params)
    helper.__name__ = method.replace('.', '_')
    helper.__doc__ = f'Call {method}; keyword arguments are protocol params.'
    return helper


for _method in RPC_METHODS:
    _name = _method.replace('.', '_')
    if not hasattr(Client, _name):
        setattr(Client, _name, _rpc_helper(_method))


def _params(items):
    result = {}
    for item in items:
        if '=' not in item:
            raise ValueError('Parameters must use key=value, e.g. address=0x00100000')
        key, value = item.split('=', 1)
        if value.startswith('0x'):
            result[key] = value
        else:
            try:
                result[key] = json.loads(value)
            except ValueError:
                result[key] = value
    return result


def _print(value, as_json=False):
    if isinstance(value, bytes):
        value = {'size': len(value), 'data': base64.b64encode(value).decode('ascii')} if as_json else value.hex(' ')
    if as_json or isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=None if as_json else 2))
    else:
        print(value)


class Shell(cmd.Cmd):
    intro = 'Play! remote debugger. help lists commands; cpu selects a processor; quit disconnects.'
    prompt = 'ee> '

    def __init__(self, client, as_json=False):
        super().__init__()
        self.client, self.as_json, self.cpu, self.session = client, as_json, 'ee', None

    def do_cpu(self, value):
        if value not in ('ee', 'iop', 'vu0', 'vu1'):
            print('cpu ee|iop|vu0|vu1')
            return
        self.cpu, self.prompt = value, value + '> '

    def do_quit(self, _):
        return True

    do_exit = do_quit
    do_EOF = do_quit

    def do_help(self, _):
        print('cpu ee|iop|vu0|vu1; break ADDRESS; delete ADDRESS; regs; continue; pause; step; events; quit')
        print('gs-state; gs-dump FILE; screenshot FILE; capture FILE; replay-open FILE [renderer=opengl|vulkan]; rpc METHOD [JSON]')
        print('\n'.join(m.replace('.', '-').replace('_', '-') + ' [key=value ...]' for m in RPC_METHODS))

    def do_events(self, _):
        try:
            _print(self.client.wait_event(timeout=.1), self.as_json)
        except DebugError as e:
            print(e)

    def default(self, line):
        try:
            words = shlex.split(line)
            if not words:
                return
            aliases = {'regs': 'registers-read', 'break': 'breakpoints-add', 'delete': 'breakpoints-remove'}
            command = aliases.get(words[0], words[0])
            values = words[1:]
            if words[0] in ('break', 'delete'):
                values = ['address=' + values[0]]
            result = execute(self.client, command, values, self.cpu, self.session)
            if command == 'replay-open':
                self.session = result['session']
            _print(result, self.as_json)
        except (DebugError, ValueError, IndexError, OSError) as error:
            print(error)


def execute(client, command, values, cpu='ee', session=None):
    if command == 'rpc':
        if not values:
            raise ValueError('rpc METHOD [JSON]')
        return client.call(values[0], json.loads(values[1]) if len(values) == 2 else _params(values[1:]))
    if command == 'screenshot' and values and '=' not in values[0]:
        client.screenshot(values[0])
        return {'saved': values[0]}
    if command == 'gs-dump' and values and '=' not in values[0]:
        client.gs_dump(values[0], **_params(values[1:]))
        return {'saved': values[0]}
    if command == 'capture':
        if not values:
            raise ValueError('capture OUTPUT [timeout=SECONDS]')
        params = _params(values[1:])
        if params.get('run', False):
            client.resume()
        client.capture(values[0], params.get('timeout'))
        return {'saved': values[0]}
    if command == 'replay-open' and values and '=' not in values[0]:
        artifact = client.upload_dump(values[0])
        try:
            return client.call('replay.open', artifact=artifact['artifact'], **_params(values[1:]))
        finally:
            client._release_artifact(artifact['artifact'])
    names = {m.replace('.', '-').replace('_', '-'): m for m in RPC_METHODS}
    names.update({m.replace('.', '-'): m for m in RPC_METHODS})
    method = names.get(command)
    if method is None:
        raise ValueError(f'Unknown command: {command}')
    params = _params(values)
    params.setdefault('cpu', cpu)
    if session and method.startswith('replay.'):
        params.setdefault('session', session)
    return client.call(method, params)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog='Commands:\n' + '\n'.join(m.replace('.', '-').replace('_', '-') + ' [key=value ...]' for m in RPC_METHODS))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=28011)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--cpu', choices=['ee', 'iop', 'vu0', 'vu1'], default='ee')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--output-dir', type=Path, help='MCP export directory')
    parser.add_argument('command', nargs='?', default='shell', help='shell, mcp, rpc, capture, screenshot, or a listed RPC command')
    parser.add_argument('params', nargs='*')
    args = parser.parse_args(argv)
    try:
        if args.command == 'mcp':
            import sys
            try:
                from playdebug_mcp import main as mcp_main
            except ImportError as error:
                print('MCP requires Python 3.10+ and requirements-mcp.txt: ' + str(error), file=sys.stderr)
                return 2
            if args.params:
                parser.error('mcp takes no command parameters')
            options = ['--host', args.host, '--port', str(args.port), '--timeout', str(args.timeout)]
            if args.output_dir:
                options += ['--output-dir', str(args.output_dir)]
            mcp_main(options)
            return 0
        with Client(args.host, args.port, args.timeout) as client:
            if args.command == 'shell':
                shell = Shell(client, args.json)
                shell.do_cpu(args.cpu)
                shell.cmdloop()
            else:
                _print(execute(client, args.command, args.params, args.cpu), args.json)
        return 0
    except (DebugError, ValueError, OSError, KeyboardInterrupt) as error:
        if args.json:
            print(json.dumps({'error': {'code': getattr(error, 'code', 'CLIENT_ERROR'), 'message': str(error)}}, ensure_ascii=False))
        else:
            print(error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
