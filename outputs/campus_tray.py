"""Windows notification-area icon, serviced by Tk's main-thread message loop.

Native callbacks only enqueue actions. They never enter Tk or do network work.
No third-party tray package or background GUI thread is required.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import struct


WINDOW_CLASS = 'SZUCampusKeepalive.TrayWindow'
WINDOW_TITLE = 'SZUCampusKeepalive.Tray'
TRAY_MESSAGE = 0x8001
RESTORE_MESSAGE = 0x8002
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 1, 2, 4, 0x10, 0x80
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [('Data1', wt.DWORD), ('Data2', wt.WORD), ('Data3', wt.WORD),
                ('Data4', wt.BYTE * 8)]


class NotifyIconData(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('hWnd', wt.HWND), ('uID', wt.UINT),
                ('uFlags', wt.UINT), ('uCallbackMessage', wt.UINT), ('hIcon', wt.HICON),
                ('szTip', wt.WCHAR * 128), ('dwState', wt.DWORD), ('dwStateMask', wt.DWORD),
                ('szInfo', wt.WCHAR * 256), ('uVersion', wt.UINT),
                ('szInfoTitle', wt.WCHAR * 64), ('dwInfoFlags', wt.DWORD),
                ('guidItem', GUID), ('hBalloonIcon', wt.HICON)]


class WindowClass(ctypes.Structure):
    _fields_ = [('style', wt.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int),
                ('cbWndExtra', ctypes.c_int), ('hInstance', wt.HINSTANCE),
                ('hIcon', wt.HICON), ('hCursor', wt.HANDLE), ('hbrBackground', wt.HBRUSH),
                ('lpszMenuName', wt.LPCWSTR), ('lpszClassName', wt.LPCWSTR)]


def _user32():
    api = ctypes.WinDLL('user32', use_last_error=True)
    signatures = {
        'RegisterClassW': ([ctypes.POINTER(WindowClass)], wt.WORD),
        'UnregisterClassW': ([wt.LPCWSTR, wt.HINSTANCE], wt.BOOL),
        'CreateWindowExW': ([wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID], wt.HWND),
        'DefWindowProcW': ([wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], LRESULT),
        'DestroyWindow': ([wt.HWND], wt.BOOL),
        'RegisterWindowMessageW': ([wt.LPCWSTR], wt.UINT),
        'FindWindowW': ([wt.LPCWSTR, wt.LPCWSTR], wt.HWND),
        'PostMessageW': ([wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], wt.BOOL),
        'SetForegroundWindow': ([wt.HWND], wt.BOOL),
        'CreatePopupMenu': ([], wt.HMENU),
        'AppendMenuW': ([wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR], wt.BOOL),
        'TrackPopupMenu': ([wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, wt.HWND, wt.LPVOID], wt.UINT),
        'DestroyMenu': ([wt.HMENU], wt.BOOL),
        'GetCursorPos': ([ctypes.POINTER(wt.POINT)], wt.BOOL),
        'LoadIconW': ([wt.HINSTANCE, wt.LPCWSTR], wt.HICON),
        'CreateIconFromResourceEx': ([ctypes.POINTER(wt.BYTE), wt.DWORD, wt.BOOL,
                                      wt.DWORD, ctypes.c_int, ctypes.c_int, wt.UINT], wt.HICON),
        'DestroyIcon': ([wt.HICON], wt.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def restore_existing_window():
    """Ask the owning Tk app to deiconify itself, even if it was withdrawn."""
    api = _user32()
    window = api.FindWindowW(WINDOW_CLASS, WINDOW_TITLE)
    return bool(window and api.PostMessageW(window, RESTORE_MESSAGE, 0, 0))


class TrayIcon:
    def __init__(self, emit_action):
        self.emit = emit_action
        self.api = _user32()
        self.shell = ctypes.WinDLL('shell32', use_last_error=True)
        self.shell.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NotifyIconData)]
        self.shell.Shell_NotifyIconW.restype = wt.BOOL
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        kernel.GetModuleHandleW.restype = wt.HMODULE
        self.instance = kernel.GetModuleHandleW(None)
        self.hwnd = self.icon = self.atom = None
        self.owns_icon = self.added = self.closed = False
        self.tooltip = '校园网保活 · 尚未检查'
        self.taskbar_created = self.api.RegisterWindowMessageW('TaskbarCreated')
        # Retain the callback for the entire native window lifetime.
        self._callback = WNDPROC(self._window_proc)
        try:
            if not self.taskbar_created:
                raise ctypes.WinError(ctypes.get_last_error())
            self.icon = self._make_icon()
            window_class = WindowClass()
            window_class.lpfnWndProc = self._callback
            window_class.hInstance = self.instance
            window_class.lpszClassName = WINDOW_CLASS
            self.atom = self.api.RegisterClassW(ctypes.byref(window_class))
            if not self.atom:
                raise ctypes.WinError(ctypes.get_last_error())
            # A hidden top-level window receives Explorer's TaskbarCreated broadcast.
            # HWND_MESSAGE windows do not receive this broadcast.
            self.hwnd = self.api.CreateWindowExW(0, WINDOW_CLASS, WINDOW_TITLE, 0,
                                                 0, 0, 0, 0, None, None, self.instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            if not self._add_icon():
                raise OSError('Windows 未能创建托盘图标。')
        except Exception:
            self.close()
            raise

    def _make_icon(self):
        # A blue N tile, encoded as an in-memory 32-bit icon (no image dependency).
        glyph = ('10001', '11001', '11001', '10101', '10011', '10011', '10001')
        pixels = bytearray()
        for y in range(31, -1, -1):
            for x in range(32):
                gx, gy = (x - 8) // 3, (y - 5) // 3
                white = 0 <= gx < 5 and 0 <= gy < 7 and glyph[gy][gx] == '1'
                dx, dy = max(6 - x, x - 25, 0), max(6 - y, y - 25, 0)
                alpha = 0 if dx * dx + dy * dy > 36 else 255
                pixels.extend((255, 255, 255, alpha) if white else (219, 100, 36, alpha))
        header = struct.pack('<IiiHHIIiiII', 40, 32, 64, 1, 32, 0, len(pixels), 0, 0, 0, 0)
        data = header + pixels + bytes(128)
        buffer = (wt.BYTE * len(data)).from_buffer_copy(data)
        icon = self.api.CreateIconFromResourceEx(buffer, len(data), True, 0x00030000, 32, 32, 0)
        if icon:
            self.owns_icon = True
            return icon
        icon = self.api.LoadIconW(None, ctypes.cast(ctypes.c_void_p(32512), wt.LPCWSTR))
        if not icon:
            raise ctypes.WinError(ctypes.get_last_error())
        return icon

    def _data(self, flags=0):
        data = NotifyIconData()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd, data.uID = self.hwnd, 1
        data.uFlags, data.uCallbackMessage = flags, TRAY_MESSAGE
        data.hIcon, data.szTip = self.icon, self.tooltip
        return data

    def _add_icon(self):
        if self.closed:
            return False
        data = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        self.added = bool(self.shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))
        if self.added:
            data.uVersion = 4
            self.shell.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))
        return self.added

    def ensure_visible(self):
        if self.closed:
            return False
        data = self._data(NIF_TIP | NIF_SHOWTIP)
        if self.shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)):
            self.added = True
            return True
        return self._add_icon()

    def update_tooltip(self, text):
        text = text[:127]
        if text == self.tooltip:
            return True
        self.tooltip = text
        return self.ensure_visible()

    def notify(self, text):
        if self.closed:
            return
        data = self._data(NIF_INFO)
        data.szInfoTitle, data.szInfo, data.dwInfoFlags = '校园网保活', text[:255], 1
        self.shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def _menu(self):
        menu = self.api.CreatePopupMenu()
        if not menu:
            return
        commands = {1: 'show', 2: 'start', 3: 'stop', 4: 'exit'}
        try:
            for command, label in ((1, '显示窗口'), (2, '开始保活'), (3, '停止保活'),
                                   (0, None), (4, '退出程序（停止保活）')):
                self.api.AppendMenuW(menu, 0x800 if command == 0 else 0, command, label)
            point = wt.POINT()
            self.api.GetCursorPos(ctypes.byref(point))
            self.api.SetForegroundWindow(self.hwnd)
            # Return a command instead of entering Tk from a native menu callback.
            choice = self.api.TrackPopupMenu(menu, 0x100 | 0x80 | 0x2, point.x, point.y,
                                            0, self.hwnd, None)
            self.api.PostMessageW(self.hwnd, 0, 0, 0)
            if choice in commands:
                self.emit(commands[choice])
        finally:
            self.api.DestroyMenu(menu)

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if not self.closed:
                if message == RESTORE_MESSAGE:
                    self.emit('show')
                    return 0
                if message == TRAY_MESSAGE:
                    event = lparam & 0xffff  # Version 4: high word contains the icon ID.
                    if event in (0x400, 0x401, 0x202, 0x203):
                        self.emit('show')
                    elif event in (0x7b, 0x205):
                        self._menu()
                    return 0
                if message == self.taskbar_created:
                    self.added = False
                    if not self._add_icon():
                        self.emit('unavailable')
                    return 0
                if message == 0x10:  # Close sent to the hidden tray owner.
                    self.emit('exit')
                    return 0
                if message == 0x11:  # WM_QUERYENDSESSION: never block Windows sign-out.
                    return 1
                if message == 0x16 and wparam:
                    self.emit('shutdown')
                    return 0
        except Exception:
            # Python exceptions must never escape a native callback.
            self.emit('unavailable')
        return self.api.DefWindowProcW(hwnd, message, wparam, lparam)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.hwnd:
            self.shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data()))
            self.api.DestroyWindow(self.hwnd)
            self.hwnd = None
        self.added = False
        if self.icon and self.owns_icon:
            self.api.DestroyIcon(self.icon)
        self.icon = None
        if self.atom:
            self.api.UnregisterClassW(WINDOW_CLASS, self.instance)
            self.atom = None
