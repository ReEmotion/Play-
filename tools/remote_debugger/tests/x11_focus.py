"""X11 focus/close helpers for the real Qt regression test (ctypes only)."""
import ctypes as C
import ctypes.util
import time


class _Data(C.Union):
    _fields_ = [('b', C.c_char * 20), ('s', C.c_short * 10), ('l', C.c_long * 5)]


class _ClientMessage(C.Structure):
    _fields_ = [('type', C.c_int), ('serial', C.c_ulong), ('send_event', C.c_int),
                ('display', C.c_void_p), ('window', C.c_ulong), ('message_type', C.c_ulong),
                ('format', C.c_int), ('data', _Data)]


class _KeyEvent(C.Structure):
    _fields_ = [('type', C.c_int), ('serial', C.c_ulong), ('send_event', C.c_int),
                ('display', C.c_void_p), ('window', C.c_ulong), ('root', C.c_ulong), ('subwindow', C.c_ulong),
                ('time', C.c_ulong), ('x', C.c_int), ('y', C.c_int), ('x_root', C.c_int), ('y_root', C.c_int),
                ('state', C.c_uint), ('keycode', C.c_uint), ('same_screen', C.c_int)]


class _Event(C.Union):
    _fields_ = [('client', _ClientMessage), ('key', _KeyEvent), ('pad', C.c_long * 24)]


class Focus:
    def __init__(self):
        self.x = C.CDLL(C.util.find_library('X11'))
        specs = {
            'XOpenDisplay': ([C.c_char_p], C.c_void_p),
            'XDefaultRootWindow': ([C.c_void_p], C.c_ulong),
            'XInternAtom': ([C.c_void_p, C.c_char_p, C.c_int], C.c_ulong),
            'XGetWindowProperty': ([C.c_void_p, C.c_ulong, C.c_ulong, C.c_long, C.c_long, C.c_int, C.c_ulong,
                                    C.POINTER(C.c_ulong), C.POINTER(C.c_int), C.POINTER(C.c_ulong), C.POINTER(C.c_ulong), C.POINTER(C.c_void_p)], C.c_int),
            'XFree': ([C.c_void_p], C.c_int),
            'XCreateSimpleWindow': ([C.c_void_p, C.c_ulong, C.c_int, C.c_int, C.c_uint, C.c_uint, C.c_uint, C.c_ulong, C.c_ulong], C.c_ulong),
            'XStoreName': ([C.c_void_p, C.c_ulong, C.c_char_p], C.c_int),
            'XMapWindow': ([C.c_void_p, C.c_ulong], C.c_int),
            'XSetInputFocus': ([C.c_void_p, C.c_ulong, C.c_int, C.c_ulong], C.c_int),
            'XFlush': ([C.c_void_p], C.c_int),
            'XDestroyWindow': ([C.c_void_p, C.c_ulong], C.c_int),
            'XCloseDisplay': ([C.c_void_p], C.c_int),
            'XSendEvent': ([C.c_void_p, C.c_ulong, C.c_int, C.c_long, C.POINTER(_Event)], C.c_int),
            'XKeysymToKeycode': ([C.c_void_p, C.c_ulong], C.c_ubyte),
            'XGetInputFocus': ([C.c_void_p, C.POINTER(C.c_ulong), C.POINTER(C.c_int)], C.c_int),
        }
        for name, (args, result) in specs.items():
            getattr(self.x, name).argtypes, getattr(self.x, name).restype = args, result
        self.display = self.x.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError('X11 is required for focus regression')
        self.root = self.x.XDefaultRootWindow(self.display)
        self.original = C.c_ulong()
        revert = C.c_int()
        self.x.XGetInputFocus(self.display, C.byref(self.original), C.byref(revert))
        self.dummy = self.x.XCreateSimpleWindow(self.display, self.root, 0, 0, 120, 30, 0, 0, 0)
        self.x.XStoreName(self.display, self.dummy, b'Play remote focus QA')
        self.x.XMapWindow(self.display, self.dummy)
        self.x.XFlush(self.display)
        time.sleep(.1)

    def atom(self, name):
        return self.x.XInternAtom(self.display, name.encode(), False)

    def property(self, window, name):
        actual, fmt, n, after, data = C.c_ulong(), C.c_int(), C.c_ulong(), C.c_ulong(), C.c_void_p()
        self.x.XGetWindowProperty(self.display, window, self.atom(name), 0, 65536, False, 0,
                                  C.byref(actual), C.byref(fmt), C.byref(n), C.byref(after), C.byref(data))
        try:
            return list(C.cast(data, C.POINTER(C.c_ulong))[:n.value]) if data.value and fmt.value == 32 else []
        finally:
            if data.value:
                self.x.XFree(data)

    def windows(self, pid):
        return [w for w in self.property(self.root, '_NET_CLIENT_LIST') if self.property(w, '_NET_WM_PID') == [pid]]

    def focus(self, window):
        self.x.XSetInputFocus(self.display, window, 1, 0)
        self.x.XFlush(self.display)
        time.sleep(.15)

    def key(self, window, character, modifiers=0):
        for kind in (2, 3):
            event = _Event()
            event.key.type, event.key.display, event.key.window = kind, self.display, window
            event.key.root, event.key.same_screen = self.root, True
            event.key.state = modifiers
            event.key.keycode = self.x.XKeysymToKeycode(self.display, ord(character) if isinstance(character, str) else character)
            self.x.XSendEvent(self.display, window, False, 1 if kind == 2 else 2, C.byref(event))
        self.x.XFlush(self.display)

    def close_window(self, window):
        event = _Event()
        event.client.type, event.client.display, event.client.window = 33, self.display, window
        event.client.message_type, event.client.format = self.atom('WM_PROTOCOLS'), 32
        event.client.data.l[0] = self.atom('WM_DELETE_WINDOW')
        self.x.XSendEvent(self.display, window, False, 0, C.byref(event))
        self.x.XFlush(self.display)

    def close(self):
        # Returning to the previous app is best effort if it is still alive.
        if self.original.value in self.property(self.root, '_NET_CLIENT_LIST') or self.original.value in (1, self.root):
            self.x.XSetInputFocus(self.display, self.original.value, 1, 0)
        self.x.XDestroyWindow(self.display, self.dummy)
        self.x.XFlush(self.display)
        self.x.XCloseDisplay(self.display)


