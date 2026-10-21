"""Offline Linux-adapter and UI tests. No campus authentication or real systemd changes."""
from dataclasses import asdict
import http.client
import json
import logging
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import campus_linux as app
import platform_linux as platform
import campus_core as core


def paths_in(root):
    root = Path(root)
    return platform.Paths(root / 'config', root / 'state', root / 'units' / platform.UNIT_NAME)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = paths_in(self.temporary.name)


class PersistenceTests(TempCase):
    def test_encrypted_round_trip_no_plaintext_password(self):
        store = platform.LinuxSettingsStore(self.paths)
        value = core.Settings('test-account', 'fake-password-123', 2.5, 'auto')
        store.save(value)
        self.assertEqual(asdict(store.load()), asdict(value))
        self.assertNotIn(value.password, store.path.read_text())
        self.assertNotIn('password_dpapi', store.path.read_text())

    def test_new_install_defaults_have_no_credentials(self):
        value = platform.LinuxSettingsStore(self.paths).load()
        self.assertEqual(value.username, '')
        self.assertEqual(value.password, '')
        self.assertFalse(self.paths.config.exists())

    def test_corrupt_config_is_not_overwritten(self):
        store = platform.LinuxSettingsStore(self.paths)
        platform.atomic_private_write(store.path, b'{broken')
        with self.assertRaises(ValueError): store.load()
        self.assertEqual(store.path.read_bytes(), b'{broken')

    def test_missing_key_is_not_regenerated(self):
        store = platform.LinuxSettingsStore(self.paths)
        store.save(core.Settings('test', 'fake-password'))
        original = store.path.read_bytes()
        store.key_path.unlink()
        with self.assertRaises(ValueError): store.load()
        with self.assertRaises(ValueError): store.save(core.Settings('new', 'different'))
        self.assertEqual(store.path.read_bytes(), original)
        self.assertFalse(store.key_path.exists())

    def test_wrong_key_cannot_decrypt(self):
        store = platform.LinuxSettingsStore(self.paths)
        store.save(core.Settings('test', 'fake-password'))
        platform.atomic_private_write(store.key_path, platform.Fernet.generate_key())
        with self.assertRaises(ValueError): store.load()

    def test_token_is_stable_separate_from_password(self):
        token = platform.management_token(self.paths)
        self.assertEqual(len(token), 43)
        self.assertEqual(platform.management_token(self.paths), token)
        self.assertFalse((self.paths.config / 'settings.json').exists())

    def test_malformed_existing_token_not_replaced(self):
        path = self.paths.config / 'ui.token'
        platform.atomic_private_write(path, b'bad')
        with self.assertRaises(ValueError): platform.management_token(self.paths)
        self.assertEqual(path.read_bytes(), b'bad')

    def test_oversize_private_file_rejected(self):
        path = self.paths.state / 'too_big'
        platform.atomic_private_write(path, b'12345')
        with self.assertRaises(ValueError): platform.read_private(path, 4)

    @unittest.skipUnless(os.name == 'posix', 'Linux permissions')
    def test_files_are_owner_only(self):
        store = platform.LinuxSettingsStore(self.paths)
        store.save(core.Settings('test', 'fake-password'))
        self.assertEqual(self.paths.config.stat().st_mode & 0o777, 0o700)
        for path in (store.path, store.key_path):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        store.path.chmod(0o644)
        with self.assertRaises(ValueError): store.load()

    @unittest.skipUnless(os.name == 'posix', 'Linux symlinks')
    def test_symlink_secrets_rejected(self):
        platform.private_directory(self.paths.config)
        target = Path(self.temporary.name) / 'outside'
        target.write_bytes(b'leave-untouched')
        link = self.paths.config / 'secret'
        link.symlink_to(target)
        with self.assertRaises((OSError, ValueError)): platform.read_private(link)
        with self.assertRaises(ValueError): platform.atomic_private_write(link, b'bad')
        self.assertEqual(target.read_bytes(), b'leave-untouched')

    @unittest.skipUnless(os.name == 'posix', 'Linux flock')
    def test_single_instance_and_release(self):
        path = self.paths.state / 'instance.lock'
        first = platform.InstanceLock(path)
        try:
            with self.assertRaises(ValueError): platform.InstanceLock(path)
        finally: first.close()
        second = platform.InstanceLock(path)
        second.close()


