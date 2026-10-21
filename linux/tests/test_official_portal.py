"""Offline official-client contract tests; never execute the real campus client."""
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import pexpect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auth_linux as auth
import campus_core as core


class FakeChild:
    def __init__(self, events, on_send=None):
        self.events, self.on_send = list(events), on_send
        self.pending, self.before, self.sent = '', '', []
        self.match, self.closed = None, False

    def expect(self, patterns, timeout):
        while True:
            matches = [(m.start(), i, m) for i, p in enumerate(patterns)
                       if hasattr(p, 'search') and (m := p.search(self.pending))]
            if matches:
                _, index, self.match = min(matches, key=lambda x: (x[0], x[1]))
                self.before = self.pending[:self.match.start()]
                self.pending = self.pending[self.match.end():]
                return index
            event = self.events.pop(0) if self.events else pexpect.EOF
            if event in (pexpect.EOF, pexpect.TIMEOUT):
                self.before = self.pending
                return patterns.index(event)
            self.pending += event

    def sendline(self, value):
        self.sent.append(value)
        if self.on_send:
            self.on_send(value)

    def close(self, force=False):
        self.closed = force


class DialogueTests(unittest.TestCase):
    def run_dialogue(self, events, **kwargs):
        child = FakeChild(events)
        spawn = Mock(return_value=child)
        result = auth.run_official('/verified/portal', 'https://net.szu.edu.cn/srun_portal_pc?ac_id=8',
                                   'test-user', 'fake-secret', threading.Event(), spawn=spawn, **kwargs)
        self.assertTrue(child.closed)
        return result, child, spawn

    def test_credentials_only_in_pty_and_redraws_not_answered_twice(self):
        result, child, spawn = self.run_dialogue([
            'Please enter your \x1b[1musername',
            'Please enter your username: t\rPlease enter your username: test-user\n',
            'Please enter your \x1b[1mpassword',
            'Please enter your password: *\rPlease enter your password: ***\n',
            'Auth Success!\r\n', pexpect.EOF])
        self.assertEqual(result, 'submitted')
        self.assertEqual(child.sent, ['test-user', 'fake-secret'])
        self.assertNotIn('fake-secret', repr(spawn.call_args))
        self.assertNotIn('test-user', repr(spawn.call_args))
        self.assertFalse(spawn.call_args.kwargs['echo'])
        for key in ('HTTP_PROXY', 'NODE_OPTIONS', 'NODE_TLS_REJECT_UNAUTHORIZED'):
            self.assertNotIn(key, spawn.call_args.kwargs['env'])

    def test_online_race_only_answers_no_and_ignores_confirmation_redraw(self):
        result, child, _ = self.run_dialogue([
            "You're online\r\n? Do you want to sign out? (y/N)",
            'Do you want to sign out? no\r\n', pexpect.EOF])
        self.assertEqual(result, 'already_online_no_logout')
        self.assertEqual(child.sent, ['n'])

    def test_credential_failure_pauses_and_never_leaks_output(self):
        child = FakeChild(['Please enter your username', 'Please enter your password',
                           'Auth failed:\x1b[0m 错误E2553 fake-secret\r\nPlease enter your username'])
        with self.assertRaises(core.CredentialsError) as raised:
            auth.run_official('/portal', 'url', 'u', 'fake-secret', threading.Event(),
                              spawn=Mock(return_value=child))
        self.assertNotIn('fake-secret', str(raised.exception))
        self.assertEqual(child.sent, ['u', 'fake-secret'])
        self.assertTrue(child.closed)

    def test_transient_error_does_not_loop_in_official_program(self):
        child = FakeChild(['Auth failed: connection timeout\nPlease enter your username'])
        with self.assertRaises(core.PortalError) as raised:
            auth.run_official('/portal', 'url', 'u', 'p', threading.Event(), spawn=lambda *a, **k: child)
        self.assertNotIsInstance(raised.exception, core.CredentialsError)
        self.assertEqual(child.sent, [])
        self.assertTrue(child.closed)

    def test_credential_failure_accepts_terminal_line_endings(self):
        for ending in ('\n', '\r\n', '\r\r\n'):
            child = FakeChild(['Auth failed: E2553 fake-secret' + ending,
                               'Please enter your username'])
            with self.subTest(ending=repr(ending)), self.assertRaises(core.CredentialsError):
                auth.run_official('/portal', 'url', 'u', 'fake-secret', threading.Event(),
                                  spawn=lambda *a, **k: child)
            self.assertEqual(child.sent, [])
            self.assertTrue(child.closed)

    def test_early_eof_is_not_success(self):
        with self.assertRaises(core.PortalError):
            self.run_dialogue(['Portal exit!', pexpect.EOF])

    def test_terminal_controls_rejected_before_spawn(self):
        for value in ('x\ny', 'x\ry', 'x\x00y', 'x\x1by', 'x\x7fy'):
            spawn = Mock()
            with self.subTest(value=repr(value)), self.assertRaises(core.CredentialsError):
                auth.run_official('/portal', 'url', 'u', value, threading.Event(), spawn=spawn)
            spawn.assert_not_called()

    def test_cancel_does_not_enter_credentials(self):
        stop = threading.Event()
        child = FakeChild(['Please enter your username', 'Please enter your password'],
                          on_send=lambda _: stop.set())
        with self.assertRaises(core.Cancelled):
            auth.run_official('/portal', 'url', 'u', 'p', stop, spawn=lambda *a, **k: child)
        self.assertEqual(child.sent, ['u'])
        self.assertTrue(child.closed)

    def test_pre_cancel_never_spawns(self):
        stop, spawn = threading.Event(), Mock()
        stop.set()
        with self.assertRaises(core.Cancelled):
            auth.run_official('/portal', 'url', 'u', 'p', stop, spawn=spawn)
        spawn.assert_not_called()

    def test_oversized_output_is_bounded(self):
        with self.assertRaises(core.PortalError):
            self.run_dialogue(['x' * 33000, pexpect.EOF])

    def test_pexpect_exception_terminal_buffer_is_not_exposed(self):
        with self.assertRaises(core.PortalError) as raised:
            auth.run_official('/portal', 'url', 'u', 'p', threading.Event(),
                              spawn=Mock(side_effect=pexpect.ExceptionPexpect('secret-terminal-output')))
        self.assertNotIn('secret-terminal-output', str(raised.exception))


class BinaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'portal'
        self.path.write_bytes(b'fake-official-binary')
        self.path.chmod(0o700)
        self.patcher = patch.object(auth, 'binary_path', return_value=self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        auth._digest.cache_clear()

    def test_only_verified_digest_accepted_and_changes_invalidate_cache(self):
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch.object(auth, 'OFFICIAL_SHA256', digest):
            self.assertEqual(auth.verified_binary(), self.path)
            self.path.write_bytes(b'replaced-with-a-different-file')
            with self.assertRaises(core.PortalError): auth.verified_binary()

    def test_unknown_binary_not_executed(self):
        with self.assertRaises(core.PortalError): auth.verified_binary()

    def test_missing_binary_is_reported_without_execution(self):
        self.path.unlink()
        self.assertFalse(auth.client_status()['ready'])

    @unittest.skipUnless(os.name == 'posix', 'Linux executable permissions')
    def test_group_writable_or_nonexecutable_files_rejected(self):
        for mode in (0o770, 0o707, 0o600):
            self.path.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(core.PortalError):
                auth.verified_binary()

    @unittest.skipUnless(os.name == 'posix', 'Linux symlinks')
    def test_symlink_binary_rejected(self):
        target = self.path.with_name('target')
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(core.PortalError): auth.verified_binary()


class AdapterTests(unittest.TestCase):
    def test_official_url_uses_fresh_discovered_ac_id_not_saved_success_url(self):
        client, runner = core.HttpClient(), Mock(return_value='submitted')
        portal = auth.OfficialTeachingPortal(client, runner=runner)
        portal.discover = Mock(return_value=({'ip': '172.20.4.8', 'ac_id': '19'}, '',
                                             'https://net.szu.edu.cn/srun_portal_success?ac_id=2'))
        with patch.object(auth, 'verified_binary', return_value=Path('/verified/portal')):
            portal.login('test', 'fake-secret')
        self.assertEqual(runner.call_args.args[1], 'https://net.szu.edu.cn/srun_portal_pc?ac_id=19')
        self.assertEqual(portal.last_login_ip, '172.20.4.8')
        self.assertNotIn('fake-secret', repr(client.captures))

    def test_linux_engines_use_official_teaching_and_existing_dorm(self):
        engine = auth.Engine()
        self.assertIsInstance(engine.portals[0], auth.OfficialTeachingPortal)
        self.assertIsInstance(engine.portals[1], core.DormPortal)

    def test_online_does_not_start_official_binary(self):
        portal = Mock(name='portal'); portal.name = 'teaching'
        portal.status.return_value = core.Status('teaching', True, 'test', '172.20.4.8', {})
        result = auth.Engine(portals=[portal]).check(core.Settings('test', 'fake-secret'))
        self.assertTrue(result.online)
        portal.login.assert_not_called()

    def test_stdout_success_is_not_proof_of_login(self):
        portal = Mock(name='portal'); portal.name = 'teaching'; portal.last_login_ip = ''
        portal.status.return_value = core.Status('teaching', False, '', '', {})
        result = auth.Engine(portals=[portal], verification_wait=0).check(core.Settings('test', 'fake-secret'))
        self.assertFalse(result.online)
        portal.login.assert_called_once()

    def test_missing_official_client_still_allows_dorm(self):
        teaching = Mock(name='teaching'); teaching.name = 'teaching'
        teaching.status.return_value = core.Status('teaching', False, '', '', {})
        teaching.login.side_effect = core.PortalError('not installed')
        dorm = Mock(name='dorm'); dorm.name = 'dorm'; dorm.last_login_ip = '172.20.4.8'
        dorm.status.side_effect = [core.Status('dorm', False, '', '', {}),
                                  core.Status('dorm', True, 'test', '172.20.4.8', {})]
        result = auth.Engine(portals=[teaching, dorm], verification_wait=0).check(core.Settings('test', 'fake-secret'))
        self.assertTrue(result.online)
        self.assertEqual(result.portal, 'dorm')

    def test_probe_never_runs_official_client(self):
        portal = Mock(name='portal'); portal.name = 'teaching'
        portal.status.return_value = core.Status('teaching', False, '', '', {})
        auth.Engine(portals=[portal]).check(core.Settings('test', 'fake-secret'), allow_login=False)
        portal.login.assert_not_called()


class WorkerTests(unittest.TestCase):
    def test_login_waits_until_other_account_goes_offline(self):
        for name in ('teaching', 'dorm'):
            with self.subTest(portal=name):
                portal = Mock()
                portal.name = name
                portal.last_login_ip = ''
                portal.status.side_effect = [
                    core.Status(name, True, 'someone', '192.0.2.10', {}),
                    core.Status(name, True, 'someone', '192.0.2.10', {}),
                    core.Status(name, False, '', '', {}),
                    core.Status(name, True, 'test', '192.0.2.10', {}),
                ]
                events, login_counts = [], []
                def emit(event, value):
                    events.append((event, value))
                    if event == 'result':
                        login_counts.append(portal.login.call_count)
                    if event == 'next' and len(login_counts) == 3:
                        worker.stop()
                def make_engine(**kwargs):
                    return auth.Engine(portals=[portal], verification_wait=0, **kwargs)
                worker = auth.Worker(core.Settings('test', 'fake-secret', 3), emit,
                                     engine_factory=make_engine)
                with patch.object(worker.wake_event, 'wait', return_value=False), \
                        patch.object(auth.time, 'time', return_value=10000.0), \
                        patch.object(auth.time, 'monotonic', return_value=1000.0):
                    worker.run()
                self.assertEqual([v.state for n, v in events if n == 'result'],
                                 ['other_account', 'other_account', 'online'])
                self.assertEqual(login_counts, [0, 0, 1])
                self.assertEqual([v for n, v in events if n == 'next'], [(10180.0, 1180.0)] * 3)
                portal.logout.assert_not_called()

    def test_other_account_one_shot_does_not_start_periodic_checks(self):
        events, factory = [], Mock()
        factory.return_value.check.return_value = core.CheckResult(False, 'other_account', 'other')
        worker = auth.Worker(core.Settings('test', 'fake-secret'),
                             lambda n, v: events.append(n), once=True, engine_factory=factory)
        worker.run()
        self.assertEqual(events, ['checking', 'result', 'done'])

    def test_other_account_waits_for_configured_keepalive_interval(self):
        events, factory = [], Mock()
        factory.return_value.check.return_value = core.CheckResult(False, 'other_account', 'other')
        def emit(name, value):
            events.append((name, value))
            if name == 'next':
                worker.stop()
        worker = auth.Worker(core.Settings('test', 'fake-secret', 2.5), emit,
                             engine_factory=factory)
        with patch.object(auth.time, 'time', return_value=10000.0), \
                patch.object(auth.time, 'monotonic', return_value=1000.0):
            worker.run()
        self.assertIn(('next', (10150.0, 1150.0)), events)
        self.assertEqual(factory.return_value.check.call_count, 1)
        self.assertEqual(events[-1][0], 'done')

    def test_one_shot_uses_linux_engine_and_emits_done(self):
        factory, emit = Mock(), Mock()
        factory.return_value.check.return_value = core.CheckResult(True, 'online', 'ok')
        worker = auth.Worker(core.Settings('test', 'fake'), emit, once=True, engine_factory=factory)
        worker.run()
        self.assertEqual([c.args[0] for c in emit.call_args_list], ['checking', 'result', 'done'])
        factory.return_value.check.assert_called_once_with(worker.settings, allow_login=True)

    def test_attention_does_not_schedule_repeated_password_attempts(self):
        factory, emit = Mock(), Mock()
        factory.return_value.check.return_value = core.CheckResult(False, 'needs_attention', 'fix credentials')
        auth.Worker(core.Settings('test', 'fake'), emit, engine_factory=factory).run()
        self.assertNotIn('next', [c.args[0] for c in emit.call_args_list])

    def test_regular_interval_can_be_stopped_without_waiting(self):
        events, factory = [], Mock()
        factory.return_value.check.return_value = core.CheckResult(True, 'online', 'ok')
        def emit(name, value):
            events.append((name, value))
            if name == 'next': worker.stop()
        worker = auth.Worker(core.Settings('test', 'fake', 3), emit, engine_factory=factory)
        started = time.monotonic()
        worker.run()
        next_time = next(v[1] for n, v in events if n == 'next')
        self.assertAlmostEqual(next_time - started, 180, delta=1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(events[-1][0], 'done')


@unittest.skipUnless(sys.platform.startswith('linux'), 'Real Linux PTY; fake offline program only')
class PtyTests(unittest.TestCase):
    SCRIPT = r'''
import sys, time
def prompt(text):
    print(text, end='', flush=True)
    return sys.stdin.readline().strip()
mode = sys.argv[1]
if mode == 'online':
    reply = prompt('Do you want to sign out? (y/N) ')
    assert reply == 'n'
elif mode == 'hang':
    time.sleep(8)
else:
    user = prompt('Please enter your \x1b[1musername')
    print('Please enter your username: t\rPlease enter your username: test-user')
    password = prompt('Please enter your \x1b[1mpassword')
    assert user == 'test-user' and password == 'fake-secret'
    if mode == 'failure':
        print('Auth failed: E2553 fake-secret\r\nPlease enter your username', flush=True)
        time.sleep(8)
    else:
        print('Auth Success!', flush=True)
'''

    def call_fake(self, mode, timeout=2):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'fake.py'
            path.write_text(self.SCRIPT, encoding='utf-8')
            children = []
            def spawn(command, args, **kwargs):
                child = pexpect.spawn(sys.executable, ['-u', str(path), mode], **kwargs)
                children.append(child)
                return child
            try:
                return auth.run_official('/fake/portal', 'url', 'test-user', 'fake-secret',
                                         threading.Event(), spawn=spawn, timeout=timeout)
            finally:
                for child in children:
                    self.assertFalse(child.isalive(), 'official child must be reaped')

    def test_actual_pty_redraw_and_login(self):
        self.assertEqual(self.call_fake('login'), 'submitted')

    def test_actual_pty_no_logout(self):
        self.assertEqual(self.call_fake('online'), 'already_online_no_logout')

    def test_actual_pty_bad_password_pauses_and_reaps_child(self):
        with self.assertRaises(core.CredentialsError): self.call_fake('failure')

    def test_actual_pty_timeout_reaps_child(self):
        with self.assertRaises(core.PortalError): self.call_fake('hang', timeout=.3)


if __name__ == '__main__':
    unittest.main()
