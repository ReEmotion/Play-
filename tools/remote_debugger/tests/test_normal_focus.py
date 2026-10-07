"""Live ordinary-start focus regression; reads only the spawned fixture's RAM.

No remote service is enabled. Linux /proc permits observing our own test child.
"""
import argparse
import os
from pathlib import Path
import signal
import struct
import subprocess
import tempfile
import time
from make_test_elf import make_elf
from x11_focus import Focus, app_pid, graceful_close


def find_counter(pid, prefix):
    fd = os.open(f'/proc/{pid}/mem', os.O_RDONLY)
    found = []
    try:
        for row in Path(f'/proc/{pid}/maps').read_text().splitlines():
            fields = row.split()
            if fields[1][0] != 'r' or len(fields) > 5 and not fields[5].startswith(('[heap]', '[anon:')):
                continue
            begin, end = (int(x, 16) for x in fields[0].split('-'))
            # EE RAM is page-aligned; the loaded entry is also page-aligned.
            for address in range(begin, end, 1 << 20):
                chunk = os.pread(fd, min(1 << 20, end - address), address)
                offset = chunk.find(prefix)
                while offset >= 0:
                    location = address + offset
                    if location % 4096 == 0:
                        found.append(location + 0x600)
                    offset = chunk.find(prefix, offset + 1)
        for address in found:
            first = struct.unpack('<I', os.pread(fd, 4, address))[0]
            time.sleep(.1)
            second = struct.unpack('<I', os.pread(fd, 4, address))[0]
            if second > first:
                return fd, address
        raise RuntimeError(f'Cannot locate the running fixture RAM counter ({len(found)} candidate addresses)')
    except BaseException:
        os.close(fd)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='play-normal-focus-'))
    print('QA directory:', root, flush=True)
    data = root / 'Play Data Files'; data.mkdir()
    (data / 'config.xml').write_text('<Config><Preference Name="ui.showexitconfirmation" Type="boolean" Value="false"/><Preference Name="ui.pausewhenfocuslost" Type="boolean" Value="true"/><Preference Name="ui.allowbackgroundrun" Type="boolean" Value="false"/><Preference Name="debugger.remote.enabled" Type="boolean" Value="false"/></Config>')
    elf = root / 'fixture.elf'; make_elf(elf)
    log = (root / 'runtime.log').open('wb')
    process = subprocess.Popen(['systemd-run', '--user', '--scope', '--quiet', '-p', 'MemoryMax=2G', '-p', 'MemorySwapMax=256M', str(Path(args.binary).resolve()), '--elf', str(elf)],
                               env=dict(os.environ, XDG_CONFIG_HOME=str(root), QT_QPA_PLATFORM='xcb'), stdout=log, stderr=log, start_new_session=True)
    focus = None; fd = None
    try:
        time.sleep(1)
        focus = Focus()
        pid = app_pid(process)
        windows = focus.windows(pid)
        assert windows, 'Qt window was not created'
        focus.focus(windows[0])
        # Debugger builds intentionally load ELF paused; ordinary builds run it.
        help_text = subprocess.run([args.binary, '--help'], capture_output=True, text=True, check=True).stdout
        if '--debugger' in help_text:
            focus.key(windows[0], 'r', 4)
            time.sleep(.2)
        fd, address = find_counter(pid, elf.read_bytes()[0x100:0x120])
        counter = lambda: struct.unpack('<I', os.pread(fd, 4, address))[0]
        a = counter(); time.sleep(.25); b = counter(); assert b > a
        focus.focus(focus.dummy)
        a = counter(); time.sleep(.5); b = counter(); assert a == b, (a, b)
        focus.focus(windows[0])
        a = counter(); time.sleep(.25); b = counter(); assert b > a
        print('PASS ordinary launch pauses on focus loss and resumes on return; preference unchanged', flush=True)
        focus.focus(focus.dummy)
        os.close(fd); fd = None
        graceful_close(process)
        focus.close(); focus = None
        assert 'Name="ui.pausewhenfocuslost" Type="boolean" Value="true"' in (data / 'config.xml').read_text()
    finally:
        if fd is not None: os.close(fd)
        if focus: focus.close()
        if process.poll() is None: os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5); log.close()


if __name__ == '__main__':
    main()