class ValidationTests(unittest.TestCase):
    def test_empty_password_keeps_existing(self):
        value = app.settings_from_input({'password': '', 'interval_minutes': 3}, core.Settings('test', 'old'))
        self.assertEqual(value.password, 'old')
        self.assertEqual(value.interval_minutes, 3)

    def test_clear_password_requires_explicit_boolean(self):
        value = app.settings_from_input({'clear_password': True}, core.Settings('test', 'old'))
        self.assertEqual(value.password, '')
        with self.assertRaises(ValueError): app.settings_from_input({'clear_password': 'yes'})

    def test_invalid_types_values_and_unknown_fields(self):
        for value in ({'username': []}, {'password': 4}, {'mode': []}, {'mode': 'bad'},
                      {'interval_minutes': True}, {'interval_minutes': float('nan')},
                      {'interval_minutes': .1}, {'password': 'x' * 4097}, {'unknown': 1}, []):
            with self.subTest(value=type(value)), self.assertRaises(ValueError):
                app.settings_from_input(value)

    def test_port_validation(self):
        self.assertEqual(app.parse_args(['run']).port, 8765)
        with patch('sys.stderr'), self.assertRaises(SystemExit): app.parse_args(['run', '--port', '80'])


class ServiceTests(TempCase):
    def test_unit_does_not_contain_credentials_or_public_bind(self):
        text = platform.service_text(PurePosixPath('/some path/project/linux/campus_linux.py'),
                                     PurePosixPath('/some path/.venv/bin/python'), self.paths, 8765)
        self.assertIn('Restart=on-failure', text)
        self.assertIn('UMask=0077', text)
        self.assertIn('MemoryMax=160M', text)
        self.assertIn('"/some path/.venv/bin/python"', text)
        self.assertIn('WorkingDirectory=/some path/project\n', text)
        self.assertIn('run --port 8765 --autostart', text)
        self.assertIn('WantedBy=default.target', text)
        self.assertNotIn('password', text.lower())
        self.assertNotIn('0.0.0.0', text)
        self.assertNotIn('Restart=always', text)

    def test_systemd_percent_dollar_and_newline_escaping(self):
        self.assertEqual(platform.systemd_quote('/%/$x', True), '"/%%/$$x"')
        self.assertEqual(platform.systemd_quote('XDG_CONFIG_HOME=/a/$b'), '"XDG_CONFIG_HOME=/a/$b"')
        with self.assertRaises(ValueError): platform.systemd_quote('/a\nExecStart=bad')

    def test_installer_does_not_overwrite_unrelated_service(self):
        self.paths.unit.parent.mkdir()
        self.paths.unit.write_text('[Service]\nExecStart=/unrelated')
        startup = platform.AutoStart(self.paths, runner=Mock())
        with self.assertRaises(ValueError): startup.install(Path(__file__), Path(sys.executable), 8765)
        self.assertIn('/unrelated', self.paths.unit.read_text())
        startup.runner.assert_not_called()

    def test_enabled_without_linger_is_not_boot_enabled(self):
        answers = ['enabled\n', 'active\n', 'no\n']
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, text, '') for text in answers])
        with patch.object(platform.os, 'getuid', return_value=1000, create=True):
            status = platform.AutoStart(self.paths, runner).status()
        self.assertTrue(status['enabled'])
        self.assertFalse(status['boot_enabled'])

    def test_enabled_with_linger_is_boot_enabled(self):
        answers = ['enabled\n', 'active\n', 'yes\n']
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, text, '') for text in answers])
        with patch.object(platform.os, 'getuid', return_value=1000, create=True):
            status = platform.AutoStart(self.paths, runner).status()
        self.assertTrue(status['enabled'])
        self.assertTrue(status['boot_enabled'])

    def test_startup_toggle_reads_back_only_our_user_service(self):
        self.paths.unit.parent.mkdir(parents=True)
        self.paths.unit.write_text(platform.UNIT_MARKER + '\n[Service]\n', encoding='utf-8')
        answers = [(0, ''), (0, 'enabled\n'), (0, 'active\n'), (0, 'yes\n'),
                   (0, ''), (1, 'disabled\n'), (0, 'active\n'), (0, 'yes\n')]
        runner = Mock(side_effect=[subprocess.CompletedProcess([], code, text, '')
                                   for code, text in answers])
        startup = platform.AutoStart(self.paths, runner)
        with patch.object(platform.os, 'getuid', return_value=1000, create=True):
            self.assertTrue(startup.set_enabled(True)['boot_enabled'])
            self.assertFalse(startup.set_enabled(False)['boot_enabled'])
        self.assertEqual(runner.call_args_list[0].args[0],
                         ['systemctl', '--user', 'enable', platform.UNIT_NAME])
        self.assertEqual(runner.call_args_list[4].args[0],
                         ['systemctl', '--user', 'disable', platform.UNIT_NAME])

    def test_status_is_cached_and_errors_are_visible(self):
        runner = Mock(side_effect=OSError('not installed'))
        startup = platform.AutoStart(self.paths, runner)
        self.assertTrue(startup.status()['error'])
        self.assertTrue(startup.status()['error'])
        self.assertEqual(runner.call_count, 1)

    @unittest.skipUnless(os.name == 'posix', 'Linux symlinks')
    def test_install_preserves_virtualenv_interpreter_symlink(self):
        exe = Path(self.temporary.name) / 'venv/bin/python'
        exe.parent.mkdir(parents=True)
        exe.symlink_to(sys.executable)
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        platform.AutoStart(self.paths, runner).install(Path(__file__), exe, 8765)
        self.assertIn(str(exe), self.paths.unit.read_text())


