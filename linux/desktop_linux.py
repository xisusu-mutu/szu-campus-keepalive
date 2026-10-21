"""Desktop entry, idempotent panel opener and short-lived browser handoff.

The opener never starts a second worker or changes the saved keepalive/autostart
choices. systemd owns the background process; the desktop owns the browser.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time

from platform_linux import APP_NAME, UNIT_NAME, UNIT_MARKER, atomic_private_write

DESKTOP_MARKER = '# Managed by szu-campus-keepalive desktop installer v1'
DESKTOP_NAME = APP_NAME + '.desktop'
TICKET_SECONDS = 60
TICKET_LIMIT = 16
START_TIMEOUT = 20


class BackendUnavailable(ValueError):
    """The local backend has not started/listened yet (safe to wait or start)."""


class LaunchTickets:
    """Bounded, memory-only, single-use codes; never the permanent UI token."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.pending = {}
        self.lock = threading.Lock()

    def _expire(self, now):
        self.pending = {code: expires for code, expires in self.pending.items() if expires > now}

    def issue(self):
        with self.lock:
            now = self.clock()
            self._expire(now)
            if len(self.pending) >= TICKET_LIMIT:
                del self.pending[next(iter(self.pending))]
            code = secrets.token_urlsafe(32)
            self.pending[code] = now + TICKET_SECONDS
            return code

    def consume(self, code):
        if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', code):
            return False
        with self.lock:
            self._expire(self.clock())
            return self.pending.pop(code, None) is not None


def desktop_quote(value):
    """Freedesktop Exec escaping: string escaping, then command quoting."""
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('桌面快捷方式路径不能包含换行或控制字符。')
    escaped = ''.join('\\' + char if char in '\\"`$' else char for char in value)
    return '"' + escaped.replace('\\', '\\\\').replace('%', '%%') + '"'


def desktop_text(script, executable, paths):
    # env preserves the install's XDG directories without invoking any shell.
    arguments = ['/usr/bin/env', 'XDG_CONFIG_HOME=' + str(paths.config.parent),
                 'XDG_STATE_HOME=' + str(paths.state.parent), str(executable),
                 '-B', str(script), 'open']
    return '\n'.join([
        DESKTOP_MARKER, '[Desktop Entry]', 'Version=1.0', 'Type=Application',
        'Name=SZU 校园网保活', 'Name[en]=SZU Campus Keepalive',
        'Comment=打开本机管理面板；关闭网页后继续后台保活',
        'Exec=' + ' '.join(desktop_quote(value) for value in arguments),
        'Icon=network-wireless', 'Terminal=false', 'StartupNotify=false',
        'Categories=Network;Utility;', 'Keywords=SZU;校园网;保活;', '',
    ])


def shortcut_locations(runner=subprocess.run):
    data_home = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    if not data_home.is_absolute():
        raise ValueError('XDG_DATA_HOME 必须是绝对路径。')
    targets = [data_home / 'applications' / DESKTOP_NAME]
    try:
        result = runner(['xdg-user-dir', 'DESKTOP'], capture_output=True, text=True, timeout=5)
        desktop = Path(result.stdout.strip())
        # Disabled/nonexistent desktop folders are normal on headless systems.
        if (result.returncode == 0 and desktop.is_absolute() and desktop.is_dir()
                and desktop != Path.home() and desktop != Path(desktop.anchor)):
            target = desktop / DESKTOP_NAME
            if target not in targets:
                targets.append(target)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return targets


def install_shortcuts(script, executable, paths, targets=None):
    targets = shortcut_locations() if targets is None else targets
    text = desktop_text(Path(script).resolve(), Path(executable).absolute(), paths)
    # Check all destinations before replacing any existing launcher.
    for target in targets:
        if target.is_symlink():
            raise ValueError('同名快捷方式是符号链接；未覆盖。')
        if target.exists():
            info = target.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 16384 or
                    (os.name == 'posix' and info.st_uid != os.getuid()) or
                    not target.read_text(encoding='utf-8').startswith(DESKTOP_MARKER + '\n')):
                raise ValueError('同名快捷方式已存在且不属于本安装器；未覆盖。')
    for target in targets:
        # Do not chmod a user's shared applications or Desktop directory to 700.
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.campus-desktop-', dir=target.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
                if os.name == 'posix':
                    os.fchmod(stream.fileno(), 0o755)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return targets


