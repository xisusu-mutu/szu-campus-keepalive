"""SZU portal client. Standard library only; no browser or driver is required.

Authentication follows the school's own Portal.js / a43.js / a44.js.
Only the two explicitly configured portal hosts may receive requests.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import html
import http.cookiejar
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

APP_TITLE = '深圳大学校园网保活'
DORM_BASE = 'http://172.30.255.42'
TEACHING_BASE = 'https://net.szu.edu.cn'
PORTAL_NAMES = {'teaching': '教学区 / 深澜', 'dorm': '宿舍区 / Dr.COM'}
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / 'data'
SETTINGS_FILE = DATA_DIR / 'settings.json'
CALLBACK = 'campus_client'


class PortalError(Exception):
    """A safe, user-facing error that contains no request credentials."""


class CredentialsError(PortalError):
    """Do not keep retrying a password/captcha/account problem."""


class Cancelled(PortalError):
    pass


def system_time() -> str:
    return datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')


def validate_interval(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError('保活间隔请输入数字，单位为分钟。') from None
    if isinstance(value, bool) or not math.isfinite(number) or not .5 <= number <= 1440:
        raise ValueError('保活间隔须为 0.5～1440 分钟。')
    return number


def retry_delay(failures: int, normal_seconds: float) -> float:
    return min(normal_seconds, 300, 30 * (2 ** min(max(failures - 1, 0), 4)))


def valid_ip(value) -> bool:
    try:
        ip = ipaddress.ip_address(str(value).strip())
        return not (ip.is_unspecified or ip.is_loopback or ip.is_multicast)
    except ValueError:
        return False


def validate_url(url: str) -> None:
    try:
        part = urllib.parse.urlsplit(url)
        origin = (part.scheme, part.hostname, part.port or (443 if part.scheme == 'https' else 80))
    except ValueError:
        raise PortalError('认证网址无效。') from None
    if part.username or part.password or origin not in {
        ('http', '172.30.255.42', 80), ('http', '172.30.255.42', 801),
        ('https', 'net.szu.edu.cn', 443),
    }:
        raise PortalError('已拦截跳向非指定认证站点的请求，未向它发送账号密码。')


def safe_endpoint(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, '', ''))


class Redactor:
    SENSITIVE = {'password', 'upass', 'user_password', 'challenge', 'token',
                 'info', 'chksum', 'cookie', 'set-cookie', 'authorization',
                 'real_name', 'nid', 'user_mac', 'olmac', 'ss4'}

    def __init__(self, secrets=()):
        self.secrets = {str(s) for s in secrets if s}

    def add(self, *values):
        self.secrets.update(str(s) for s in values if s)

    def clean(self, value):
        if isinstance(value, dict):
            return {str(k): '[REDACTED]' if str(k).lower() in self.SENSITIVE else self.clean(v)
                    for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, '[REDACTED]')
            return value[:1500]
        return value


def parse_jsonp(text: str) -> dict:
    text = text.strip().lstrip('\ufeff')
    if not text.startswith('{'):
        match = re.fullmatch(r'[A-Za-z_$][\w.$]*\s*\(\s*(\{.*\})\s*\)\s*;?', text, re.S)
        if not match:
            raise PortalError('认证接口返回格式无效，无法解析 JSON/JSONP。')
        text = match[1]
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        raise PortalError('认证接口返回的数据不完整。') from None
    if not isinstance(value, dict):
        raise PortalError('认证接口的数据格式不符合预期。')
    return value


def js_string(text: str, name: str, default='') -> str:
    match = re.search(r'\b' + re.escape(name) + r'''\s*[:=]\s*(["'])(.*?)\1''', text, re.S)
    return html.unescape(match[2]).strip() if match else default


def parse_teaching_config(page: str) -> dict:
    marker = re.search(r'\b(?:var|let|const)\s+CONFIG\s*=\s*\{', page)
    if not marker:
        raise PortalError('教学区页面未提供本次连接的配置，未采用旧链接的 IP/接入编号。')
    block = page[marker.end():].split('</script>', 1)[0]
    ip, acid = js_string(block, 'ip'), js_string(block, 'acid')
    if not valid_ip(ip) or not acid.isdigit():
        raise PortalError('教学区未返回有效的当前 IP / 接入编号，请先连接校园网。')
    return {'ac_id': acid, 'ip': ip, 'nas': js_string(block, 'nas')}


@dataclass
class Settings:
    username: str = ''
    password: str = field(default='', repr=False)
    interval_minutes: float = 15.0
    mode: str = 'auto'

    def validate(self):
        self.interval_minutes = validate_interval(self.interval_minutes)
        self.username = self.username.strip()
        if self.mode not in ('auto', 'teaching', 'dorm'):
            raise ValueError('认证区域设置无效。')
        if any(c in self.username for c in '\r\n\x00'):
            raise ValueError('账号不能包含换行或空字符。')


@dataclass
class Status:
    portal: str
    online: bool
    account: str
    ip: str
    details: dict


@dataclass
class CheckResult:
    online: bool
    state: str
    message: str
    portal: str = ''
    account: str = ''
    ip: str = ''
    details: dict = field(default_factory=dict)
    checked_at: str = field(default_factory=system_time)
    captures: list = field(default_factory=list)


def parse_status(portal: str, data: dict) -> Status:
    if portal == 'teaching':
        account = str(data.get('user_name') or '')
        ip = str(data.get('online_ip') or data.get('user_ip') or '')
        success = data.get('error') == 'ok'
    else:
        account = str(data.get('uid') or '')
        ip = str(data.get('v4ip') or data.get('v46ip') or data.get('ss5') or '')
        success = data.get('result') in (1, '1', 'ok')
    return Status(portal, bool(success and account and valid_ip(ip)), account, ip, data)


def same_account(actual: str, expected: str) -> bool:
    return re.sub(r'^,[01ab],', '', actual).strip().casefold() == expected.strip().casefold()


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if getattr(req, 'contains_credentials', False):
            raise PortalError('认证请求出现重定向；已阻止转发密码，请在认证网页确认提示。')
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpClient:
    """Capture only our own responses. Never save auth queries or cookies."""
    RESPONSE_FIELDS = {
        'error', 'error_msg', 'suc_msg', 'ecode', 'res', 'result', 'msg', 'ret_code',
        'user_name', 'uid', 'online_ip', 'client_ip', 'user_ip', 'v4ip', 'v46ip',
        'sum_bytes', 'sum_seconds', 'user_balance', 'time', 'flow', 'fee',
        'login_method', 'account_prefix', 'check_online_method', 'en_md5',
    }

    def __init__(self, stop=None, redactor=None, timeout=6):
        self.stop = stop or threading.Event()
        self.redactor = redactor or Redactor()
        self.timeout = timeout
        self.captures = []
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), SafeRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def check_cancel(self):
        if self.stop.is_set():
            raise Cancelled('已停止。')

    def capture(self, endpoint, response, status=200):
        self.captures.append({'time': system_time(), 'endpoint': safe_endpoint(endpoint),
                              'http_status': status, 'response': self.redactor.clean(response)})
        del self.captures[:-50]

    def get(self, url, params=None, credentials=False):
        self.check_cancel()
        validate_url(url)
        if params:
            url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) SZUCampusClient/1.0',
            'Cache-Control': 'no-cache', 'Accept': '*/*',
        })
        request.contains_credentials = credentials
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise PortalError('认证站点的响应过大，已停止解析。')
                encoding = response.headers.get_content_charset()
                if not encoding:
                    match = re.search(br'charset\s*=\s*["\x27]?([\w-]+)', raw[:5000], re.I)
                    encoding = match[1].decode('ascii') if match else 'utf-8'
                try:
                    text = raw.decode(encoding, errors='replace')
                except LookupError:
                    text = raw.decode('utf-8', errors='replace')
                final = response.geturl()
                validate_url(final)
                self.capture(final, {'received': '响应已收到；未保存请求参数或页面全文。'}, response.status)
                return text, final
        except PortalError:
            raise
        except urllib.error.HTTPError as exc:
            self.capture(url, {'error': f'HTTP {exc.code}'}, exc.code)
            raise PortalError(f'认证接口返回 HTTP {exc.code}：{safe_endpoint(url)}') from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            self.capture(url, {'error': '连接失败或超时'})
            raise PortalError(f'连接失败或超时：{safe_endpoint(url)}（检查校园网连接）') from None

    def json(self, url, params=None, credentials=False):
        params = {'callback': CALLBACK, '_': int(time.time() * 1000), **(params or {})}
        text, final = self.get(url, params, credentials)
        result = parse_jsonp(text)
        selected = result.get('data', result)
        if not isinstance(selected, dict):
            selected = result
        safe = {k: v for k, v in selected.items() if k in self.RESPONSE_FIELDS}
        if 'challenge' in result:
            safe['challenge'] = '[REDACTED]'
        self.captures[-1]['response'] = self.redactor.clean(safe)
        return result


def srun_encode(text: str, token: str) -> str:
    """Exact 32-bit xencode + the alphabet used by the portal's Portal.js."""
    if not text:
        return ''
    # JS charCodeAt works with UTF-16 code units (including non-ASCII passwords).
    def words(value, length):
        units = struct.unpack('<' + 'H' * (len(value.encode('utf-16le')) // 2), value.encode('utf-16le'))
        out = []
        for i in range(0, len(units), 4):
            word = 0
            for j in range(4):
                word |= (units[i+j] if i+j < len(units) else 0) << (8*j)
            out.append(word & 0xffffffff)
        if length:
            out.append(len(units))
        return out
    v, key = words(text, True), words(token, False)
    key.extend([0] * max(0, 4-len(key)))
    n, z, total = len(v)-1, v[-1], 0
    for _ in range(6 + 52 // len(v)):
        total = (total + 0x9E3779B9) & 0xffffffff
        e = (total >> 2) & 3
        for p in range(n+1):
            y = v[p+1] if p < n else v[0]
            m = (z >> 5) ^ (y << 2)
            m += (y >> 3) ^ (z << 4) ^ (total ^ y)
            m += key[(p & 3) ^ e] ^ z
            z = v[p] = (v[p] + m) & 0xffffffff
    raw = struct.pack('<' + 'I' * len(v), *v)
    alphabet = 'LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA'
    return '{SRBX1}' + base64.b64encode(raw).decode('ascii').translate(str.maketrans(
        'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/', alphabet))


def srun_parameters(username, password, config, token):
    ip, acid = config['ip'], config['ac_id']
    digest = hmac.new(token.encode(), password.encode(), hashlib.md5).hexdigest()
    info = srun_encode(json.dumps({'username': username, 'password': password, 'ip': ip,
        'acid': acid, 'enc_ver': 'srun_bx1'}, ensure_ascii=False, separators=(',', ':')), token)
    checksum = hashlib.sha1(''.join(token + s for s in (username, digest, acid, ip, '200', '1', info)).encode()).hexdigest()
    return {'action': 'login', 'username': username, 'password': '{MD5}' + digest,
            'os': 'Windows NT', 'name': 'Windows', 'nas_ip': config.get('nas', ''),
            'double_stack': '0', 'chksum': checksum, 'info': info, 'ac_id': acid,
            'ip': ip, 'n': '200', 'type': '1'}


def auth_failure(data: dict, redactor: Redactor):
    messages = [str(data[k]) for k in ('error_msg', 'msg', 'suc_msg', 'error', 'res', 'ret_code', 'ecode')
                if data.get(k) not in (None, '', 'ok')]
    message = redactor.clean('；'.join(messages)) or '认证接口未返回成功结果。'
    # Code meanings are from the school's current pro/js/lang.js, not guesses.
    attention_codes = {
        'E2531': '用户不存在', 'E2533': '密码错误次数超限，请至少等待 5 分钟后再试',
        'E2534': '认证系统暂时禁用该连接', 'E2535': '认证系统已禁用',
        'E2536': '授权已过期', 'E2553': '账号或密码错误',
        'E2606': '用户已禁用', 'E2607': '产品已禁用', 'E2611': '当前设备与账号绑定不匹配',
    }
    for code, meaning in attention_codes.items():
        if code in message.upper():
            raise CredentialsError(f'{code}：{meaning}。自动重试已暂停，请处理后重新开始。')
    if re.search(r'password|passwd|captcha|密码|验证码|账号不存在|用户不存在|账户锁定', message, re.I):
        raise CredentialsError(message + '；自动重试已暂停，请检查账号或在网页处理。')
    raise PortalError(message)


class TeachingPortal:
    name = 'teaching'

    def __init__(self, client):
        self.client = client
        self.last_login_ip = ''

    def status(self):
        return parse_status(self.name, self.client.json(TEACHING_BASE + '/cgi-bin/rad_user_info'))

    def discover(self):
        page, url = self.client.get(TEACHING_BASE + '/')
        for _ in range(3):
            if re.search(r'\b(?:var|let|const)\s+CONFIG\s*=', page):
                return parse_teaching_config(page), page, url
            meta = re.search(r'''<meta\b[^>]*content\s*=\s*(["'])[^"']*url\s*=\s*(.*?)\1''', page, re.I)
            target = urllib.parse.urljoin(url, html.unescape(meta[2]).strip()) if meta else TEACHING_BASE + '/srun_portal_pc'
            validate_url(target)
            page, url = self.client.get(target)
        raise PortalError('无法从教学区的新页面取得当前接入配置。可切换到宿舍区或打开认证网页。')

    def login(self, username, password):
        config, page, page_url = self.discover()
        # Use the configuration file linked by this page, including theme changes.
        scripts = re.findall(r'''<script\b[^>]*src\s*=\s*["']([^"']*creater\.js[^"']*)''', page, re.I)
        if not scripts:
            raise PortalError('教学区页面缺少密码处理配置，未提交登录。')
        creator, _ = self.client.get(urllib.parse.urljoin(page_url, html.unescape(scripts[0])))
        crypto = re.search(r'\bcryptography\s*:\s*(true|false)', creator)
        if not crypto:
            raise PortalError('教学区密码处理配置已变化，请使用网页登录。')
        if crypto[1] == 'true':
            password = base64.b64encode(urllib.parse.quote(password, safe="~!*'()").encode()).decode()
            self.client.redactor.add(password)
        challenge = self.client.json(TEACHING_BASE + '/cgi-bin/get_challenge',
                                     {'username': username, 'ip': config['ip']})
        token = challenge.get('challenge')
        if challenge.get('error') != 'ok' or not isinstance(token, str) or not token:
            auth_failure(challenge, self.client.redactor)
        self.client.redactor.add(token)
        if valid_ip(challenge.get('client_ip')):
            config['ip'] = challenge['client_ip']
        self.last_login_ip = config['ip']
        self.client.capture(page_url, {'current_ip': config['ip'], 'current_ac_id': config['ac_id']})
        params = srun_parameters(username, password, config, token)
        self.client.redactor.add(params['password'], params['info'], params['chksum'])
        result = self.client.json(TEACHING_BASE + '/cgi-bin/srun_portal', params, credentials=True)
        if result.get('error') != 'ok':
            auth_failure(result, self.client.redactor)
        return result


class DormPortal:
    name = 'dorm'

    def __init__(self, client):
        self.client = client
        self.last_login_ip = ''

    def status(self):
        return parse_status(self.name, self.client.json(DORM_BASE + '/drcom/chkstatus'))

    def discover(self):
        page, url = self.client.get(DORM_BASE + '/a79.htm')
        # Query parameters are accepted ONLY from this freshly fetched redirect.
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        pick = lambda *keys: next((query[k][0] for k in keys if query.get(k)), '')
        ip = pick('wlanuserip', 'client_ip', 'userip', 'ip') or js_string(page, 'v46ip') or js_string(page, 'ss5')
        if not valid_ip(ip):
            raise PortalError('宿舍区页面没有提供有效的当前 IP，未使用旧链接中的 IP。')
        mac = pick('wlanusermac', 'usermac', 'umac', 'mac') or js_string(page, 'ss4', '000000000000')
        mac = re.sub(r'[:-]', '', mac)
        acip = pick('wlanacip', 'acip', 'nasip')
        b64 = lambda text: base64.b64encode(text.encode()).decode()
        config = self.client.json(DORM_BASE + ':801/eportal/portal/page/loadConfig', {
            'program_index': '', 'wlan_vlan_id': js_string(page, 'vlanid', '0'),
            'wlan_user_ip': b64(ip), 'wlan_user_ipv6': '', 'wlan_user_ssid': pick('ssid', 'essid'),
            'wlan_user_areaid': '', 'wlan_ac_ip': b64(acip),
            'wlan_ap_mac': '000000000000', 'gw_id': '000000000000',
        }).get('data')
        if not isinstance(config, dict) or 'login_method' not in config:
            raise PortalError('宿舍区没有返回有效的登录方式配置。')
        return {'ip': ip, 'mac': mac, 'acip': acip, 'acname': pick('wlanacname'),
                'config': config, 'page': page}

    def login(self, username, password):
        found = self.discover()
        config = found['config']
        self.last_login_ip = found['ip']
        try:
            method = int(config['login_method'])
        except (ValueError, TypeError):
            raise PortalError('宿舍区登录方式无效。') from None
        self.client.capture(DORM_BASE + '/a79.htm', {'current_ip': found['ip'], 'login_method': method})
        params = {'terminal_type': 1, 'lang': 'zh-cn'}
        if method == 0:
            md5_enabled = str(config.get('en_md5', 0)) == '1'
            if md5_enabled:
                source, _ = self.client.get(DORM_BASE + '/a43.js')
                pid, calg = js_string(source, 'PID'), js_string(source, 'CALG')
                if not pid or not calg:
                    raise PortalError('宿舍区密码摘要配置无法读取。')
                password = hashlib.md5((pid + password + calg).encode()).hexdigest() + calg + pid
                self.client.redactor.add(password)
            params.update({'DDDDD': username, 'upass': password, '0MKKey': '123456',
                           'R1': '', 'R2': '1' if md5_enabled else '', 'R3': '0',
                           'R6': '0', 'para': '00', 'v6ip': ''})
            endpoint = DORM_BASE + '/drcom/login'
        elif method in (1, 9):
            source, _ = self.client.get(DORM_BASE + '/a59.js')
            version = js_string(source, 'jsVersion')
            if not version:
                raise PortalError('宿舍区未返回当前页面协议版本，未提交登录。')
            prefix = ''
            if str(config.get('account_prefix', 1)) == '1':
                prefix = ',b,' if str(config.get('custom_perceive', 0)) == '1' else ',0,'
            params.update({'login_method': method, 'user_account': prefix + username,
                           'user_password': password, 'wlan_user_ip': found['ip'],
                           'wlan_user_ipv6': '', 'wlan_user_mac': found['mac'],
                           'wlan_ac_ip': found['acip'], 'wlan_ac_name': found['acname'],
                           'jsVersion': version})
            endpoint = DORM_BASE + ':801/eportal/portal/login'
        else:
            raise PortalError(f'宿舍区返回了尚未适配的登录方式 {method}，请先用网页登录。')
        result = self.client.json(endpoint, params, credentials=True)
        if result.get('result') not in (1, '1', 'ok'):
            auth_failure(result, self.client.redactor)
        return result


class Engine:
    def __init__(self, portals=None, stop=None, notify=None, verification_wait=1):
        self.stop = stop or threading.Event()
        self.notify = notify or (lambda message: None)
        self.client = HttpClient(self.stop)
        self.portals = portals if portals is not None else [TeachingPortal(self.client), DormPortal(self.client)]
        self.verification_wait = verification_wait

    def check_cancel(self):
        if self.stop.is_set():
            raise Cancelled('已停止。')

    def result(self, online, state, message, status=None):
        safe = self.client.redactor
        details = {k: v for k, v in (status.details if status else {}).items() if k in HttpClient.RESPONSE_FIELDS}
        return CheckResult(online, state, safe.clean(message), status.portal if status else '',
                           status.account if status else '', status.ip if status else '',
                           safe.clean(details), captures=list(self.client.captures))

    def check(self, settings: Settings, allow_login=True):
        self.check_cancel()
        settings.validate()
        self.client.redactor.add(settings.password)
        selected = [p for p in self.portals if settings.mode == 'auto' or p.name == settings.mode]
        available, online_records, problems = [], [], []
        for portal in selected:
            self.check_cancel()
            self.notify(f'检查 {PORTAL_NAMES[portal.name]} 的在线状态…')
            try:
                status = portal.status()
                available.append(portal)
                if status.online:
                    online_records.append(status)
            except Cancelled:
                raise
            except PortalError as exc:
                problems.append(str(exc))
                self.notify(str(exc))
        for status in online_records:
            if not settings.username or same_account(status.account, settings.username):
                return self.result(True, 'online', '已确认当前账号在线。', status)
        if online_records:
            return self.result(False, 'other_account', '当前设备已有其他账号在线。', online_records[0])
        if not allow_login:
            state = 'offline' if available else 'unreachable'
            return self.result(False, state, '未发现当前账号的在线记录。' if available else '两个认证入口均无法确认状态，请检查校园网连接。')
        if not settings.username or not settings.password:
            return self.result(False, 'needs_attention', '请输入并保存账号密码后再开始保活。')
        for portal in available:
            self.check_cancel()
            self.notify(f'尝试 {PORTAL_NAMES[portal.name]}：重新获取当前接入参数并登录…')
            try:
                portal.login(settings.username, settings.password)
                for _ in range(3):
                    if self.stop.wait(self.verification_wait):
                        raise Cancelled('已停止。')
                    status = portal.status()
                    expected_ip = getattr(portal, 'last_login_ip', '')
                    if status.online and same_account(status.account, settings.username):
                        if expected_ip and status.ip != expected_ip:
                            raise PortalError('认证期间网络 IP 发生变化，将在下次检查时重新探测。')
                        return self.result(True, 'online', '登录成功，已通过在线状态接口再次确认。', status)
                problems.append(PORTAL_NAMES[portal.name] + ' 返回登录结果，但尚未发现在线记录。')
            except Cancelled:
                raise
            except CredentialsError as exc:
                return self.result(False, 'needs_attention', str(exc))
            except PortalError as exc:
                problems.append(str(exc))
                self.notify(str(exc))
        return self.result(False, 'offline' if available else 'unreachable',
                           '；'.join(problems) or '未能确认登录成功，请检查网络或打开认证网页。')


def protect_password(value: str, decrypt=False) -> str:
    """DPAPI current-user scope; never fall back to plain text on errors."""
    if not value:
        return ''
    if os.name != 'nt':
        raise ValueError('密码保存使用 Windows 当前用户加密，仅支持 Windows。')
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value, validate=True) if decrypt else value.encode('utf-8')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError('Windows 无法加密/解密密码。请使用原 Windows 账号，或重新输入密码。')
    try:
        result = ctypes.string_at(output.pbData, output.cbData)
        return result.decode('utf-8') if decrypt else base64.b64encode(result).decode('ascii')
    finally:
        kernel.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))


