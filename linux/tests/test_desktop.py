"""Offline desktop-launch tests; no real user-service changes or campus requests."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import campus_linux as app
import desktop_linux as desktop
import platform_linux as platform


class TicketTests(unittest.TestCase):
    def test_ticket_is_random_and_single_use(self):
        tickets = desktop.LaunchTickets()
        first, second = tickets.issue(), tickets.issue()
        self.assertNotEqual(first, second)
        self.assertRegex(first, r'^[A-Za-z0-9_-]{43}$')
        self.assertTrue(tickets.consume(first))
        self.assertFalse(tickets.consume(first))
        self.assertTrue(tickets.consume(second))

    def test_ticket_expires_at_deadline(self):
        clock = Mock(return_value=10)
        tickets = desktop.LaunchTickets(clock)
        code = tickets.issue()
        clock.return_value = 10 + desktop.TICKET_SECONDS
        self.assertFalse(tickets.consume(code))

    def test_ticket_cache_is_bounded_and_old_codes_are_evicted(self):
        tickets = desktop.LaunchTickets()
        codes = [tickets.issue() for _ in range(desktop.TICKET_LIMIT + 3)]
        self.assertEqual(len(tickets.pending), desktop.TICKET_LIMIT)
        self.assertFalse(tickets.consume(codes[0]))
        self.assertTrue(tickets.consume(codes[-1]))

    def test_bad_shapes_are_rejected(self):
        tickets = desktop.LaunchTickets()
        for value in (None, [], {}, 1, '', 'x' * 10000, '\n' * 43):
            self.assertFalse(tickets.consume(value))

    def test_concurrent_redemption_only_succeeds_once(self):
        tickets = desktop.LaunchTickets()
        code = tickets.issue()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(tickets.consume, [code] * 16))
        self.assertEqual(results.count(True), 1)


class DesktopCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='campus-desktop-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = platform.Paths(self.root/'config home'/platform.APP_NAME,
                                    self.root/'state home'/platform.APP_NAME,
                                    self.root/'config home/systemd/user'/platform.UNIT_NAME)


class ShortcutTests(DesktopCase):
    def test_entry_uses_open_not_run_and_has_no_shell_or_credentials(self):
        text = desktop.desktop_text(PurePosixPath('/some path/linux/campus_linux.py'),
                                    PurePosixPath('/some path/.venv/bin/python'), self.paths)
        self.assertIn('"/some path/.venv/bin/python"', text)
        self.assertIn('"/some path/linux/campus_linux.py" "open"', text)
        self.assertIn('Terminal=false', text)
        self.assertIn('XDG_CONFIG_HOME=', text)
        for forbidden in ('sh -c', ' run ', 'ui.token', 'password', ' --enable', '0.0.0.0'):
            self.assertNotIn(forbidden, text)

    def test_exec_escaping_and_invalid_control_characters(self):
        self.assertEqual(desktop.desktop_quote('/a path/100%'), '"/a path/100%%"')
        self.assertEqual(desktop.desktop_quote('$`"\\'), '"' + '\\\\$\\\\`\\\\"\\\\\\\\' + '"')
        for value in ('/a\nb', '/a\rb', '/a\tb', '/a\x00b'):
            with self.assertRaises(ValueError): desktop.desktop_quote(value)

    def test_install_is_idempotent_and_preserves_interpreter_path(self):
        target = self.root/'applications'/desktop.DESKTOP_NAME
        executable = self.root/'venv/bin/python'
        desktop.install_shortcuts(Path(__file__), executable, self.paths, [target])
        first = target.read_bytes()
        desktop.install_shortcuts(Path(__file__), executable, self.paths, [target])
        self.assertEqual(target.read_bytes(), first)
        self.assertIn(desktop.desktop_quote(executable.absolute()), first.decode())

    def test_all_existing_shortcuts_checked_before_any_replacement(self):
        first, other = self.root/'new.desktop', self.root/'other.desktop'
        other.write_text('[Desktop Entry]\nName=Other app\n')
        with self.assertRaises(ValueError):
            desktop.install_shortcuts(Path(__file__), Path(sys.executable), self.paths, [first, other])
        self.assertFalse(first.exists())
        self.assertIn('Other app', other.read_text())

    @unittest.skipUnless(os.name == 'posix', 'Linux filesystem permissions')
    def test_install_preserves_shared_directory_permissions(self):
        directory = self.root/'applications'
        directory.mkdir(mode=0o755)
        target = directory/desktop.DESKTOP_NAME
        desktop.install_shortcuts(Path(__file__), Path(sys.executable), self.paths, [target])
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    @unittest.skipUnless(os.name == 'posix', 'Linux symlinks')
    def test_symlink_shortcut_is_never_overwritten(self):
        outside, link = self.root/'outside', self.root/'launcher.desktop'
        outside.write_text('keep')
        link.symlink_to(outside)
        with self.assertRaises(ValueError):
            desktop.install_shortcuts(Path(__file__), Path(sys.executable), self.paths, [link])
        self.assertEqual(outside.read_text(), 'keep')

    def test_headless_installs_menu_entry_without_inventing_desktop(self):
        runner = Mock(side_effect=FileNotFoundError())
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root/'data')}):
            locations = desktop.shortcut_locations(runner)
        self.assertEqual(locations, [self.root/'data/applications'/desktop.DESKTOP_NAME])

    def test_xdg_desktop_directory_and_custom_data_home(self):
        folder = self.root/'桌面 folder'
        folder.mkdir()
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, str(folder)+'\n'))
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root/'data')}):
            locations = desktop.shortcut_locations(runner)
        self.assertEqual(locations[-1], folder/desktop.DESKTOP_NAME)

    def test_disabled_desktop_directory_does_not_clutter_home(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, str(Path.home())+'\n'))
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root/'data')}):
            self.assertEqual(len(desktop.shortcut_locations(runner)), 1)

    def test_relative_data_home_is_rejected(self):
        with patch.dict(os.environ, {'XDG_DATA_HOME': 'relative'}), self.assertRaises(ValueError):
            desktop.shortcut_locations(Mock())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Freedesktop/GIO native parser')
    def test_native_desktop_exec_round_trip_with_special_paths(self):
        try:
            from gi.repository import Gio
        except ImportError:
            self.skipTest('PyGObject not installed; parser verified on the Linux test host')
        folder = self.root/'a space 中文 "$`%\\ folder'
        folder.mkdir()
        result_path = self.root/'result.json'
        script = folder/'application.py'
        script.write_text('import json, os, sys\nfrom pathlib import Path\n'
                          + 'Path(' + repr(str(result_path)) + ').write_text(json.dumps('
                          + '[sys.argv[1:], os.environ["XDG_CONFIG_HOME"], os.environ["XDG_STATE_HOME"]]))\n')
        paths = platform.Paths(folder/'config/app', folder/'state/app', self.paths.unit)
        target = self.root/desktop.DESKTOP_NAME
        desktop.install_shortcuts(script, Path(sys.executable), paths, [target])
        validator = shutil.which('desktop-file-validate')
        if validator:
            result = subprocess.run([validator, str(target)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        entry = Gio.DesktopAppInfo.new_from_filename(str(target))
        self.assertIsNotNone(entry)
        self.assertTrue(entry.launch([], None))
        deadline = time.monotonic() + 5
        while not result_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(json.loads(result_path.read_text()),
                         [['open'], str(paths.config.parent), str(paths.state.parent)])


class OpenTests(DesktopCase):
    def setUp(self):
        super().setUp()
        session = patch.dict(os.environ, {'DISPLAY': ':test'}, clear=False)
        session.start()
        self.addCleanup(session.stop)
        self.startup, self.request, self.browser = Mock(), Mock(), Mock(return_value=True)
        self.ticket = {'app': platform.APP_NAME, 'port': 8876, 'code': 'x' * 43}
        self.request.return_value = self.ticket

    def install_marker(self):
        self.paths.unit.parent.mkdir(parents=True, exist_ok=True)
        self.paths.unit.write_text(platform.UNIT_MARKER+'\n[Service]\n')

    def launch(self, **kwargs):
        return desktop.open_panel(self.paths, self.startup, self.request, self.browser, **kwargs)

    def test_existing_backend_only_opens_panel_without_start_or_check(self):
        address = self.launch()
        self.assertEqual(address, 'http://127.0.0.1:8876/')
        self.browser.assert_called_once_with(address+'#launch='+self.ticket['code'])
        self.request.assert_called_once_with(self.paths, '/api/ui-ticket', {}, timeout=1.5)
        self.startup.command.assert_not_called()
        self.startup.set_enabled.assert_not_called()

    def test_repeated_clicks_do_not_start_additional_backends(self):
        self.launch()
        self.launch()
        self.assertEqual(self.browser.call_count, 2)
        self.startup.command.assert_not_called()

    def test_stopped_backend_started_once_and_waited_for(self):
        self.install_marker()
        self.request.side_effect = [desktop.BackendUnavailable(), desktop.BackendUnavailable(), self.ticket]
        pause = Mock()
        self.launch(pause=pause)
        self.startup.command.assert_called_once_with(['systemctl', '--user', 'start', '--no-block', platform.UNIT_NAME])
        self.startup.set_enabled.assert_not_called()
        pause.assert_called_once_with(0.25)

    def test_wait_timeout_is_bounded_and_does_not_open_browser(self):
        self.install_marker()
        self.request.side_effect = desktop.BackendUnavailable()
        with self.assertRaisesRegex(ValueError, '20 秒'):
            self.launch(clock=Mock(side_effect=[0, desktop.START_TIMEOUT]), pause=Mock())
        self.assertEqual(self.startup.command.call_count, 1)
        self.browser.assert_not_called()

    def test_headless_open_has_no_startup_side_effects(self):
        with patch.dict(os.environ, {'DISPLAY': '', 'WAYLAND_DISPLAY': ''}), self.assertRaisesRegex(ValueError, '图形桌面'):
            self.launch()
        self.request.assert_not_called()
        self.startup.command.assert_not_called()

    def test_missing_or_unmanaged_unit_not_started(self):
        self.request.side_effect = desktop.BackendUnavailable()
        with self.assertRaisesRegex(ValueError, 'install --desktop --open'): self.launch()
        self.paths.unit.parent.mkdir(parents=True)
        self.paths.unit.write_text('[Service]\nExecStart=unrelated\n')
        with self.assertRaises(ValueError): self.launch()
        self.startup.command.assert_not_called()

    def test_non_transient_errors_do_not_restart_or_mask_the_problem(self):
        self.request.side_effect = ValueError('更新后显式重启')
        with self.assertRaisesRegex(ValueError, '显式重启'): self.launch()
        self.startup.command.assert_not_called()

    def test_browser_failure_does_not_stop_or_reconfigure_backend(self):
        self.browser.return_value = False
        with self.assertRaisesRegex(ValueError, '浏览器'): self.launch()
        self.startup.command.assert_not_called()
        self.assertEqual(self.request.call_count, 1)

    def test_wrong_identity_bad_port_and_untrusted_url_are_rejected(self):
        for changes in ({'app': 'other'}, {'port': True}, {'port': 80}, {'port': 65536},
                        {'code': 'https://evil.example'}, {'code': None}):
            self.request.return_value = {**self.ticket, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.launch()
        self.browser.assert_not_called()

    def test_cli_installer_flags_and_silent_autostart_entry(self):
        args = app.parse_args(['install', '--desktop', '--open'])
        self.assertTrue(args.desktop and args.open_panel)
        self.assertFalse(args.enable or args.start)
        self.assertEqual(app.parse_args(['open']).command, 'open')
        text = platform.service_text(PurePosixPath('/app/linux/campus_linux.py'),
                                     PurePosixPath('/venv/bin/python'), self.paths, 8765)
        self.assertIn('run --port 8765 --autostart', text)
        self.assertNotIn(' open', text)
        self.assertNotIn('xdg-open', text)

    def test_failure_report_is_private_and_overwrites_instead_of_growing(self):
        with patch('desktop_linux.graphical_session', return_value=False):
            desktop.report_open_error(self.paths, 'first')
            desktop.report_open_error(self.paths, 'second')
        data = platform.read_private(self.paths.state/'last_launcher_error.txt').decode()
        self.assertNotIn('first', data)
        self.assertIn('second', data)


class BrowserOpenerTests(unittest.TestCase):
    def test_browser_is_detached_and_url_never_goes_through_shell(self):
        with patch('desktop_linux.shutil.which', return_value='/usr/bin/xdg-open'), patch('desktop_linux.subprocess.Popen') as popen:
            popen.return_value.wait.return_value = 0
            self.assertTrue(desktop.open_browser('http://127.0.0.1:8765/#launch=test'))
            arguments, options = popen.call_args
            self.assertEqual(arguments[0][0], '/usr/bin/xdg-open')
            self.assertTrue(options['start_new_session'])
            self.assertNotIn('shell', options)
            self.assertEqual(options['stderr'], subprocess.DEVNULL)

    def test_browser_opener_error_does_not_expose_code(self):
        with patch('desktop_linux.shutil.which', return_value='/usr/bin/xdg-open'), patch('desktop_linux.subprocess.Popen') as popen:
            popen.return_value.wait.return_value = 4
            with self.assertRaises(ValueError) as caught:
                desktop.open_browser('http://127.0.0.1:8765/#launch=private-code')
            self.assertNotIn('private-code', str(caught.exception))

    def test_long_lived_browser_is_not_killed(self):
        with patch('desktop_linux.shutil.which', return_value='/usr/bin/xdg-open'), patch('desktop_linux.subprocess.Popen') as popen:
            popen.return_value.wait.side_effect = subprocess.TimeoutExpired('browser', 5)
            self.assertTrue(desktop.open_browser('http://127.0.0.1:8765/'))
            popen.return_value.kill.assert_not_called()

    def test_missing_desktop_opener_reports_actionable_error(self):
        with patch('desktop_linux.shutil.which', return_value=None), self.assertRaisesRegex(ValueError, 'xdg-utils'):
            desktop.open_browser('http://127.0.0.1:8765/')


@unittest.skipUnless(sys.platform.startswith('linux'), 'Real Linux daemon lifecycle')
class LinuxLifecycleTests(DesktopCase):
    def test_open_start_reopen_and_exit_preserve_saved_pause(self):
        platform.atomic_private_write(self.paths.state/'run_state.json', b'{"keepalive_enabled":false}')
        self.paths.unit.parent.mkdir(parents=True, exist_ok=True)
        self.paths.unit.write_text(platform.UNIT_MARKER+'\n[Service]\n')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        # The child uses no real account and a fake systemd status reader. Its
        # real flock/HTTP/shutdown loop is exercised without changing user units.
        code = ('import sys; from types import SimpleNamespace; '
                + 'sys.path.insert(0, ' + repr(str(Path(app.__file__).parent)) + '); '
                + 'import campus_linux as app; '
                + 'app.AutoStart=lambda paths: SimpleNamespace(status=lambda: {}); app.main()')
        environment = {**os.environ, 'XDG_CONFIG_HOME': str(self.paths.config.parent),
                       'XDG_STATE_HOME': str(self.paths.state.parent)}
        child = []
        def start(arguments):
            self.assertEqual(arguments, ['systemctl', '--user', 'start', '--no-block', platform.UNIT_NAME])
            process = subprocess.Popen([sys.executable, '-B', '-c', code, 'run', '--port', str(port), '--autostart'],
                                       env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            child.append(process)
        def cleanup():
            for process in child:
                if process.poll() is None: process.terminate()
                process.communicate(timeout=8)
        self.addCleanup(cleanup)
        startup, browser = Mock(), Mock(return_value=True)
        startup.command.side_effect = start
        with patch.dict(os.environ, {'DISPLAY': ':test'}):
            desktop.open_panel(self.paths, startup, app.control_request, browser)
            desktop.open_panel(self.paths, startup, app.control_request, browser)
        startup.command.assert_called_once()
        self.assertIsNone(child[0].poll())
        snapshot = app.control_request(self.paths)
        self.assertFalse(snapshot['keepalive_enabled'])
        self.assertEqual(snapshot['run_state'], 'stopped')
        self.assertEqual(snapshot['launched_by'], 'systemd')
        self.assertIsNone(snapshot['result'])
        app.control_request(self.paths, '/api/exit', {})
        self.assertEqual(child[0].wait(timeout=8), 0)
        self.assertFalse(json.loads(platform.read_private(self.paths.state/'run_state.json'))['keepalive_enabled'])


if __name__ == '__main__':
    unittest.main()