def graphical_session():
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def open_browser(url):
    # Keep the browser outside systemd's keepalive cgroup and memory limit.
    opener = shutil.which('xdg-open')
    arguments = [opener, url] if opener else None
    if not arguments:
        opener = shutil.which('gio')
        arguments = [opener, 'open', url] if opener else None
    if not arguments:
        raise ValueError('找不到浏览器打开工具，请安装 xdg-utils 并设置默认浏览器，再点击图标。后台状态未改变。')
    try:
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        try:
            code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Some desktop handlers remain alive with their browser. Do not kill it.
            return True
        if code == 0:
            return True
    except OSError:
        pass
    raise ValueError('后台已就绪，但系统未能打开浏览器。请设置默认浏览器后再点击图标；后台仍可继续运行。')


def open_panel(paths, startup, request, opener=open_browser, clock=time.monotonic, pause=time.sleep):
    if not graphical_session():
        raise ValueError('当前没有图形桌面，不能自动打开浏览器。无桌面主机请使用 run 或 install --start，面板通过 SSH 隧道访问。')

    def ticket():
        return request(paths, '/api/ui-ticket', {}, timeout=1.5)

    try:
        result = ticket()
    except BackendUnavailable:
        unit = paths.unit
        if unit.is_symlink() or not unit.is_file() or not unit.read_text(encoding='utf-8').startswith(UNIT_MARKER + '\n'):
            raise ValueError('尚未安装本项目的后台服务，请在 Linux 程序目录运行 python3 campus_linux.py install --desktop --open。') from None
        # systemd coalesces concurrent starts; never call Controller.start() here.
        startup.command(['systemctl', '--user', 'start', '--no-block', UNIT_NAME])
        deadline = clock() + START_TIMEOUT
        while True:
            try:
                result = ticket()
                break
            except BackendUnavailable:
                if clock() >= deadline:
                    raise ValueError('后台在 20 秒内没有就绪。请检查 keepalive.log 或 journalctl --user -u ' + UNIT_NAME + '；未更改保活和自启设置。') from None
                pause(0.25)
    if (not isinstance(result, dict) or result.get('app') != APP_NAME or
            type(result.get('port')) is not int or not 1024 <= result['port'] <= 65535 or
            not isinstance(result.get('code'), str) or
            not re.fullmatch(r'[A-Za-z0-9_-]{43}', result['code'])):
        raise ValueError('后台返回的面板启动凭证无效；没有打开浏览器。请确认后台与桌面入口版本一致。')
    address = 'http://127.0.0.1:' + str(result['port']) + '/'
    if not opener(address + '#launch=' + result['code']):
        raise ValueError('后台已就绪，但未能打开浏览器；请检查默认浏览器后再次点击图标。')
    return address  # The short-lived code is deliberately absent from logs/stdout.


def report_open_error(paths, message):
    """Make Terminal=false failures visible; never record handoff URLs or secrets."""
    message = str(message)[:1500]
    try:
        stamp = time.strftime('%Y-%m-%d %H:%M:%S %z')
        atomic_private_write(paths.state / 'last_launcher_error.txt', (stamp + '\n' + message + '\n').encode())
    except (OSError, ValueError):
        pass
    if not graphical_session():
        return
    for name, arguments in (
            ('zenity', ['--error', '--no-markup', '--title=校园网保活', '--text=' + message]),
            ('kdialog', ['--title', '校园网保活', '--error', message]),
            ('notify-send', ['--app-name=校园网保活', '--', '无法打开校园网保活面板', message])):
        executable = shutil.which(name)
        if executable:
            try:
                subprocess.Popen([executable, *arguments], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, close_fds=True)
                return
            except OSError:
                continue