class FakeWorker:
    def __init__(self, settings, emit, once=False, allow_login=True):
        self.settings, self.emit, self.once = settings, emit, once
        self.started = self.stopped = False
        self.checks = 0
        self.thread = Mock()
    def start(self): self.started = True
    def stop(self): self.stopped = True
    def check_now(self): self.checks += 1
    def update(self, settings): self.settings = settings


class ControllerCase(TempCase):
    def setUp(self):
        super().setUp()
        self.store = platform.LinuxSettingsStore(self.paths)
        self.store.save(core.Settings('fake-user', 'fake-secret'))
        self.startup = Mock()
        self.startup.status.return_value = {'enabled': False, 'installed': True, 'linger': False,
                                           'boot_enabled': False, 'error': '', 'state': 'disabled', 'active': 'active'}
        self.controller = app.Controller(self.store, self.startup, self.paths, worker_factory=FakeWorker,
                                         logger=Mock())


class ControllerTests(ControllerCase):
    def test_start_is_single_instance_and_repeat_is_check_now(self):
        self.controller.start()
        worker = self.controller.worker
        self.controller.start()
        self.assertIs(self.controller.worker, worker)
        self.assertEqual(worker.checks, 1)

    def test_stop_waits_for_done_before_new_worker(self):
        self.controller.start()
        worker = self.controller.worker
        self.controller.stop()
        self.assertTrue(worker.stopped)
        with self.assertRaises(ValueError): self.controller.start()
        worker.emit('done', None)
        self.controller.start()
        self.assertIsNot(self.controller.worker, worker)

    def test_stale_worker_events_are_ignored(self):
        self.controller.start()
        old = self.controller.worker
        old.emit('done', None)
        self.controller.start()
        current = self.controller.worker
        old.emit('done', None)
        self.assertIs(self.controller.worker, current)

    def test_stop_persists_across_daemon_restart(self):
        self.controller.stop()
        again = app.Controller(self.store, self.startup, self.paths, worker_factory=FakeWorker)
        self.assertFalse(again.desired_running)

    def test_once_check_does_not_turn_on_continuous_keepalive(self):
        self.controller.stop()
        self.controller.check_now()
        self.assertTrue(self.controller.worker.once)
        self.assertFalse(self.controller.desired_running)

    def test_settings_update_applied_without_exposing_password(self):
        self.controller.start()
        self.controller.save_settings({'interval_minutes': 5, 'password': ''})
        self.assertEqual(self.controller.worker.settings.interval_minutes, 5)
        self.assertEqual(self.store.load().password, 'fake-secret')
        encoded = json.dumps(self.controller.snapshot())
        self.assertNotIn('fake-secret', encoded)
        self.assertNotIn('"password":', encoded)

    def test_logs_bounded_and_redacted(self):
        for _ in range(250): self.controller.log('sample fake-secret\nunsafe-line')
        self.assertEqual(len(self.controller.logs), 200)
        self.assertNotIn('fake-secret', json.dumps(list(self.controller.logs)))
        self.assertNotIn('\n', self.controller.logs[-1]['message'])

    def test_result_is_not_persisted_and_login_error_pauses_retries(self):
        self.controller.start()
        result = core.CheckResult(False, 'needs_attention', 'bad fake-secret',
                                  details={'message': 'raw-response-detail'},
                                  captures=[{'password': 'fake-secret'}])
        self.controller.worker.emit('result', result)
        self.assertFalse(self.controller.desired_running)
        self.assertFalse((self.paths.state / 'last_response.json').exists())
        snapshot = self.controller.snapshot()
        encoded = json.dumps(snapshot)
        self.assertNotIn('fake-secret', encoded)
        self.assertNotIn('raw-response-detail', encoded)
        self.assertIn('REDACTED', encoded)
        self.assertNotIn('details', snapshot['result'])
        self.assertNotIn('captures', snapshot['result'])

    def test_online_result_keeps_only_status_in_memory(self):
        self.controller.start()
        result = core.CheckResult(True, 'online', '已确认在线', 'teaching',
                                  'fake-user', '192.0.2.10', {'error': 'ok'},
                                  checked_at='2026-10-18 12:00:00 +0800',
                                  captures=[{'response': 'raw-response'}])
        self.controller.worker.emit('result', result)
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot['result'], {
            'online': True, 'state': 'online', 'message': '已确认在线',
            'portal': 'teaching', 'account': 'fake-user', 'ip': '192.0.2.10',
            'checked_at': '2026-10-18 12:00:00 +0800',
        })
        self.assertNotIn('password_saved', snapshot['settings'])
        self.assertFalse((self.paths.state / 'last_response.json').exists())
        self.assertEqual(self.store.load().password, 'fake-secret')

    def test_other_account_keeps_running_without_any_logout_operation(self):
        self.controller.start()
        self.controller.worker.emit('result', core.CheckResult(False, 'other_account', 'other'))
        self.assertTrue(self.controller.desired_running)
        self.assertFalse(self.controller.worker.stopped)
        self.assertEqual(self.controller.snapshot()['result']['state'], 'other_account')
        self.controller.worker.emit('next', (time.time()+900, time.monotonic()+900))
        self.assertEqual(self.controller.snapshot()['run_state'], 'waiting')
        restarted = app.Controller(self.store, self.startup, self.paths, worker_factory=FakeWorker,
                                   logger=Mock(), launched_by='systemd')
        self.assertTrue(restarted.desired_running)

    def test_one_shot_other_account_does_not_enable_paused_keepalive(self):
        self.controller.stop()
        self.controller.check_now()
        self.controller.worker.emit('result', core.CheckResult(False, 'other_account', 'other'))
        self.controller.worker.emit('done', None)
        self.assertFalse(self.controller.desired_running)
        self.assertEqual(self.controller.state, 'stopped')

    def test_system_time_and_next_check_are_present(self):
        self.controller.start()
        self.controller.worker.emit('next', (time.time()+45, time.monotonic()+45))
        value = self.controller.snapshot()
        self.assertTrue(value['system_time'])
        self.assertTrue(value['next_check'])
        self.assertGreaterEqual(value['remaining_seconds'], 44)
        self.assertLessEqual(value['remaining_seconds'], 45)

    def test_no_credentials_cannot_start_repeated_login(self):
        self.controller.save_settings({'clear_password': True})
        with self.assertRaises(ValueError): self.controller.start()
        self.assertIsNone(self.controller.worker)

    def test_shutdown_does_not_change_saved_keepalive_choice(self):
        self.controller.start()
        self.controller.stop(remember=False)
        self.assertTrue(self.controller.desired_running)


