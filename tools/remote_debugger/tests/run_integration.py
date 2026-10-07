"""Live integration / OOM regression runner; one memory-capped emulator at a time.

python3 run_integration.py --binary build-remote/Source/ui_qt/Play [--disc /path/game.iso]
Results and captured images stay in the temporary directory printed on startup.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playdebug import Client, DebugError, RPC_METHODS
from make_test_elf import make_elf
from x11_focus import regression, graceful_close, Focus, app_pid


def expect_error(code, func):
    try:
        func()
    except DebugError as e:
        assert e.code == code, str(e)
    else:
        raise AssertionError(f'Expected {code}')


def protocol(port):
    def recv(conn):
        def exact(size):
            data = bytearray()
            while len(data) < size:
                part = conn.recv(size - len(data))
                if not part:
                    raise RuntimeError('Protocol connection closed')
                data.extend(part)
            return data
        size, = struct.unpack('!I', exact(4))
        return json.loads(exact(size))
    def encode(v):
        data = json.dumps(v).encode()
        return struct.pack('!I', len(data)) + data
    with socket.create_connection(('127.0.0.1', port)) as conn:
        for b in encode({'id': 1, 'method': 'hello', 'params': {'version': 1}}):
            conn.sendall(bytes([b]))
        assert recv(conn)['result']['state'] == 'paused'
        conn.sendall(encode({'id': 2, 'method': 'status'}) + encode({'id': 3, 'method': 'status'}))
        assert recv(conn)['id'] == 2
        assert recv(conn)['id'] == 3
        conn.sendall(struct.pack('!I', 1) + b'{')
        assert recv(conn)['error']['code'] == 'INVALID_JSON'
    time.sleep(.1)
    with socket.create_connection(('127.0.0.1', port)) as conn:
        conn.sendall(encode({'id': 1, 'method': 'hello', 'params': {'version': 2}}))
        assert recv(conn)['error']['code'] == 'VERSION_MISMATCH'
    time.sleep(.1)
    for length in (0, 8 * 1024 * 1024 + 1):
        with socket.create_connection(('127.0.0.1', port)) as conn:
            conn.sendall(struct.pack('!I', length))
            assert conn.recv(1) == b''
        time.sleep(.1)
    with socket.create_connection(('127.0.0.1', port)) as conn:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        conn.sendall(encode({'id': 1, 'method': 'hello', 'params': {'version': 1}}))
        recv(conn)
        conn.sendall(b''.join(encode({'id': i + 2, 'method': 'disassemble', 'params': {'address': '0x100000', 'count': 4096}}) for i in range(64)))
        time.sleep(1.5)  # Intentionally leave the bounded server send queue unread.
        # A new controller must be admitted while the unread old socket is still
        # open: this proves the server proactively discarded the slow session.
        deadline = time.monotonic() + 10
        while True:
            try:
                with Client(port=port, timeout=3) as replacement:
                    assert replacement.status()['state'] == 'paused'
                break
            except DebugError as error:
                if error.code != 'CONTROLLER_BUSY' or time.monotonic() >= deadline:
                    raise
                time.sleep(.3)
    time.sleep(.1)
    print('PASS slow-client bounded send queue and disconnect', flush=True)
    print('PASS protocol fragmentation/coalescing/invalid JSON/length/version', flush=True)


def deterministic(c, info):
    assert set(c.hello['capabilities']) == set(RPC_METHODS)
    expect_error('STALE_GENERATION', lambda: c.call('registers.read', generation='0x0'))
    expect_error('CONTROLLER_BUSY', lambda: Client(port=c._socket.getpeername()[1], timeout=1))
    for cpu in ('ee', 'iop', 'vu0', 'vu1'):
        regs = c.registers(cpu)
        assert len(regs['gpr.T7']) == 34
        assert set(regs) == {r['name'] for r in c.hello['cpus'][cpu]}
        stopped = c.step(cpu)
        assert stopped['reason'] == 'step' and stopped['cpu'] == cpu
    value = '0x123456789abcdef00123456789abcdef'
    c.registers_write(name='gpr.T7', value=value)
    assert c.registers()['gpr.T7'] == value
    expect_error('READ_ONLY_REGISTER', lambda: c.registers_write(name='gpr.R0', value='0x1'))
    expect_error('UNSAFE_MEMORY', lambda: c.memory_read('0x10006000'))
    expect_error('INVALID_PARAMS', lambda: c.expression_evaluate(expression='T0 / 0'))
    data = b'Play remote debugger\0'
    c.memory_write('0x100800', data)
    assert c.memory_read('0x100800', len(data)) == data
    # A short compiled block: addiu T7,R0,1; jump to itself; nop.
    code = struct.pack('<III', 0x240f0001, 0x08000000 | (0x100900 >> 2), 0)
    c.registers_write(name='gpr.T7', value='0x0')
    c.memory_write('0x100900', code, space='instruction')
    c.registers_write(name='pc', value='0x100900')
    c.step()
    assert int(c.registers()['gpr.T7'], 16) == 1
    c.memory_write('0x100900', struct.pack('<I', 0x240f0002), space='instruction')
    c.registers_write(name='pc', value='0x100900')
    c.step()
    assert int(c.registers()['gpr.T7'], 16) == 2
    c.breakpoints_add(address='0x100904')
    c.registers_write(name='pc', value='0x100900')
    c.resume()
    stopped = c.wait_event('stopped', predicate=lambda e: e['data']['reason'] == 'breakpoint')
    assert stopped['data']['cpu'] == 'ee'
    assert c.breakpoints_list()['items'] == ['0x00100904']
    c.breakpoints_remove(address='0x100904')
    c.assemble_jal(address='0x100908', target='0x100900')
    assert c.memory_read('0x100908', 4, space='instruction') == struct.pack('<I', 0x0c000000 | (0x100900 >> 2))
    c.tags_set(kind='comments', address='0x100900', text='QA marker')
    assert c.tags_list(kind='comments')['items'][0]['text'] == 'QA marker'
    c.tags_save(); c.tags_remove(kind='comments', address='0x100900'); c.tags_load()
    assert any(r['text'] == 'QA marker' for r in c.tags_list(kind='comments')['items'])
    for method in ('elf.header', 'elf.programs', 'elf.sections', 'elf.symbols', 'modules', 'kernel.types', 'callstack', 'effective_address'):
        c.call(method)
    for cpu in ('ee', 'iop'):
        types = c.kernel_types(cpu=cpu)['items']
        for schema in types:
            c.kernel_objects(cpu=cpu, type=schema['id'], limit=4)
    c.search_word(begin='0x100900', end='0x10090c', value='0x240f0002')
    c.search_half(begin='0x100900', end='0x10090c', value='0x2')
    c.search_callers(begin='0x100900', end='0x10090c', value='0x100900')
    c.analysis_run(begin='0x100900', end='0x10090c')
    c.analysis_functions(begin='0x100900', end='0x10090c')
    c.analysis_vu(cpu='vu1', begin='0x0', end='0x10')
    c.logs_read(channel='stdout'); c.logs_read(channel='stderr')
    c.gs_draw(enabled=False); c.gs_draw(enabled=True)
    # Restore the actual test program and continue in the background.
    c.registers_write(name='pc', value=info['entry'])
    c.resume()
    expect_error('TARGET_RUNNING', lambda: c.registers_write(name='pc', value=info['entry']))
    print('PASS deterministic CPU/register/memory/JIT/breakpoints/analysis/metadata', flush=True)


def replay_vu(c, fixture):
    if not fixture:
        return
    a = c.upload_dump(fixture)
    session = c.replay_open(artifact=a['artifact'], renderer='vulkan')['session']
    c.artifact_release(artifact=a['artifact'])
    assert c.replay_vu1_registers_read(session=session)['pc'] == '0x00000000'
    c.replay_vu1_step(session=session)
    assert int(c.replay_vu1_registers_read(session=session)['vi.1'], 16) == 7
    c.replay_vu1_disassemble(session=session, address='0x0', count=4)
    c.replay_vu1_memory_read(session=session, address='0x0', size=16, space='instruction')
    c.replay_vu1_memory_read(session=session, address='0x0', size=16)
    c.replay_vu1_memory_write(session=session, address='0x100', data=base64.b64encode(b'VUQA').decode())
    c.replay_vu1_registers_write(session=session, name='vi.2', value='0x12')
    c.replay_vu1_gif(session=session)
    c.replay_vu1_analysis_vu(session=session, begin='0x0', end='0x18')
    c.replay_close()
    print('PASS isolated captured VU1 step/registers/micro/data/GIF/analysis', flush=True)


def reset_regression(c, process, fixture):
    focus = Focus()
    try:
        c.pause()
        generation = c.generation
        artifact = c.artifact_begin(size=0)['artifact']
        session = None
        if fixture:
            dump = c.upload_dump(fixture)
            session = c.replay_open(artifact=dump['artifact'], renderer='vulkan')['session']
        task = c.frame_capture(timeout=5)
        main = focus.windows(app_pid(process))[0]
        focus.focus(main)
        focus.key(main, 'r', modifiers=5)  # Ctrl+Shift+R: existing UI Reset action.
        event = c.wait_event('program_changed', timeout=5, predicate=lambda e: e['data']['generation'] != generation)
        assert event['data']['generation'] != generation
        expect_error('STALE_GENERATION', lambda: c.registers_read(generation=generation))
        expect_error('STALE_REFERENCE', lambda: c.artifact_read(artifact=artifact))
        if session:
            expect_error('STALE_REFERENCE', lambda: c.replay_packets(session=session))
            c.replay_close()
        expect_error('STALE_GENERATION', lambda: c.wait_task(task))
        assert c.status()['state'] == 'paused'
        print('PASS GUI reset invalidates requests/artifacts/replay and cancels capture', flush=True)
    finally:
        focus.close()


def live_gs(c, root):
    before = c.status()
    state = c.gs_state()
    assert state['format'] == 'play-gs-state-v1'
    assert len(state['registers']) == 128
    assert all(len(reg['value']) == 18 for reg in state['registers'])
    assert state['target']['state'] == 'paused'
    assert state['target']['pc'] == before['pc'] and state['target']['frames'] == before['frames']
    assert len(state['contexts']) == 2 and len(state['vertices']) == 3
    assert 'PMODE' in state['private_registers'] and 'SMODE2' in state['private_registers']
    assert 'ram' not in state
    dumped = json.loads(c.gs_dump(root / 'live-gs.json'))
    ram = base64.b64decode(dumped['ram']['data'], validate=True)
    assert len(ram) == dumped['ram']['size'] == 4 * 1024 * 1024
    assert dumped['registers'] == state['registers']
    assert dumped['private_registers'] == state['private_registers']
    after = c.status()
    assert after['pc'] == before['pc'] and after['frames'] == before['frames']
    c.resume()
    expect_error('TARGET_RUNNING', c.gs_state)
    expect_error('TARGET_RUNNING', c.gs_dump)
    c.pause()
    print('PASS live GS state/dump: 128 registers, private state, contexts, vertices, 4 MiB RAM; no PC/frame advance', flush=True)


def graphics(c, root):
    time.sleep(1)
    before = int(c.status()['frames'], 16)
    time.sleep(.2)
    assert int(c.status()['frames'], 16) > before
    c.screenshot(root / 'running.png')
    dump = c.capture(root / 'frame.dmp.zip', timeout=8)
    c.pause()
    live_gs(c, root)
    c.screenshot(root / 'paused.png')
    expect_error('TIMEOUT', lambda: c.capture(timeout=.4))
    task = c.frame_capture(timeout=5)
    c.task_cancel(task=task['task'])
    expect_error('CANCELED', lambda: c.wait_task(task))
    for renderer in ('opengl', 'vulkan'):
        a = c.upload_dump(dump)
        s = c.replay_open(artifact=a['artifact'], renderer=renderer)['session']
        c.artifact_release(artifact=a['artifact'])
        packets = c.replay_packets(session=s, limit=4)
        c.replay_writes(session=s, packet=0)
        kicks = c.replay_kicks(session=s)['items']
        if kicks:
            c.replay_seek(session=s, command=kicks[-1]['command'])
        c.replay_context(session=s, context=0)
        c.replay_context(session=s, context=1)
        c.replay_options(session=s, display='448p', alpha=False, depth=False, blend=False)
        image = c.replay_image(session=s, context=0)
        assert c.download(image, root / f'replay-{renderer}.png').startswith(b'\x89PNG')
        c.artifact_release(artifact=image['artifact'])
        for kind in ('depthbuffer', 'texture'):
            try:
                a = c.replay_image(session=s, kind=kind)
                c.download(a, root / f'{kind}-{renderer}.png')
                c.artifact_release(artifact=a['artifact'])
            except DebugError as e:
                assert e.code in ('NO_IMAGE', 'UNSUPPORTED'), str(e)
        exported = c.replay_export(session=s)
        assert c.download(exported)[:2] == b'PK'
        c.artifact_release(artifact=exported['artifact'])
        c.replay_close()
    print('PASS background frames/screenshots/capture/timeout/cancel/OpenGL+Vulkan replay/export', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--disc')
    parser.add_argument('--port', type=int, default=28100)
    parser.add_argument('--renderer', choices=('opengl', 'vulkan'), default='opengl')
    parser.add_argument('--oom-regression', action='store_true')
    parser.add_argument('--vu-fixture')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='play-remote-check-'))
    print(f'QA directory: {root}', flush=True)
    data = root / 'Play Data Files'
    data.mkdir()
    # Preference values: OpenGL=0, Vulkan=1. Never touch user configuration.
    (data / 'config.xml').write_text('<Config><Preference Name="video.gshandler" Type="integer" Value="' + str(args.renderer == 'vulkan' and 1 or 0) + '"/><Preference Name="ui.showexitconfirmation" Type="boolean" Value="false"/></Config>')
    elf = root / 'test.elf'
    info = make_elf(elf)
    if args.oom_regression:
        # Disable the delay loop to reproduce the original high-rate producer.
        data_bytes = bytearray(elf.read_bytes())
        for i in range(0x100, 0x500, 4):
            if data_bytes[i:i+4] == struct.pack('<I', 0x15a0fffe):
                data_bytes[i:i+4] = b'\0' * 4
        elf.write_bytes(data_bytes)
    env = dict(os.environ, XDG_CONFIG_HOME=str(root), QT_QPA_PLATFORM='xcb')
    command = ['systemd-run', '--user', '--scope', '--quiet', '-p', 'MemoryMax=2G', '-p', 'MemorySwapMax=256M', '-p', 'MemoryHigh=1536M',
               str(Path(args.binary).resolve()), '--remote-debug', f'127.0.0.1:{args.port}']
    command += ['--disc', args.disc] if args.disc else ['--elf', str(elf)]
    with (root / 'runtime.log').open('wb') as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    c = Client(port=args.port, timeout=2)
                    c.close()
                    break
                except (OSError, DebugError):
                    if time.monotonic() > deadline or process.poll() is not None:
                        raise RuntimeError((root / 'runtime.log').read_text())
                    time.sleep(.2)
            time.sleep(.1)
            protocol(args.port)
            with Client(port=args.port) as c:
                if args.disc:
                    print('Game:', c.hello['program'], 'renderer:', c.hello['renderer'], flush=True)
                    c.resume()
                    for _ in range(6):
                        time.sleep(5)
                        print('Game progress:', c.status(), flush=True)
                    print('Game status:', c.status(), flush=True)
                    graphics(c, root)
                elif args.oom_regression:
                    c.resume()
                    # Reproduce capture too; it must return an explicit bounded failure.
                    task = c.frame_capture(timeout=3)
                    try:
                        c.wait_task(task, timeout=4)
                    except DebugError as e:
                        assert e.code in ('TIMEOUT', 'CAPTURE_LIMIT'), str(e)
                        print('Bounded capture result:', e, flush=True)
                    time.sleep(5)
                    print('OOM regression target responsive:', c.status(), flush=True)
                    c.pause()
                else:
                    deterministic(c, info)
                    graphics(c, root)
                    replay_vu(c, args.vu_fixture)
                    reset_regression(c, process, args.vu_fixture)
                    regression(c, process)
                    c.resume()
            time.sleep(.2)
            with Client(port=args.port) as c:
                assert c.status()['state'] == 'paused'
                print('PASS disconnect/reconnect takeover', flush=True)
            time.sleep(.1)
            cli = str(Path(__file__).resolve().parents[1] / 'playdebug.py')
            result = subprocess.run([sys.executable, cli, '--port', str(args.port), '--json', 'status'], capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stdout + result.stderr
            assert json.loads(result.stdout)['state'] == 'paused'
            time.sleep(.1)
            result = subprocess.run([sys.executable, cli, '--port', str(args.port), '--json', 'gs-state'], capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stdout + result.stderr
            assert len(json.loads(result.stdout)['registers']) == 128
            time.sleep(.1)
            result = subprocess.run([sys.executable, cli, '--port', str(args.port), '--json', 'gs-dump', str(root / 'cli-live-gs.json')], capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stdout + result.stderr
            assert json.loads((root / 'cli-live-gs.json').read_text())['ram']['size'] == 4194304
            time.sleep(.1)
            result = subprocess.run([sys.executable, cli, '--port', str(args.port), 'shell'], input='cpu ee\nregs\nstep\nbreak 0x100000\ndelete 0x100000\ncontinue\npause\nquit\n', capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stdout + result.stderr
            assert 'gpr.T0' in result.stdout and '"reason": "step"' in result.stdout
            print('PASS real one-shot JSON CLI and interactive CPU/register/step/break/control commands', flush=True)
            graceful_close(process)
        finally:
            # Stop only this test's process group, even when assertions fail.
            import signal
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)


if __name__ == '__main__':
    main()
