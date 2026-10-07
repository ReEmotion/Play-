"""Live Qt remote-debugger settings regression (X11, standard library only)."""
import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playdebug import Client, DebugError
from make_test_elf import make_elf
from test_normal_focus import find_counter
from x11_focus import Focus, app_pid, graceful_close


def wait_for(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('Timed out waiting for Qt / listener')


def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.2):
            return True
    except OSError:
        return False


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    binary = str(Path(args.binary).resolve())
    root = Path(tempfile.mkdtemp(prefix='play-remote-settings-'))
    print('QA directory:', root, flush=True)
    data = root / 'Play Data Files'; data.mkdir()
    config = data / 'config.xml'
    initial_port = free_port() if listening(28011) else 28011
    config.write_text('<Config><Preference Name="ui.showexitconfirmation" Type="boolean" Value="false"/>' +
                      (f'<Preference Name="debugger.remote.endpoint" Type="string" Value="127.0.0.1:{initial_port}"/>' if initial_port != 28011 else '') + '</Config>')
    elf = root / 'fixture.elf'; make_elf(elf)
    process = None; focus = None; fd = None; client = None
    logs = []

    def launch(*options):
        log = (root / f'runtime-{len(logs)}.log').open('wb'); logs.append(log)
        child = subprocess.Popen(['systemd-run', '--user', '--scope', '--quiet', '-p', 'MemoryMax=2G', '-p', 'MemorySwapMax=256M',
                                  binary, '--elf', str(elf), *options],
                                 env=dict(os.environ, XDG_CONFIG_HOME=str(root), QT_QPA_PLATFORM='xcb'),
                                 stdout=log, stderr=log, start_new_session=True)
        time.sleep(1)
        pid = app_pid(child)
        window = wait_for(lambda: focus.windows(pid))[0]
        focus.focus(window)
        return child, pid, window

    def settings(enabled=None, address=None, port=None, failure=False):
        focus.focus(window)
        before = set(focus.windows(pid))
        focus.key(window, ',', 4)  # Ctrl+, opens Settings.
        dialog = wait_for(lambda: next((w for w in focus.windows(pid) if w not in before), None))
        focus.focus(dialog)
        focus.key(dialog, 0xff57)  # End selects the Remote Debugger page.
        time.sleep(.1)
        focus.key(dialog, 0xff09)  # Tab: enable checkbox.
        if enabled is not None:
            focus.key(dialog, ' ')
        if address is not None or port is not None:
            focus.key(dialog, 0xff09)  # Address input.
            if address is not None:
                focus.key(dialog, 'a', 4)
                for character in address:
                    focus.key(dialog, character)
            focus.key(dialog, 0xff09)  # Port input.
            if port is not None:
                focus.key(dialog, 'a', 4)
                for character in str(port):
                    focus.key(dialog, character)
        focus.key(dialog, 0xff1b)  # Close applies settings.
        if failure:
            alert = wait_for(lambda: next((w for w in focus.windows(pid) if w not in before and w != dialog), None))
            focus.focus(alert); focus.key(alert, 0xff0d)
        wait_for(lambda: dialog not in focus.windows(pid))
        time.sleep(.25)
        focus.focus(window)

    def toggle_background():
        focus.focus(window)
        before = set(focus.windows(pid))
        focus.key(window, ',', 4)
        dialog = wait_for(lambda: next((w for w in focus.windows(pid) if w not in before), None))
        focus.focus(dialog)
        focus.key(dialog, 0xff50)  # Home: General page.
        for _ in range(4):
            focus.key(dialog, 0xff09)
        focus.key(dialog, ' ')
        focus.key(dialog, 0xff1b)
        wait_for(lambda: dialog not in focus.windows(pid))
        focus.focus(window)

    def preferences():
        return {item.attrib['Name']: item.attrib['Value'] for item in ET.parse(config).getroot()}

    try:
        focus = Focus()
        process, pid, window = launch()
        wait_for(lambda: listening(initial_port)); time.sleep(.15)
        client = Client(port=initial_port)
        client.resume()
        fd, counter_address = find_counter(pid, elf.read_bytes()[0x100:0x120])
        def counter():
            return int.from_bytes(os.pread(fd, 4, counter_address), 'little')
        client.pause()
        settings(enabled=False)
        assert not listening(initial_port)
        a = counter(); time.sleep(.2); assert counter() == a, 'Disabling resumed a paused target'
        assert preferences()['debugger.remote.enabled'] == 'false'
        client.close(); client = None
        print('PASS default startup; Qt disable persists and preserves pause', flush=True)

        settings(enabled=True)
        wait_for(lambda: listening(initial_port)); time.sleep(.15)
        client = Client(port=initial_port); client.resume()
        port = free_port()
        settings(address='0.0.0.0', port=port)
        client.close(); client = None
        a = counter(); time.sleep(.25); assert counter() > a, 'Rebinding paused the running target'
        assert not listening(initial_port)
        assert any(row.split()[1] == f'00000000:{port:04X}' and row.split()[3] == '0A'
                   for row in Path('/proc/net/tcp').read_text().splitlines()[1:])
        assert preferences()['debugger.remote.endpoint'] == f'0.0.0.0:{port}'
        time.sleep(.15)
        client = Client(port=port); client.resume()
        with socket.socket() as occupied:
            occupied.bind(('0.0.0.0', 0)); occupied.listen()
            settings(port=occupied.getsockname()[1], failure=True)
        assert client.status()['state'] == 'running'
        assert preferences()['debugger.remote.endpoint'] == f'0.0.0.0:{port}'
        print('PASS editable 0.0.0.0 binding, state-preserving rebind, failed bind retains session/settings', flush=True)

        toggle_background()
        assert preferences()['ui.allowbackgroundrun'] == 'false'
        assert client.status()['state'] == 'running'
        focus.focus(focus.dummy)
        a = counter(); time.sleep(.3); assert counter() == a
        assert client.status()['state'] == 'paused'
        focus.focus(window)
        a = counter(); time.sleep(.3); assert counter() > a
        toggle_background()
        assert preferences()['ui.allowbackgroundrun'] == 'true'
        focus.focus(focus.dummy)
        a = counter(); time.sleep(.3); assert counter() > a
        client.pause(); focus.focus(window)
        assert client.status()['state'] == 'paused'
        client.resume()
        print('PASS independent background setting: focus pause/resume when off; running/manual pause retained when on', flush=True)

        settings(enabled=False)
        client.close(); client = None
        a = counter(); time.sleep(.2); assert counter() > a, 'Disabling paused the running target'
        os.close(fd); fd = None
        graceful_close(process); process = None
        process, pid, window = launch()
        assert not listening(port) and not listening(initial_port)
        graceful_close(process); process = None
        print('PASS disabling preserves running state; disabled preference survives restart', flush=True)

        process, pid, window = launch('--remote-debug', f'0.0.0.0:{port}')
        wait_for(lambda: listening(port)); time.sleep(.15)
        with Client(port=port) as c:
            assert c.status()['state'] == 'paused'
        settings()  # Closing unchanged settings must retain command-line override.
        assert listening(port)
        graceful_close(process); process = None
        assert preferences()['debugger.remote.enabled'] == 'false'
        process, pid, window = launch('--no-remote-debug')
        assert not listening(port) and not listening(initial_port)
        graceful_close(process); process = None
        print('PASS startup overrides and unchanged Qt settings preserve saved preferences', flush=True)
    finally:
        if client: client.close()
        if fd is not None: os.close(fd)
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        if focus: focus.close()
        for log in logs: log.close()


if __name__ == '__main__':
    main()
