"""Real stdio MCP / Vulkan integration; requires requirements-mcp.txt and Linux X11."""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp import Client as MCPClient, StdioServerParameters
from playdebug import RPC_METHODS
from make_test_elf import make_elf
from x11_focus import graceful_close


async def verify(root, port, fixture):
    cli = Path(__file__).resolve().parents[1] / 'playdebug.py'
    params = StdioServerParameters(command=sys.executable, args=[str(cli), '--port', str(port),
                                  '--output-dir', str(root / 'exports'), 'mcp'])
    async with MCPClient(params, read_timeout_seconds=30) as client:
        async def call(name, **arguments):
            value = await client.call_tool(name, arguments)
            assert not value.is_error, value
            return value.structured_content['result']

        async def rpc(method, **params):
            return await call('play_' + method.replace('.', '_'), params=params)

        tools = (await client.list_tools()).tools
        assert {'play_' + method.replace('.', '_') for method in RPC_METHODS} <= {tool.name for tool in tools}
        connected = await call('play_connect')
        assert connected['hello']['renderer'] == 'vulkan', connected
        assert connected['status']['state'] == 'paused'
        for cpu in ('ee', 'iop', 'vu0', 'vu1'):
            registers = await rpc('registers.read', cpu=cpu)
            assert len(registers['gpr.T7']) == 34
        await rpc('memory.write', address='0x100800', data=base64.b64encode(b'MCP test').decode())
        memory = await rpc('memory.read', address='0x100800', size=8)
        assert base64.b64decode(memory['data']) == b'MCP test'
        await call('play_cli', command='continue')
        await asyncio.sleep(.5)
        before = await rpc('status')
        await asyncio.sleep(.2)
        after = await rpc('status')
        assert after['state'] == 'running' and int(after['frames'], 16) > int(before['frames'], 16)
        reused = await call('play_connect')
        assert reused['reused'] and reused['status']['state'] == 'running'
        error = await client.call_tool('play_gs_state')
        assert error.is_error and error.structured_content['error']['code'] == 'TARGET_RUNNING'
        await rpc('pause')
        snapshot = await rpc('status')
        state = await rpc('gs.state')
        assert len(state['registers']) == 128
        item = await call('play_gs_dump_save')
        dumped = json.loads(Path(item['path']).read_text())
        assert len(base64.b64decode(dumped['ram']['data'])) == 4194304
        assert dumped['registers'] == state['registers']
        resource = await client.read_resource(item['uri'])
        assert json.loads(resource.contents[0].text) == dumped
        after = await rpc('status')
        assert after['pc'] == snapshot['pc'] and after['frames'] == snapshot['frames']
        screenshot = await client.call_tool('play_screenshot_save')
        assert not screenshot.is_error, screenshot
        item = screenshot.structured_content['result']
        assert Path(item['path']).read_bytes().startswith(b'\x89PNG')
        assert any(content.type == 'image' for content in screenshot.content)
        resource = await client.read_resource(item['uri'])
        assert base64.b64decode(resource.contents[0].blob) == Path(item['path']).read_bytes()
        captured = await call('play_cli', command='capture', arguments=['frame.dmp.zip', 'run=true', 'timeout=8'])
        assert Path(captured['path']).read_bytes().startswith(b'PK')
        await rpc('pause')
        replay = await call('play_replay_open_file', path=captured['path'])
        session = replay['session']
        await rpc('replay.context', session=session, context=0)
        image = await rpc('replay.image', session=session, context=0)
        image = await call('play_artifact_save', artifact=image['artifact'], filename='replay.png', mime='image/png')
        assert Path(image['path']).read_bytes().startswith(b'\x89PNG')
        await rpc('replay.close', session=session)
        if fixture:
            replay = await call('play_replay_open_file', path=str(Path(fixture).resolve()))
            session = replay['session']
            assert (await rpc('replay.vu1.registers.read', session=session))['pc'] == '0x00000000'
            await rpc('replay.vu1.step', session=session)
            assert int((await rpc('replay.vu1.registers.read', session=session))['vi.1'], 16) == 7
            await rpc('replay.close', session=session)
        # Explicit disconnect does not implicitly stop/resume the target.
        await rpc('continue')
        await call('play_disconnect')
        connected = await call('play_connect')
        assert not connected['reused'] and connected['status']['state'] == 'paused'
        print('PASS MCP tools/CLI, persistent control, CPU/memory, Vulkan screenshot, live GS dump/resource, capture/replay, reconnect', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--port', type=int, default=28210)
    parser.add_argument('--vu-fixture')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='play-mcp-check-'))
    print('QA directory:', root, flush=True)
    data = root / 'Play Data Files'
    data.mkdir()
    (data / 'config.xml').write_text('<Config><Preference Name="video.gshandler" Type="integer" Value="1"/>'
                                   '<Preference Name="ui.showexitconfirmation" Type="boolean" Value="false"/></Config>')
    elf = root / 'test.elf'
    make_elf(elf)
    command = ['systemd-run', '--user', '--scope', '--quiet', '-p', 'MemoryMax=2G', '-p', 'MemorySwapMax=256M',
               str(Path(args.binary).resolve()), '--remote-debug', f'127.0.0.1:{args.port}', '--elf', str(elf)]
    with (root / 'runtime.log').open('wb') as log:
        process = subprocess.Popen(command, env=dict(os.environ, XDG_CONFIG_HOME=str(root), QT_QPA_PLATFORM='xcb'),
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    with socket.create_connection(('127.0.0.1', args.port), timeout=.5):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError((root / 'runtime.log').read_text())
                    time.sleep(.1)
            asyncio.run(verify(root, args.port, args.vu_fixture))
            graceful_close(process)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)


if __name__ == '__main__':
    main()