class SettingsStore:
    def __init__(self, path=SETTINGS_FILE):
        self.path = Path(path)

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            obj = json.loads(self.path.read_text(encoding='utf-8'))
            if obj.get('version') != 1:
                raise ValueError('配置版本不受支持。')
            config = Settings(username=str(obj.get('username', '')),
                              password=protect_password(obj.get('password_dpapi', ''), decrypt=True),
                              interval_minutes=obj.get('interval_minutes', 15), mode=obj.get('mode', 'auto'))
            config.validate()
            return config
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ValueError('读取配置失败，请重新填写并保存设置。' +
                             (' ' + str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else '')) from None

    def save(self, config: Settings):
        config.validate()
        obj = {'version': 1, 'username': config.username,
               'password_dpapi': protect_password(config.password),
               'interval_minutes': config.interval_minutes, 'mode': config.mode}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, self.path)


class AutoStart:
    KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
    NAME = 'SZUCampusKeepalive'

    def __init__(self, script=None, executable=None):
        self.script = Path(script or APP_DIR / 'campus_keepalive.py').resolve()
        exe = Path(executable or sys.executable)
        windowed = exe.with_name('pythonw.exe')
        self.executable = windowed if windowed.exists() else exe
        self.command = subprocess.list2cmdline([str(self.executable), str(self.script), '--autostart'])

    def state(self) -> str:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, self.NAME)
            return 'enabled' if value == self.command else 'different'
        except FileNotFoundError:
            return 'disabled'
        except OSError:
            return 'error'

    def set_enabled(self, enabled: bool):
        import winreg
        if enabled and (not self.executable.is_file() or not self.script.is_file()):
            raise OSError('启动所需的 Python 或脚本文件不存在。')
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, self.NAME, 0, winreg.REG_SZ, self.command)
            else:
                try:
                    winreg.DeleteValue(key, self.NAME)
                except FileNotFoundError:
                    pass
        expected = 'enabled' if enabled else 'disabled'
        if self.state() != expected:
            raise OSError('开机启动设置未能生效，请检查 Windows 权限或安全软件。')


