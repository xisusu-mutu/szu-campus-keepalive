#!/usr/bin/env python3
"""Linux keepalive daemon, loopback-only UI and management CLI.

The Windows program is left intact; both launchers import outputs/campus_core.py.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import asdict
from datetime import datetime
import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import campus_core as core
import auth_linux
import desktop_linux
from platform_linux import (AutoStart, InstanceLock, LinuxSettingsStore, Paths, UNIT_NAME,
                            atomic_private_write, management_token, private_directory,
                            read_private, validate_settings)

DEFAULT_PORT = 8765
WEB_DIR = Path(__file__).resolve().parent / 'web'


def local_time(epoch=None):
    value = datetime.now().astimezone() if epoch is None else datetime.fromtimestamp(epoch).astimezone()
    return value.strftime('%Y-%m-%d %H:%M:%S %z')


def settings_from_input(obj, previous=None):
    if not isinstance(obj, dict) or set(obj) - {'username', 'password', 'interval_minutes', 'mode', 'clear_password'}:
        raise ValueError('配置字段无效。')
    previous = previous or core.Settings()
    if type(obj.get('clear_password', False)) is not bool:
        raise ValueError('清除密码标记无效。')
    password = obj.get('password', '')
    if not isinstance(password, str):
        raise ValueError('密码必须是文本。')
    if not password and not obj.get('clear_password', False):
        password = previous.password
    value = core.Settings(username=obj.get('username', previous.username), password=password,
                          interval_minutes=obj.get('interval_minutes', previous.interval_minutes),
                          mode=obj.get('mode', previous.mode))
    validate_settings(value)
    return value


class Controller:
    """One active worker; UI lifecycle is independent of keepalive lifecycle."""
    def __init__(self, store, startup, paths, worker_factory=auth_linux.Worker,
                 logger=None, launched_by='manual'):
        self.store, self.startup, self.paths = store, startup, paths
        self.worker_factory = worker_factory
        self.logger = logger or logging.getLogger('campus_linux.null')
        self.lock = threading.RLock()
        self.worker = None
        self.generation = 0
        self.state = 'stopped'
        self.next_check = None
        self.last_result = None
        self.logs = deque(maxlen=200)
        self.config_error = ''
        self.started_at = local_time()
        self.launched_by = launched_by
        self.desired_running = True
        try:
            self.settings = store.load()
        except (OSError, ValueError) as exc:
            self.settings = core.Settings()
            self.config_error = str(exc) if isinstance(exc, ValueError) else '配置读取失败，请检查文件权限。'
        try:
            flag = json.loads(read_private(paths.state / 'run_state.json'))
            if not isinstance(flag, dict) or type(flag.get('keepalive_enabled')) is not bool:
                raise ValueError()
            self.desired_running = flag['keepalive_enabled']
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.desired_running = False
            self.config_error = self.config_error or '保活开关记录损坏，已暂停；请在面板确认后点击开始。'
        if self.config_error:
            self.log(self.config_error)

    def log(self, text):
        with self.lock:
            text = core.Redactor([self.settings.password]).clean(str(text))
            text = text.replace('\r', ' ').replace('\n', ' ')[:1500]
            self.logs.append({'time': local_time(), 'message': text})
            self.logger.info('%s', text)

    def _remember_running(self, enabled):
        atomic_private_write(self.paths.state / 'run_state.json',
                             json.dumps({'keepalive_enabled': enabled}).encode())
        self.desired_running = enabled

    def start(self, once=False):
        with self.lock:
            if self.worker is not None:
                if self.state == 'stopping':
                    raise ValueError('上一轮正在停止，请稍候再开始。')
                self.worker.check_now()
                return
            if not once and (not self.settings.username or not self.settings.password):
                raise ValueError('请先保存校园网账号和密码。')
            if not once:
                self._remember_running(True)
            self.generation += 1
            generation = self.generation
            self.worker = self.worker_factory(
                self.settings, lambda name, value: self._emit(generation, name, value),
                once=once, allow_login=True)
            self.state = 'checking'
            self.next_check = None
            self.log('开始单次检查。' if once else '开始后台保活。')
            try:
                self.worker.start()
            except Exception:
                self.worker = None
                self.state = 'stopped'
                raise

    def stop(self, remember=True):
        with self.lock:
            if remember:
                self._remember_running(False)
            worker = self.worker
            if worker:
                self.state = 'stopping'
                worker.stop()
            else:
                self.state = 'stopped'
            self.next_check = None
            self.log('停止保活。')
            return worker

    def check_now(self):
        self.start(once=True)

    def save_settings(self, values):
        with self.lock:
            value = settings_from_input(values, self.settings)
            self.store.save(value)
            self.settings = value
            self.config_error = ''
            if self.worker is not None and self.state != 'stopping':
                self.worker.update(value)
            self.log('设置已保存；运行中的保活将在当前请求结束后应用新设置。')

    def _emit(self, generation, name, value):
        with self.lock:
            if generation != self.generation:
                return
            if name == 'checking':
                if self.state != 'stopping':
                    self.state = 'checking'
                self.next_check = None
            elif name == 'log':
                self.log(value)
            elif name == 'result':
                self.last_result = core.Redactor([self.settings.password]).clean({
                    field: getattr(value, field) for field in
                    ('online', 'state', 'message', 'portal', 'account', 'ip', 'checked_at')
                })
                self.log(value.message)
                self.log('本轮操作完成，系统时间：' + local_time())
                if value.state == 'needs_attention':
                    try:
                        self._remember_running(False)
                    except (OSError, ValueError):
                        self.desired_running = False
                        self.log('暂停标记无法持久化，请检查磁盘空间和文件权限。')
                    self.log('已暂停自动重试，处理账号提示后请手动开始。')
            elif name == 'next':
                if self.state != 'stopping':
                    self.state = 'waiting'
                    self.next_check = value
                    self.log('下次检查：' + local_time(value[0]))
            elif name == 'error':
                self.log(value)
                self.last_result = {'online': False, 'state': 'error', 'message': value,
                                    'checked_at': local_time()}
                try:
                    self._remember_running(False)
                except (OSError, ValueError):
                    self.desired_running = False
            elif name == 'done':
                self.worker = None
                self.state = 'stopped'
                self.next_check = None

    def snapshot(self):
        # Query systemd outside the worker lock; a slow user bus must not block authentication.
        startup = self.startup.status()
        teaching_client = auth_linux.client_status()
        with self.lock:
            remaining = max(0, self.next_check[1] - time.monotonic()) if self.next_check else None
            return {'system_time': local_time(), 'started_at': self.started_at,
                    'launched_by': self.launched_by, 'run_state': self.state,
                    'keepalive_enabled': self.desired_running,
                    'next_check': local_time(time.time() + remaining) if remaining is not None else None,
                    'remaining_seconds': round(remaining) if remaining is not None else None,
                    'settings': {'username': self.settings.username,
                                 'interval_minutes': self.settings.interval_minutes, 'mode': self.settings.mode},
                    'config_error': self.config_error, 'autostart': startup,
                    'teaching_client': teaching_client,
                    'result': self.last_result, 'logs': list(self.logs)}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 12

    def __init__(self, address, controller, token, shutdown_event):
        if address[0] != '127.0.0.1':
            raise ValueError('管理界面仅允许监听 127.0.0.1。')
        self.controller, self.token, self.shutdown_event = controller, token, shutdown_event
        self.launch_tickets = desktop_linux.LaunchTickets()
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(address, Handler)

    def get_request(self):
        client, address = super().get_request()
        client.settimeout(5)
        return client, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        self.controller.log('管理请求异常已隔离；没有记录请求内容。')


class Handler(BaseHTTPRequestHandler):
    server_version = 'CampusKeepalive'
    sys_version = ''

    def log_message(self, *args):
        pass  # Never write headers, URLs, tokens or request bodies to access logs.

    def respond(self, status, data, content_type='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        self.close_connection = True

    def trusted_request(self):
        hosts = self.headers.get_all('Host', [])
        if len(hosts) != 1 or not re.fullmatch(r'(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?', hosts[0], re.I):
            self.respond(403, {'error': '只接受本机 Host；远程使用 SSH 端口转发。'})
            return False
        origin = self.headers.get('Origin')
        if ((origin is not None and origin != 'http://' + hosts[0]) or
                self.headers.get('Sec-Fetch-Site') == 'cross-site'):
            self.respond(403, {'error': '已拒绝跨站请求。'})
            return False
        return True

    def authenticated(self):
        supplied = self.headers.get('Authorization', '')
        if not secrets.compare_digest(supplied.encode('utf-8'), ('Bearer ' + self.server.token).encode()):
            self.respond(401, {'error': '请先填写管理令牌。'})
            return False
        return True

    def do_GET(self):
        if not self.trusted_request():
            return
        assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
                  '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if self.path in assets:
            filename, content_type = assets[self.path]
            self.respond(200, (WEB_DIR / filename).read_bytes(), content_type)
        elif self.path == '/api/state':
            if self.authenticated():
                self.respond(200, self.server.controller.snapshot())
        else:
            self.respond(404, {'error': '页面不存在。'})

    def do_POST(self):
        if not self.trusted_request():
            return
        # Only a valid, one-use code can bootstrap a desktop tab. All other
        # mutations (including issuing codes) still require the management token.
        if self.path != '/api/ui-connect' and not self.authenticated():
            return
        try:
            lengths = self.headers.get_all('Content-Length', [])
            if (self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1 or
                    not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 16384 or
                    self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json'):
                raise ValueError('需要不超过 16 KiB 的 JSON 请求。')
            payload = self.rfile.read(int(lengths[0]))
            if len(payload) != int(lengths[0]):
                raise ValueError('请求不完整。')
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError('请求必须是 JSON 对象。')
            controller = self.server.controller
            if self.path == '/api/ui-ticket':
                if data:
                    raise ValueError('面板启动请求不接受额外参数。')
                if self.server.shutdown_event.is_set():
                    self.respond(409, {'error': '后台正在退出，请等几秒后再次点击应用图标。'})
                    return
                self.respond(200, {'app': desktop_linux.APP_NAME, 'port': self.server.server_port,
                                   'code': self.server.launch_tickets.issue(),
                                   'expires_in': desktop_linux.TICKET_SECONDS})
                return
            elif self.path == '/api/ui-connect':
                if set(data) != {'code'} or not self.server.launch_tickets.consume(data.get('code')):
                    self.respond(401, {'error': '自动连接凭证已过期或已使用，请再次点击“SZU 校园网保活”图标。'})
                    return
                self.respond(200, {'token': self.server.token})
                return
            elif self.path == '/api/settings':
                controller.save_settings(data)
            elif self.path == '/api/autostart':
                if set(data) != {'enabled'}:
                    raise ValueError('启动项请求无效。')
                controller.startup.set_enabled(data['enabled'])
                controller.log('已更新开机启动设置。')
            elif self.path in ('/api/start', '/api/stop', '/api/check', '/api/exit'):
                if data:
                    raise ValueError('该操作不接受额外参数。')
                if self.path == '/api/start':
                    controller.start()
                elif self.path == '/api/stop':
                    controller.stop()
                elif self.path == '/api/check':
                    controller.check_now()
                else:
                    self.respond(200, {'ok': True, 'message': '正在退出后台程序。'})
                    self.server.shutdown_event.set()
                    return
            else:
                self.respond(404, {'error': '操作不存在。'})
                return
            self.respond(200, {'ok': True})
        except (ValueError, UnicodeError) as exc:
            # JSON decoder text may contain untrusted content; only our fixed messages are returned.
            message = 'JSON 格式无效。' if isinstance(exc, (json.JSONDecodeError, UnicodeError)) else str(exc)
            self.respond(400, {'error': message})
        except (OSError, socket.timeout):
            self.respond(503, {'error': '操作未完成，请检查磁盘、权限或用户服务。'})
        except Exception as exc:
            self.server.controller.log('管理操作失败（' + type(exc).__name__ + '）。')
            self.respond(500, {'error': '操作未完成，请查看日志。'})


def make_logger(paths):
    private_directory(paths.state)
    target = paths.state / 'keepalive.log'
    if target.is_symlink():
        raise ValueError('日志不能是符号链接。')
    logger = logging.getLogger('campus_linux')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(target, maxBytes=524288, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', '%Y-%m-%d %H:%M:%S %z'))
    logger.addHandler(handler)
    return logger


def serve(args, paths):
    os.umask(0o077)
    private_directory(paths.config)
    private_directory(paths.state)
    lock = InstanceLock(paths.state / 'instance.lock')
    server, controller, logger = None, None, None
    shutdown_event = threading.Event()
    try:
        token = management_token(paths)
        logger = make_logger(paths)
        controller = Controller(LinuxSettingsStore(paths), AutoStart(paths), paths, logger=logger,
                                launched_by='systemd' if args.autostart else 'manual')
        server = LocalServer(('127.0.0.1', args.port), controller, token, shutdown_event)
        atomic_private_write(paths.state / 'server.json',
                             json.dumps({'pid': os.getpid(), 'port': args.port}).encode())
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: shutdown_event.set())
        server_thread = threading.Thread(target=server.serve_forever, name='campus-ui', daemon=True)
        server_thread.start()
        controller.log(f'本机管理界面已监听 127.0.0.1:{args.port}；关闭网页不停止保活。')
        if args.paused:
            controller.stop()
        elif (controller.desired_running and not controller.config_error and
              controller.settings.username and controller.settings.password):
            controller.start()
        else:
            controller.log('保活处于暂停/待配置状态，请在面板中开始。')
        shutdown_event.wait()
    finally:
        if controller is not None:
            worker = controller.stop(remember=False)
            if worker is not None:
                worker.thread.join(timeout=12)
        if server is not None:
            # shutdown is called only after serve_forever has started.
            if 'server_thread' in locals():
                server.shutdown()
                server_thread.join(timeout=3)
            server.server_close()
        lock.close()
        if logger:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)


def control_request(paths, endpoint='/api/state', data=None, timeout=35):
    try:
        info = json.loads(read_private(paths.state / 'server.json', 2048))
        port = info['port']
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError()
        token = read_private(paths.config / 'ui.token', 256).decode().strip()
    except FileNotFoundError:
        raise desktop_linux.BackendUnavailable('未找到正在使用的管理端口；请先点击应用图标或启动用户服务。') from None
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('后台端口记录或管理令牌无法读取，请检查私人数据文件及权限。') from None
    request = urllib.request.Request('http://127.0.0.1:' + str(port) + endpoint,
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={'Authorization': 'Bearer ' + token,
                                              'Content-Type': 'application/json'})
    # Never forward the management token, including to another loopback listener.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError('管理端口出现重定向；已拒绝发送令牌。')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read(1048576))
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and endpoint == '/api/ui-ticket':
            raise ValueError('运行中的后台版本不支持桌面自动连接。更新源码后，请显式重启用户服务再打开面板。') from None
        try:
            message = json.loads(exc.read(16384)).get('error', '管理操作失败。')
        except (ValueError, AttributeError):
            message = '管理操作失败。'
        raise ValueError(message) from None
    except (urllib.error.URLError, OSError):
        raise desktop_linux.BackendUnavailable('无法连接后台程序；请用 systemctl --user status ' + UNIT_NAME + ' 检查。') from None


def open_desktop(paths, startup):
    try:
        address = desktop_linux.open_panel(paths, startup, control_request)
    except (OSError, ValueError) as exc:
        message = (str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError)
                   else '打开面板失败，请检查文件权限、用户服务和默认浏览器。')
        desktop_linux.report_open_error(paths, message)
        raise ValueError(message) from None
    print('已请求在默认浏览器打开并自动连接面板：' + address)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='深圳大学校园网保活 Linux 版')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='前台运行后台程序；适合首次测试')
    run.add_argument('--port', type=int, default=DEFAULT_PORT)
    run.add_argument('--paused', action='store_true')
    run.add_argument('--autostart', action='store_true', help=argparse.SUPPRESS)
    commands.add_parser('open', help='桌面入口：按需启动后台，自动打开并连接本机面板')
    install = commands.add_parser('install', help='安装本用户 systemd 服务')
    install.add_argument('--port', type=int, default=DEFAULT_PORT)
    install.add_argument('--enable', action='store_true')
    install.add_argument('--start', action='store_true')
    install.add_argument('--desktop', action='store_true', help='同时安装应用菜单与桌面快捷方式')
    install.add_argument('--open', action='store_true', dest='open_panel', help='安装后打开并自动连接面板（需图形桌面）')
    configure = commands.add_parser('configure', help='配置账号、密码、区域和间隔')
    configure.add_argument('--stdin', action='store_true', help='从标准输入读取 JSON 配置')
    commands.add_parser('token', help='显示本用户的管理令牌')
    for name in ('status', 'start', 'stop', 'check', 'quit'):
        commands.add_parser(name)
    auto = commands.add_parser('autostart', help='查看或修改用户服务启动项')
    auto.add_argument('value', choices=['status', 'on', 'off'], default='status', nargs='?')
    commands.add_parser('probe', help='查询认证状态')
    commands.add_parser('portal-info', help='检查官方 portal 的安装、版本与依赖')
    args = parser.parse_args(argv)
    if hasattr(args, 'port') and not 1024 <= args.port <= 65535:
        parser.error('端口必须是 1024～65535。')
    return args


def main(argv=None):
    args = parse_args(argv)
    if not sys.platform.startswith('linux'):
        raise ValueError('此启动器只用于 Linux；Windows 请运行 outputs/campus_keepalive.py。')
    paths = Paths.default()
    store, startup = LinuxSettingsStore(paths), AutoStart(paths)
    if args.command == 'run':
        serve(args, paths)
    elif args.command == 'open':
        open_desktop(paths, startup)
    elif args.command == 'configure':
        if args.stdin:
            raw = sys.stdin.read(16385)
            if len(raw) > 16384:
                raise ValueError('配置输入过大。')
            obj = json.loads(raw)
        else:
            previous = store.load()
            obj = {'username': input('校园网账号（留空保留）: ').strip() or previous.username,
                   'password': getpass.getpass('校园网密码（留空保留）: '),
                   'interval_minutes': input('保活间隔/分钟（留空保留）: ').strip() or previous.interval_minutes,
                   'mode': input('区域 auto/teaching/dorm（留空保留）: ').strip() or previous.mode}
        # Refuse an offline write while a daemon holds the lock; apply through its controller.
        try:
            lock = InstanceLock(paths.state / 'instance.lock')
        except ValueError as exc:
            if '已有实例' not in str(exc):
                raise
            control_request(paths, '/api/settings', obj)
        else:
            try:
                store.save(settings_from_input(obj, store.load()))
            finally:
                lock.close()
        print('配置已保存。')
    elif args.command == 'install':
        startup.install(Path(__file__), Path(sys.executable), args.port)
        if args.desktop:
            for shortcut in desktop_linux.install_shortcuts(Path(__file__), Path(sys.executable), paths):
                print('已安装应用入口：' + str(shortcut))
        if args.enable:
            startup.set_enabled(True)
        if args.start:
            startup.command(['systemctl', '--user', 'start', UNIT_NAME])
        print('用户服务已安装。开启免登录开机运行还需要 loginctl 的 Linger=yes。')
        print(json.dumps(startup.status(refresh=True), ensure_ascii=False, indent=2))
        if args.open_panel:
            open_desktop(paths, startup)
    elif args.command == 'token':
        print(management_token(paths))
    elif args.command == 'autostart':
        result = startup.status() if args.value == 'status' else startup.set_enabled(args.value == 'on')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == 'probe':
        result = auth_linux.Engine().check(store.load(), allow_login=False)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    elif args.command == 'portal-info':
        print(json.dumps(auth_linux.client_status(), ensure_ascii=False, indent=2))
    else:
        endpoint = '/api/state' if args.command == 'status' else '/api/' + ('exit' if args.command == 'quit' else args.command)
        result = control_request(paths, endpoint, None if args.command == 'status' else {})
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError) as exc:
        print('错误：' + (str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError)
                         else '操作失败，请检查文件、权限、端口和输入格式。'), file=sys.stderr)
        sys.exit(1)
