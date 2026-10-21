"""Linux persistence, instance lock and user-service adapter.

Runtime data is outside the checkout. No Windows settings are read here.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time

from cryptography.fernet import Fernet, InvalidToken

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
from campus_core import Settings

APP_NAME = 'szu-campus-keepalive'
UNIT_NAME = APP_NAME + '.service'
UNIT_MARKER = '# Managed by szu-campus-keepalive Linux installer v1'


@dataclass(frozen=True)
class Paths:
    config: Path
    state: Path
    unit: Path

    @classmethod
    def default(cls):
        config_home = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
        state_home = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
        if not config_home.is_absolute() or not state_home.is_absolute():
            raise ValueError('XDG_CONFIG_HOME 和 XDG_STATE_HOME 必须是绝对路径。')
        return cls(config_home / APP_NAME, state_home / APP_NAME,
                   config_home / 'systemd/user' / UNIT_NAME)


def private_directory(path: Path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('私人数据目录不能是符号链接。')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('私人数据路径不是目录。')
    if os.name == 'posix':
        if info.st_uid != os.getuid():
            raise ValueError('私人数据目录不属于当前用户。')
        path.chmod(0o700)


def read_private(path: Path, limit=131072) -> bytes:
    """Refuse symlinks, non-owned files, loose permissions and oversized data."""
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('私人配置不是普通文件。')
        if os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError('私人配置须属于当前用户且权限为 600。')
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('私人配置文件过大。')
        return data


def atomic_private_write(path: Path, data: bytes):
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError('拒绝覆盖符号链接。')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            if os.name == 'posix':
                os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_secret(path: Path, factory) -> bytes:
    """Create exactly once, never replace a key that decrypts existing settings."""
    private_directory(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return read_private(path, 256).strip()
    data = factory()
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    return data


class LinuxSettingsStore:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.path = paths.config / 'settings.json'
        self.key_path = paths.config / 'secret.key'

    def load(self) -> Settings:
        try:
            raw = read_private(self.path)
        except FileNotFoundError:
            return Settings()
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict) or obj.get('version') != 1:
                raise ValueError()
            encrypted = obj.get('password_fernet', '')
            # A missing/wrong key is an error, never grounds to regenerate one.
            password = (Fernet(read_private(self.key_path, 256).strip())
                        .decrypt(encrypted.encode('ascii')).decode('utf-8')) if encrypted else ''
            value = Settings(username=obj['username'], password=password,
                             interval_minutes=obj['interval_minutes'], mode=obj['mode'])
            validate_settings(value)
            return value
        except (ValueError, TypeError, KeyError, AttributeError, OSError, InvalidToken):
            raise ValueError('Linux 配置无法读取或解密，请检查密钥及文件权限。') from None

    def save(self, settings: Settings):
        validate_settings(settings)
        if self.path.exists() and not self.key_path.exists():
            raise ValueError('配置密钥缺失，请恢复 secret.key。')
        key = private_secret(self.key_path, Fernet.generate_key)
        obj = {'version': 1, 'username': settings.username,
               'password_fernet': Fernet(key).encrypt(settings.password.encode()).decode(),
               'interval_minutes': settings.interval_minutes, 'mode': settings.mode}
        atomic_private_write(self.path, json.dumps(obj, ensure_ascii=False, indent=2).encode())


def validate_settings(settings: Settings):
    if not isinstance(settings.username, str) or len(settings.username) > 256:
        raise ValueError('账号格式无效或过长。')
    if not isinstance(settings.password, str) or len(settings.password) > 4096:
        raise ValueError('密码格式无效或过长。')
    if not isinstance(settings.mode, str):
        raise ValueError('认证区域无效。')
    settings.validate()


def management_token(paths: Paths) -> str:
    token = private_secret(paths.config / 'ui.token', lambda: secrets.token_urlsafe(32).encode())
    if len(token) != 43 or any(chr(c) not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in token):
        raise ValueError('管理令牌文件无效。')
    return token.decode('ascii')


class InstanceLock:
    """Advisory lock survives no process; never unlink the locked inode."""
    def __init__(self, path: Path):
        import fcntl
        private_directory(path.parent)
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('实例锁文件不安全。')
            os.fchmod(self.fd, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise ValueError('同一用户的数据目录已有实例运行，请用网页面板或 status 命令。') from None
        except Exception:
            os.close(self.fd)
            self.fd = None
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def systemd_quote(value, expand_dollars=False) -> str:
    value = str(value)
    if '\n' in value or '\r' in value or '\x00' in value:
        raise ValueError('服务路径不能包含换行或空字符。')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if expand_dollars:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def service_text(script: Path, executable: Path, paths: Paths, port: int) -> str:
    return '\n'.join([
        UNIT_MARKER, '[Unit]', 'Description=SZU campus network keepalive (local management UI)',
        'StartLimitIntervalSec=300', 'StartLimitBurst=5', '', '[Service]', 'Type=simple',
        'ExecStart=' + systemd_quote(executable, True) + ' -B ' + systemd_quote(script, True) +
        f' run --port {port} --autostart',
        # WorkingDirectory takes one path, not an ExecStart-style quoted word.
        'WorkingDirectory=' + str(script.parent.parent).replace('%', '%%'),
        'Environment=' + systemd_quote('XDG_CONFIG_HOME=' + str(paths.config.parent)),
        'Environment=' + systemd_quote('XDG_STATE_HOME=' + str(paths.state.parent)),
        'Environment=PYTHONUNBUFFERED=1', 'Environment=PYTHONDONTWRITEBYTECODE=1',
        'Restart=on-failure', 'RestartSec=30', 'TimeoutStopSec=25', 'UMask=0077',
        'NoNewPrivileges=true', 'LimitCORE=0', 'LimitNOFILE=128', 'TasksMax=32',
        'MemoryMax=160M', '', '[Install]', 'WantedBy=default.target', '',
    ])


class AutoStart:
    def __init__(self, paths: Paths, runner=subprocess.run):
        self.paths, self.runner = paths, runner
        self._lock = threading.RLock()
        self._cached, self._checked = None, 0.0

    def command(self, arguments, require_success=True):
        try:
            result = self.runner(arguments, capture_output=True, text=True, timeout=12,
                                 env={**os.environ, 'LC_ALL': 'C', 'SYSTEMD_PAGER': ''})
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('无法连接 systemd 用户管理器；请在正常用户会话中操作。') from None
        if require_success and result.returncode != 0:
            raise ValueError('systemd 操作失败；请检查用户服务权限及 journalctl 日志。')
        return result

    def status(self, refresh=False):
        with self._lock:
            if not refresh and self._cached is not None and time.monotonic() - self._checked < 5:
                return dict(self._cached)
            installed = self.paths.unit.is_file()
            try:
                enabled = self.command(['systemctl', '--user', 'is-enabled', UNIT_NAME], False)
                active = self.command(['systemctl', '--user', 'is-active', UNIT_NAME], False)
                linger = self.command(['loginctl', 'show-user', str(os.getuid()),
                                       '--property=Linger', '--value'], False)
                enabled_state = enabled.stdout.strip()
                has_linger = linger.returncode == 0 and linger.stdout.strip() == 'yes'
                is_enabled = enabled.returncode == 0 and enabled_state == 'enabled'
                value = {'installed': installed, 'enabled': is_enabled, 'linger': has_linger,
                         'boot_enabled': is_enabled and has_linger, 'state': enabled_state or 'unknown',
                         'active': active.stdout.strip(), 'error': ''}
            except ValueError as exc:
                value = {'installed': installed, 'enabled': False, 'linger': False,
                         'boot_enabled': False, 'state': 'unknown', 'active': 'unknown', 'error': str(exc)}
            self._cached, self._checked = value, time.monotonic()
            return dict(value)

    def set_enabled(self, enabled: bool):
        if type(enabled) is not bool:
            raise ValueError('开机启动开关必须是布尔值。')
        if not self.paths.unit.is_file() or not self.paths.unit.read_text().startswith(UNIT_MARKER):
            raise ValueError('请先运行 Linux install 命令安装本项目的用户服务。')
        self.command(['systemctl', '--user', 'enable' if enabled else 'disable', UNIT_NAME])
        value = self.status(refresh=True)
        if value['error'] or value['enabled'] != enabled:
            raise ValueError('启动项状态回读不匹配，请检查 systemd 用户服务。')
        return value

    def install(self, script: Path, executable: Path, port: int):
        unit = self.paths.unit
        if unit.is_symlink() or (unit.exists() and not unit.read_text().startswith(UNIT_MARKER)):
            raise ValueError('同名服务已存在且不属于本安装器；未覆盖。')
        # Only this new/managed unit is written, not other user services.
        unit.parent.mkdir(parents=True, exist_ok=True)
        # Keep a virtualenv's python path; resolve() would follow its symlink out of the venv.
        text = service_text(script.resolve(), executable.absolute(), self.paths, port)
        fd, temporary = tempfile.mkstemp(prefix='campus-verify-', suffix='.service', dir=unit.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(text)
            self.command(['systemd-analyze', '--user', 'verify', temporary])
            os.replace(temporary, unit)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.command(['systemctl', '--user', 'daemon-reload'])
        self._cached = None