class SingleInstance:
    def __init__(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateMutexW(None, False, r'Local\SZUCampusKeepaliveGui')
        if not self.handle:
            raise OSError('无法创建单实例锁。')
        self.already_running = ctypes.get_last_error() == 183

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class KeepaliveWorker:
    """One daemon worker; interruptible waits; no network I/O in the UI thread."""
    def __init__(self, settings, emit, once=False, allow_login=True):
        self.settings, self.emit = settings, emit
        self.once, self.allow_login = once, allow_login
        self.stop_event, self.wake_event = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.run, name='campus-network', daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()

    def check_now(self):
        self.wake_event.set()

    def update(self, settings):
        self.settings = settings
        self.wake_event.set()

    def run(self):
        failures = 0
        try:
            while not self.stop_event.is_set():
                self.wake_event.clear()
                config = self.settings
                self.emit('checking', None)
                engine = Engine(stop=self.stop_event, notify=lambda text: self.emit('log', text))
                result = engine.check(config, allow_login=self.allow_login)
                self.emit('result', result)
                if self.once or result.state == 'needs_attention':
                    break
                normal_interval = result.online or result.state == 'other_account'
                failures = 0 if normal_interval else failures + 1
                seconds = config.interval_minutes * 60 if normal_interval else retry_delay(failures, config.interval_minutes * 60)
                # A changed configuration or manual check wakes the wait immediately.
                self.emit('next', (time.time() + seconds, time.monotonic() + seconds))
                self.wake_event.wait(seconds)
        except Cancelled:
            self.emit('log', '已取消当前检查；没有注销校园网。')
        except Exception as exc:
            # Never dump exception locals/HTTP request URLs, which may contain credentials.
            self.emit('error', f'程序遇到异常（{type(exc).__name__}），本次保活已停止。')
        finally:
            self.emit('done', None)
