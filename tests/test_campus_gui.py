"""GUI behavior tests use a hidden test window and fake settings/startup stores."""
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import campus_core as core
import campus_keepalive as gui


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Mock()
        self.store.load.return_value = core.Settings(username='test', password='test-password')
        self.startup = Mock()
        self.startup.state.return_value = 'disabled'
        self.tray = Mock()
        self.tray.ensure_visible.return_value = True
        self.tray.update_tooltip.return_value = True
        self.patches = [patch.object(core, 'DATA_DIR', Path(self.temp.name)),
                        patch.object(core, 'SettingsStore', return_value=self.store),
                        patch.object(core, 'AutoStart', return_value=self.startup),
                        patch.object(gui.campus_tray, 'TrayIcon', return_value=self.tray)]
        for p in self.patches:
            p.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.tk.call('tk', 'scaling', 2.0)  # Reproduce the user's 144-DPI fonts.
        self.app = gui.CampusApp(self.root, initial_probe=False)
        self.root.update_idletasks()

    def tearDown(self):
        if not self.app.closed:
            self.root.update_idletasks()
            for callback in self.root.tk.call('after', 'info'):
                self.root.after_cancel(callback)
            self.app.close(force=True)
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_startup_checkbox_reads_real_store_not_saved_preference(self):
        self.assertFalse(self.app.startup_checked.get())
        self.assertEqual(self.app.startup_text.get(), '未开启')
        self.startup.state.return_value = 'enabled'
        self.app.refresh_startup()
        self.assertTrue(self.app.startup_checked.get())
        self.assertEqual(self.app.startup_text.get(), '已开启')

    def test_high_dpi_window_reserves_readable_log_space(self):
        self.root.deiconify()
        self.root.update()
        self.assertGreaterEqual(self.app.log_text.winfo_height(), 140)

    def test_startup_checkbox_can_enable_and_disable(self):
        self.app.startup_checked.set(True)
        self.app.toggle_startup()
        self.startup.set_enabled.assert_called_with(True)
        self.app.startup_checked.set(False)
        self.app.toggle_startup()
        self.startup.set_enabled.assert_called_with(False)

    def test_interval_save_updates_worker(self):
        self.app.interval.set('2.5')
        worker = Mock(once=False)
        self.app.worker = worker
        self.assertTrue(self.app.save(quiet=True))
        self.assertEqual(self.store.save.call_args.args[0].interval_minutes, 2.5)
        self.assertEqual(worker.update.call_args.args[0].interval_minutes, 2.5)

    def test_bad_interval_does_not_overwrite_settings(self):
        self.app.interval.set('nan')
        with patch.object(gui.messagebox, 'showerror'):
            self.assertFalse(self.app.save(quiet=True))
        self.store.save.assert_not_called()

    def test_stop_updates_controls_and_clears_checking_status(self):
        worker = Mock(once=False)
        self.app.worker = worker
        self.app.login_state.set('正在检查…')
        self.app.stop()
        worker.stop.assert_called_once()
        self.app.events.put(('done', None))
        self.app.poll()
        self.assertIsNone(self.app.worker)
        self.assertEqual(self.app.login_state.get(), '已停止检查')
        self.assertIn('已停止', self.app.running_state.get())

    def test_online_status_and_capture_are_displayed(self):
        result = core.CheckResult(True, 'online', '已确认在线', 'teaching', 'test', '172.31.1.2',
                                  {'error':'ok'}, checked_at='2026-10-10 15:00:00 +0800')
        self.app.show_result(result)
        self.assertIn('成功登录', self.app.login_state.get())
        self.assertIn('172.31.1.2', self.app.account_status.get())
        self.assertIn('online_response', self.app.capture_text.get('1.0', 'end'))
        self.assertTrue((Path(self.temp.name) / 'last_response.json').exists())

    def test_clock_uses_system_time_and_running_worker_can_be_woken(self):
        with patch.object(core, 'system_time', return_value='2026-10-10 16:00:00 +0800'):
            self.app.tick()
        self.assertIn('16:00:00', self.app.clock.get())
        worker = Mock(once=False)
        self.app.worker = worker
        self.app.check_now()
        worker.check_now.assert_called_once()

    def test_countdown_does_not_wake_before_deadline(self):
        worker = Mock(once=False)
        self.app.worker = worker
        self.app.next_check = (1000, 100.5)
        with patch.object(gui.time, 'monotonic', return_value=100.0):
            self.app.tick()
        worker.check_now.assert_not_called()
        with patch.object(gui.time, 'monotonic', return_value=100.5):
            self.app.tick()
        worker.check_now.assert_called_once()

    def test_ui_log_redacts_current_password(self):
        self.app.log('error: test-password')
        self.assertNotIn('test-password', self.app.log_text.get('1.0', 'end'))

    def test_titlebar_close_keeps_keepalive_worker_running(self):
        worker = Mock(once=False)
        self.app.worker = worker
        self.root.deiconify()
        self.root.update()
        with patch.object(gui.messagebox, 'askyesno', return_value=True) as confirm:
            self.root.tk.call(self.root.protocol('WM_DELETE_WINDOW'))
        confirm.assert_not_called()
        self.assertFalse(self.app.closed)
        self.assertEqual(self.root.state(), 'withdrawn')
        worker.stop.assert_not_called()
        self.tray.close.assert_not_called()
        self.assertTrue(self.app.hidden_to_tray)

    def test_hiding_does_not_start_an_idle_worker_or_change_startup(self):
        with patch.object(self.app, 'launch_worker') as launch:
            self.app.hide_window()
        launch.assert_not_called()
        self.startup.set_enabled.assert_not_called()
        self.assertIsNone(self.app.worker)

    def test_tray_show_event_restores_window(self):
        self.app.hide_window()
        self.app.events.put(('tray', 'show'))
        self.app.poll()
        self.root.update_idletasks()
        self.assertEqual(self.root.state(), 'normal')
        self.assertFalse(self.app.hidden_to_tray)

    def test_hidden_window_still_processes_results_and_deadlines(self):
        worker = Mock(once=False)
        self.app.worker = worker
        self.app.hide_window()
        result = core.CheckResult(True, 'online', 'online', 'teaching', 'test', '172.31.1.2')
        self.app.events.put(('result', result))
        self.app.events.put(('next', (1100, 101.0)))
        self.app.poll()
        with patch.object(gui.time, 'monotonic', return_value=102.0):
            self.app.tick()
        self.assertEqual(self.root.state(), 'withdrawn')
        self.assertIs(self.app.last_result, result)
        worker.check_now.assert_called_once()
        worker.stop.assert_not_called()

    def test_explicit_exit_stops_worker_and_removes_tray(self):
        worker = Mock(once=False)
        self.app.worker = worker
        self.app.hide_window()
        with patch.object(gui.messagebox, 'askyesno', return_value=True):
            self.app.events.put(('tray', 'exit'))
            self.app.poll()
        self.assertTrue(self.app.closed)
        worker.stop.assert_called_once()
        self.tray.close.assert_called_once()
        self.app.close(force=True)  # Idempotent even after Tk was destroyed.
        worker.stop.assert_called_once()

    def test_cancelling_exit_restores_window_and_keeps_worker(self):
        worker = Mock(once=False)
        self.app.worker = worker
        self.app.hide_window()
        with patch.object(gui.messagebox, 'askyesno', return_value=False):
            self.app.tray_action('exit')
        self.assertFalse(self.app.closed)
        self.assertEqual(self.root.state(), 'normal')
        worker.stop.assert_not_called()
        self.tray.close.assert_not_called()

    def test_missing_tray_never_leaves_window_unreachable(self):
        self.tray.ensure_visible.return_value = False
        with patch.object(gui.messagebox, 'showwarning') as warning:
            self.app.hide_window()
        self.assertEqual(self.root.state(), 'normal')
        self.assertFalse(self.app.hidden_to_tray)
        warning.assert_called_once()

    def test_failed_tray_initialization_keeps_window_usable(self):
        self.app.tray = None
        with patch.object(gui.campus_tray, 'TrayIcon', side_effect=OSError('not available')):
            with patch.object(gui.messagebox, 'showwarning'):
                self.app.hide_window()
        self.assertEqual(self.root.state(), 'normal')
        self.assertFalse(self.app.closed)

    def test_tray_failure_restores_hidden_window_without_stopping_worker(self):
        worker = Mock(once=False)
        self.app.worker = worker
        self.app.hide_window()
        self.app.events.put(('tray', 'unavailable'))
        self.app.poll()
        self.assertEqual(self.root.state(), 'normal')
        worker.stop.assert_not_called()

    def test_tray_stop_does_not_close_window_or_tray(self):
        worker = Mock(once=False)
        self.app.worker = worker
        self.app.hide_window()
        self.app.tray_action('stop')
        worker.stop.assert_called_once()
        self.tray.close.assert_not_called()
        self.assertFalse(self.app.closed)
        self.assertEqual(self.root.state(), 'withdrawn')

    def test_windows_shutdown_exits_without_confirmation(self):
        worker = Mock(once=False)
        self.app.worker = worker
        with patch.object(gui.messagebox, 'askyesno') as confirm:
            self.app.tray_action('shutdown')
        confirm.assert_not_called()
        worker.stop.assert_called_once()
        self.assertTrue(self.app.closed)


if __name__ == '__main__':
    unittest.main()