def app_pid(process):
    """systemd-run --scope may exec Play or fork one immediate child."""
    from pathlib import Path
    if 'Play' in Path(f'/proc/{process.pid}/comm').read_text():
        return process.pid
    children = Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()
    for child in children:
        if Path(f'/proc/{child}/comm').read_text().strip() == 'Play':
            return int(child)
    raise RuntimeError('Cannot find the test emulator PID')


def regression(client, process):
    focus = Focus()
    try:
        windows = focus.windows(app_pid(process))
        if not windows:
            raise RuntimeError('Qt test window missing from X11 client list')
        main = windows[0]
        client.resume()
        before = int(client.status()['frames'], 16)
        focus.focus(focus.dummy)
        time.sleep(.25)
        assert client.status()['state'] == 'running'
        assert int(client.status()['frames'], 16) > before
        client.pause()
        focus.focus(main)
        assert client.status()['state'] == 'paused'
        client.step()
        focus.focus(focus.dummy)
        focus.focus(main)
        assert client.status()['state'] == 'paused'
        client.resume()
        focus.focus(focus.dummy)
        print('PASS actual X11 focus loss/return while running, paused and stepped', flush=True)
    finally:
        focus.close()


def graceful_close(process, timeout=5):
    focus = Focus()
    try:
        windows = focus.windows(app_pid(process))
        for window in windows:
            focus.close_window(window)
        process.wait(timeout=timeout)
        assert process.returncode == 0, process.returncode
        print('PASS Qt window close and remote service shutdown', flush=True)
    finally:
        focus.close()
