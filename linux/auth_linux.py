"""Linux authentication: official SZU client for teaching, shared Dr.COM for dorm.

The official executable is installed separately, never bundled or kept alive.
Only this adapter handles its terminal prompts. HTTP status verification and
the desktop implementation remain in the existing shared module.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import campus_core as core

OFFICIAL_GUIDE = 'https://www1.szu.edu.cn/nc/view.asp?id=639'
# Verified against the guide's portal.rar on 2026-10-18 (Linux x86-64).
OFFICIAL_SHA256 = '288555fca83dd8edd3d406dfbc9467640d0fdd4e2989d7d171ba36b82fd67b50'
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
COLOR = r'(?:\x1b\[[0-9;]*m)*'
USERNAME = re.compile(r'Please enter your\s+' + COLOR + r'username', re.I)
PASSWORD = re.compile(r'Please enter your\s+' + COLOR + r'password', re.I)
LOGOUT = re.compile(r'Do you want to sign out\?', re.I)
# PTYs may translate an existing CRLF into CRCRLF; still stop on the first failure.
FAILURE = re.compile(r'Auth failed:' + COLOR + r'\s*([^\r\n]+)\r*\n', re.I)
SUCCESS = re.compile(r'Auth Success!', re.I)


def binary_path():
    return Path.home() / '.local/lib/szu-campus-keepalive/portal'


@lru_cache(maxsize=4)
def _digest(path, signature):
    # The stat signature invalidates the cache after replacement or chmod.
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verified_binary():
    path = binary_path()
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or (os.name == 'posix' and info.st_mode & 0o022):
            raise core.PortalError('官方 portal 必须是普通文件，且不能允许其他用户写入。')
        if os.name == 'posix' and info.st_uid not in (os.getuid(), 0):
            raise core.PortalError('官方 portal 须属于当前用户或 root。')
        if not os.access(path, os.X_OK):
            raise core.PortalError('官方 portal 没有执行权限；请按 Linux README 安装。')
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                     info.st_ctime_ns, info.st_mode)
        if _digest(path, signature) != OFFICIAL_SHA256:
            raise core.PortalError('portal 版本不匹配，请检查来源和版本。')
    except OSError:
        raise core.PortalError('未安装或无法读取官方 portal；请按 Linux README 安装。') from None
    return path


def client_status():
    try:
        verified_binary()
        if importlib.util.find_spec('pexpect') is None:
            raise core.PortalError('缺少 pexpect；请安装 linux/requirements.txt 中的依赖。')
        return {'ready': True, 'name': '学校官方 portal',
                'message': '学校官方 portal 已就绪。'}
    except core.PortalError as exc:
        return {'ready': False, 'name': '学校官方 portal', 'message': str(exc)}


def _auth_failure(text):
    """Classify, but never persist arbitrary subprocess output or credentials."""
    text = ANSI.sub('', text)
    code = re.search(r'E\d{4}', text, re.I)
    if code:
        core.auth_failure({'error_msg': code[0].upper()}, core.Redactor())
    if re.search(r'password|passwd|captcha|account|username|密码|验证码|账号|用户|锁定', text, re.I):
        raise core.CredentialsError('官方程序提示账号、密码或验证问题；已暂停自动重试，请到认证网页确认。')
    raise core.PortalError('官方程序报告认证失败；本轮不重复输入密码，稍后重新检查网络。')


def run_official(binary, url, username, password, stop, *, spawn=None, timeout=45):
    """One bounded interaction. Credentials go only to a non-echoing PTY.

    Enquirer redraws a prompt for every typed character: after answering a
    prompt we stop matching it. Never feed all answers blindly or answer yes.
    Pexpect exception strings include terminal buffers, so never expose them.
    """
    try:
        import pexpect
    except ImportError:
        raise core.PortalError('缺少 pexpect；请安装 linux/requirements.txt 中的依赖。') from None
    if any(ord(c) < 32 or ord(c) == 127 for c in username + password):
        raise core.CredentialsError('账号密码含终端控制字符，未发送；请检查配置。')
    if stop.is_set():
        raise core.Cancelled('已停止。')
    child = None
    deadline = time.monotonic() + timeout
    phase, completed = 'username', ''
    # Do not inherit proxy, Node injection or insecure TLS environment options.
    env = {'HOME': str(Path.home()), 'PATH': os.defpath, 'LANG': 'C.UTF-8',
           'LC_ALL': 'C.UTF-8', 'TERM': 'dumb', 'NO_COLOR': '1', 'FORCE_COLOR': '0'}
    try:
        child = (spawn or pexpect.spawn)(str(binary), [url], env=env, echo=False,
                                         encoding='utf-8', codec_errors='replace',
                                         timeout=.25, maxread=2048, searchwindowsize=8192)
        child.delaybeforesend = 0.05
        while time.monotonic() < deadline:
            if stop.is_set():
                raise core.Cancelled('已停止。')
            patterns = [pexpect.EOF, pexpect.TIMEOUT, FAILURE]
            actions = ['eof', 'wait', 'failure']
            if not completed:
                patterns += [LOGOUT, SUCCESS]
                actions += ['logout', 'success']
            if phase in ('username', 'password'):
                patterns.append(USERNAME if phase == 'username' else PASSWORD)
                actions.append(phase)
            index = child.expect(patterns, timeout=min(.25, max(.01, deadline - time.monotonic())))
            if len(child.before or '') > 32768:
                raise core.PortalError('官方程序输出异常过长，已结束本轮调用。')
            action = actions[index]
            if action == 'wait':
                continue
            if action == 'failure':
                _auth_failure(child.match[1])
            elif action == 'username':
                child.sendline(username)
                phase = 'password'
            elif action == 'password':
                child.sendline(password)
                phase = 'result'
            elif action == 'logout':
                child.sendline('n')
                completed, phase = 'already_online_no_logout', 'done'
            elif action == 'success':
                completed, phase = 'submitted', 'done'
            elif action == 'eof':
                if completed:
                    return completed  # Not proof of login: Engine verifies HTTP status next.
                raise core.PortalError('官方程序提前结束，未确认认证结果；请检查客户端版本及运行环境。')
        raise core.PortalError('官方程序交互超时，已关闭本轮子进程；下次检查会重新尝试。')
    except (OSError, pexpect.ExceptionPexpect):
        raise core.PortalError('无法运行官方 portal；请检查 Linux 架构、依赖和执行权限。') from None
    finally:
        if child is not None:
            child.close(force=True)


class OfficialTeachingPortal(core.TeachingPortal):
    """Keep discovery/status; delegate teaching authentication to SZU's client."""
    def __init__(self, client, runner=run_official):
        super().__init__(client)
        self.runner = runner

    def login(self, username, password):
        binary = verified_binary()
        config, _, _ = self.discover()
        self.last_login_ip = config['ip']
        # Positional URL overrides portal's cached authURL/acid. No old user IP,
        # MAC, success page, external connectivity probe or credentials in argv.
        url = core.TEACHING_BASE + '/srun_portal_pc?ac_id=' + config['ac_id']
        outcome = self.runner(binary, url, username, password, self.client.stop)
        self.client.capture(url, {'client': 'SZU official portal', 'result': outcome,
                                  'current_ac_id': config['ac_id'],
                                  'verification': '随后独立查询在线记录，不以终端输出判定成功。'})
        return {'official_client_result': outcome}