class WebTests(ControllerCase):
    def setUp(self):
        super().setUp()
        self.exit_event = threading.Event()
        self.token = 'fake-ui-token'
        self.server = app.LocalServer(('127.0.0.1', 0), self.controller, self.token, self.exit_event)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path='/api/state', method='GET', body=None, headers=None, auth=True):
        defaults = {'Authorization': 'Bearer '+self.token} if auth else {}
        if body is not None:
            defaults['Content-Type'] = 'application/json'
            if not isinstance(body, str): body = json.dumps(body)
        defaults.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        try:
            connection.request(method, path, body=body, headers=defaults)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally: connection.close()

    def test_public_static_page_no_configuration(self):
        status, headers, body = self.request('/', auth=False)
        self.assertEqual(status, 200)
        self.assertIn('frame-ancestors', headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('fake-user', body)

    def test_status_requires_token(self):
        status, _, body = self.request(auth=False)
        self.assertEqual(status, 401)
        self.assertNotIn('fake-user', body)

    def test_wrong_token_rejected(self):
        status, _, _ = self.request(headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(status, 401)

    def test_authorized_state_never_returns_password(self):
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertNotIn('fake-secret', body)
        self.assertNotIn('"password":', body)

    def test_dns_rebinding_host_rejected(self):
        status, _, _ = self.request(headers={'Host': 'evil.example'})
        self.assertEqual(status, 403)

    def test_cross_origin_post_rejected_even_with_token(self):
        status, _, _ = self.request('/api/start', 'POST', {}, {'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)
        self.assertIsNone(self.controller.worker)

    def test_cross_site_fetch_rejected(self):
        status, _, _ = self.request(headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(status, 403)

    def test_tunneled_local_port_may_differ_from_server_port(self):
        status, _, _ = self.request(headers={'Host': '127.0.0.1:8876', 'Origin': 'http://127.0.0.1:8876'})
        self.assertEqual(status, 200)

    def test_query_string_tokens_are_not_accepted(self):
        status, _, _ = self.request('/api/state?token='+self.token, auth=False)
        self.assertEqual(status, 404)

    def test_private_files_and_traversal_not_served(self):
        for path in ('/../platform_linux.py', '/ui.token', '/settings.json', '/last_response.json'):
            with self.subTest(path=path): self.assertEqual(self.request(path)[0], 404)

    def test_settings_blank_password_preserved(self):
        status, _, _ = self.request('/api/settings', 'POST', {'interval_minutes': 3, 'password': ''})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.load().password, 'fake-secret')
        self.assertEqual(self.store.load().interval_minutes, 3)

    def test_invalid_json_types_and_content_type_rejected(self):
        for body, headers in [('{not-json', {}), ('[]', {}), ('{}', {'Content-Type': 'text/plain'}),
                              ('{}', {'Content-Length': '20000'}), ('{}', {'Transfer-Encoding': 'chunked'})]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request('/api/start', 'POST', body, headers)[0], 400)
        self.assertIsNone(self.controller.worker)

    def test_start_stop_check_do_not_modify_autostart(self):
        self.assertEqual(self.request('/api/start', 'POST', {})[0], 200)
        self.assertEqual(self.request('/api/check', 'POST', {})[0], 200)
        self.assertEqual(self.request('/api/stop', 'POST', {})[0], 200)
        self.startup.set_enabled.assert_not_called()

    def test_checkbox_changes_only_startup_choice(self):
        self.assertEqual(self.request('/api/autostart', 'POST', {'enabled': True})[0], 200)
        self.startup.set_enabled.assert_called_once_with(True)
        self.assertIsNone(self.controller.worker)

    def test_exit_is_explicit_authenticated_post(self):
        self.assertEqual(self.request('/api/exit')[0], 404)
        self.assertFalse(self.exit_event.is_set())
        self.assertEqual(self.request('/api/exit', 'POST', {})[0], 200)
        self.assertTrue(self.exit_event.is_set())

    def test_no_public_bind(self):
        with self.assertRaises(ValueError): app.LocalServer(('0.0.0.0', 0), self.controller, self.token, self.exit_event)

    def test_desktop_ticket_issuance_requires_authentication(self):
        status, _, body = self.request('/api/ui-ticket', 'POST', {}, auth=False)
        self.assertEqual(status, 401)
        self.assertNotIn('code', body)
        self.assertEqual(self.request('/api/ui-ticket')[0], 404)

    def test_shutting_down_backend_does_not_issue_doomed_handoff(self):
        self.exit_event.set()
        status, _, body = self.request('/api/ui-ticket', 'POST', {})
        self.assertEqual(status, 409)
        self.assertIn('后台正在退出', body)
        self.assertEqual(self.server.launch_tickets.pending, {})

    def test_desktop_one_time_handoff_and_replay_rejection(self):
        status, _, body = self.request('/api/ui-ticket', 'POST', {})
        self.assertEqual(status, 200)
        code = json.loads(body)['code']
        self.assertNotIn(self.token, body)
        status, headers, body = self.request('/api/ui-connect', 'POST', {'code': code}, auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(json.loads(body)['token'], self.token)
        status, _, body = self.request('/api/ui-connect', 'POST', {'code': code}, auth=False)
        self.assertEqual(status, 401)
        self.assertNotIn(self.token, body)
        self.assertNotIn(code, body)

    def test_handoff_rejects_expired_or_unknown_code(self):
        clock = Mock(return_value=1)
        self.server.launch_tickets.clock = clock
        code = self.server.launch_tickets.issue()
        clock.return_value = 1000
        for value in (code, 'a'*43, [], None):
            self.assertEqual(self.request('/api/ui-connect', 'POST', {'code': value}, auth=False)[0], 401)

    def test_handoff_rejects_cross_origin_without_consuming_code(self):
        code = self.server.launch_tickets.issue()
        for headers in ({'Origin': 'https://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}, {'Host': 'evil.example'}):
            self.assertEqual(self.request('/api/ui-connect', 'POST', {'code': code}, headers, auth=False)[0], 403)
        self.assertEqual(self.request('/api/ui-connect', 'POST', {'code': code}, auth=False)[0], 200)

    def test_handoff_does_not_accept_codes_in_query_or_get(self):
        code = self.server.launch_tickets.issue()
        self.assertEqual(self.request('/api/ui-connect?code='+code, auth=False)[0], 404)
        self.assertEqual(self.request('/api/ui-connect', auth=False)[0], 404)
        self.assertTrue(self.server.launch_tickets.consume(code))

    def test_handoff_requires_json_and_does_not_accept_extra_fields(self):
        code = self.server.launch_tickets.issue()
        self.assertEqual(self.request('/api/ui-connect', 'POST', {'code': code}, {'Content-Type': 'text/plain'}, auth=False)[0], 400)
        self.assertEqual(self.request('/api/ui-connect', 'POST', {'code': code, 'url': 'https://evil.example'}, auth=False)[0], 401)
        self.assertEqual(self.request('/api/ui-ticket', 'POST', {'extra': True})[0], 400)
        self.assertTrue(self.server.launch_tickets.consume(code))

    def test_desktop_open_preserves_paused_state_and_writes_no_credentials(self):
        self.controller.stop()
        before = (self.paths.state/'run_state.json').read_bytes()
        _, _, body = self.request('/api/ui-ticket', 'POST', {})
        code = json.loads(body)['code']
        self.request('/api/ui-connect', 'POST', {'code': code}, auth=False)
        self.assertIsNone(self.controller.worker)
        self.assertFalse(self.controller.desired_running)
        self.assertEqual((self.paths.state/'run_state.json').read_bytes(), before)
        self.startup.set_enabled.assert_not_called()
        self.assertNotIn(code, str(self.controller.logs))
        self.assertNotIn(self.token, str(self.controller.logs))


if __name__ == '__main__':
    unittest.main()