class Engine(core.Engine):
    def __init__(self, portals=None, **kwargs):
        super().__init__(portals=portals, **kwargs)
        if portals is None:
            self.portals = [OfficialTeachingPortal(self.client), core.DormPortal(self.client)]


class Worker(core.KeepaliveWorker):
    """Linux engine selection without editing the user's Windows scheduler.

    The short scheduling loop intentionally mirrors the existing worker;
    shared retry/error/result types keep its behaviour compatible.
    """
    def __init__(self, *args, engine_factory=Engine, **kwargs):
        super().__init__(*args, **kwargs)
        self.engine_factory = engine_factory

    def run(self):
        failures = 0
        try:
            while not self.stop_event.is_set():
                self.wake_event.clear()
                config = self.settings
                self.emit('checking', None)
                engine = self.engine_factory(stop=self.stop_event, notify=lambda text: self.emit('log', text))
                result = engine.check(config, allow_login=self.allow_login)
                self.emit('result', result)
                if self.once or result.state == 'needs_attention':
                    break
                normal_interval = result.online or result.state == 'other_account'
                failures = 0 if normal_interval else failures + 1
                seconds = (config.interval_minutes * 60 if normal_interval else
                           core.retry_delay(failures, config.interval_minutes * 60))
                self.emit('next', (time.time() + seconds, time.monotonic() + seconds))
                self.wake_event.wait(seconds)
        except core.Cancelled:
            self.emit('log', '已取消当前检查；没有注销校园网。')
        except Exception as exc:
            self.emit('error', f'程序遇到异常（{type(exc).__name__}），本次保活已停止。')
        finally:
            self.emit('done', None)
