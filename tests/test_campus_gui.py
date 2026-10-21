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
        self.workers = []
        def make_worker(settings, emit, once=False, allow_login=True):
            worker = Mock(once=once, allow_login=allow_login, settings=settings, emit=emit)
            self.workers.append(worker)
            return worker
        self.worker_factory = Mock(side_effect=make_worker)
        self.patches = [patch.object(core, 'DATA_DIR', Path(self.temp.name)),
                        patch.object(core, 'SettingsStore', return_value=self.store),
                        patch.object(core, 'AutoStart', return_value=self.startup),
                        patch.object(core, 'KeepaliveWorker', self.worker_factory),
                        patch.object(gui.campus_tray, 'TrayIcon', return_value=self.tray)]
        for p in self.patches:
            p.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.tk.call('tk', 'scaling', 2.0)  # Reproduce the user's 144-DPI fonts.
        # Pair the high-DPI fixture with a fixed desktop size. CI may expose a
        # 768-pixel desktop, which tests screen clipping instead of DPI sizing.
        self.root.maxsize(1920, 1080)
        self.root.winfo_screenwidth = Mock(return_value=1920)
        self.root.winfo_screenheight = Mock(return_value=1080)
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

    def reopen(self, **options):
        self.app.close(force=True)
        self.root = tk.Tk()
        self.root.withdraw()
        with patch.object(self.root, 'after') as schedule:
            self.app = gui.CampusApp(self.root, **options)
        for call in schedule.call_args_list:
            if call.args[0] == 300:
                call.args[1]()
        self.root.update_idletasks()

    def logs(self):
        return self.app.log_text.get('1.0', 'end')

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

    def test_startup_control_is_large_and_keyboard_focusable(self):
        scale = max(1.0, self.root.winfo_fpixels('1i') / 96)
        control = self.app.startup_check
        style = gui.ttk.Style(self.root)
        self.assertEqual(control.cget('style'), 'Startup.TCheckbutton')
        self.assertGreaterEqual(int(style.lookup('Startup.TCheckbutton', 'indicatorsize')), round(24 * scale))
        self.assertGreaterEqual(control.winfo_reqheight(), round(44 * scale) - 1)
        self.assertEqual(str(control.cget('takefocus')), '1')

    def test_startup_control_displays_actual_state_in_words(self):
        for state, text in (('disabled', '已关闭'), ('enabled', '已开启'),
                            ('different', '需更新启动项'), ('error', '无法读取状态')):
            with self.subTest(state=state):
                self.startup.state.return_value = state
                self.app.refresh_startup()
                self.assertEqual(self.app.startup_label.get(), '开机自动保活：' + text)
                self.assertEqual(self.app.startup_check.instate(['selected']), state == 'enabled')
        self.startup.set_enabled.assert_not_called()

    def test_startup_control_padding_click_and_space_toggle(self):
        def store_enabled(enabled):
            self.startup.state.return_value = 'enabled' if enabled else 'disabled'
        self.startup.set_enabled.side_effect = store_enabled
        self.root.deiconify()
        self.root.update()
        control = self.app.startup_check
        x, y = control.winfo_width() - 4, control.winfo_height() // 2
        control.event_generate('<ButtonPress-1>', x=x, y=y)
        control.event_generate('<ButtonRelease-1>', x=x, y=y)
        self.root.update()
        self.startup.set_enabled.assert_called_once_with(True)
        self.assertEqual(self.app.startup_label.get(), '开机自动保活：已开启')
        control.focus_force()
        self.root.update()
        control.event_generate('<KeyPress-space>')
        control.event_generate('<KeyRelease-space>')
        self.root.update()
        self.assertEqual(self.startup.set_enabled.call_count, 2)
        self.startup.set_enabled.assert_called_with(False)
        self.assertEqual(self.app.startup_label.get(), '开机自动保活：已关闭')

    def test_startup_control_restores_actual_state_if_write_fails(self):
        self.startup.set_enabled.side_effect = OSError('test write failure')
        with patch.object(gui.messagebox, 'showerror') as error:
            self.app.startup_check.invoke()
        error.assert_called_once()
        self.assertFalse(self.app.startup_check.instate(['selected']))
        self.assertEqual(self.app.startup_label.get(), '开机自动保活：已关闭')

    def test_window_omits_explanatory_labels(self):
        pending, labels = [self.root], []
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            if isinstance(widget, gui.ttk.Label):
                labels.append(str(widget.cget('text')))
        text = '\n'.join(labels)
        for phrase in ('以认证接口的在线记录为准', '密码使用 Windows 当前用户',
                       '点右上角 × 隐藏到托盘', '睡眠、关机时无法保活'):
            self.assertNotIn(phrase, text)

    def test_startup_checkbox_can_enable_and_disable(self):
        self.app.startup_checked.set(True)
        self.app.toggle_startup()
        self.startup.set_enabled.assert_called_with(True)
        self.app.startup_checked.set(False)
        self.app.toggle_startup()
        self.startup.set_enabled.assert_called_with(False)

    def test_autostart_launch_starts_continuous_keepalive_with_saved_settings(self):
        self.app.close(force=True)
        self.root = tk.Tk()
        self.root.withdraw()
        worker = Mock(once=False)
        with patch.object(self.root, 'after') as schedule, \
                patch.object(core, 'KeepaliveWorker', return_value=worker) as factory:
            self.app = gui.CampusApp(self.root, autostart=True)
            callbacks = [call.args[1] for call in schedule.call_args_list if call.args[0] == 300]
            self.assertEqual(callbacks, [self.app.resume_keepalive])
            callbacks[0]()
        worker.start.assert_called_once()
        self.assertEqual(factory.call_args.args[2:], (False, True))
        self.assertEqual(factory.call_args.args[0], self.store.load.return_value)
        self.assertTrue(self.app.autostart)
        self.assertIn('随 Windows 登录自动启动', self.app.log_text.get('1.0', 'end'))
        self.startup.set_enabled.assert_not_called()

    def test_manual_launch_automatically_uses_saved_credentials(self):
        self.reopen()
        self.app.worker.start.assert_called_once()
        self.assertFalse(self.app.worker.once)
        self.assertTrue(self.app.worker.allow_login)
        self.assertEqual(self.app.worker.settings, self.store.load.return_value)
        self.assertTrue(self.app.run_store.load())
        self.store.save.assert_not_called()
        self.startup.set_enabled.assert_not_called()

    def test_exit_while_running_preserves_switch_and_next_launch_resumes(self):
        self.app.start()
        previous = self.app.worker
        with patch.object(gui.messagebox, 'askyesno') as confirm:
            self.reopen()
        confirm.assert_not_called()
        previous.stop.assert_called_once()
        self.assertTrue(self.app.desired_running)
        self.assertTrue(self.app.run_store.load())
        self.assertIsNot(self.app.worker, previous)
        self.app.worker.start.assert_called_once()

    def test_explicit_stop_remains_paused_after_manual_and_autostart_launch(self):
        self.app.start()
        self.app.stop()
        # Exit before the stopped worker's asynchronous 'done' event arrives.
        self.reopen()
        self.assertIsNone(self.app.worker)
        self.assertFalse(self.app.desired_running)
        self.reopen(autostart=True)
        self.assertIsNone(self.app.worker)
        self.assertFalse(self.app.run_store.load())
        self.assertIn('上次保活为关闭状态', self.logs())

    def test_explicit_start_argument_overrides_remembered_pause(self):
        self.app.stop()
        self.reopen(start_immediately=True)
        self.assertIsNotNone(self.app.worker)
        self.assertTrue(self.app.run_store.load())

    def test_startup_missing_credentials_logs_without_dialog_or_worker(self):
        for autostart in (False, True):
            for username, password in (('', 'test-password'), ('test', ''), ('', '')):
                with self.subTest(autostart=autostart, username=username, has_password=bool(password)):
                    self.store.load.return_value = core.Settings(username, password)
                    with patch.object(gui.messagebox, 'showwarning') as warning, \
                            patch.object(gui.messagebox, 'showerror') as error:
                        self.reopen(autostart=autostart)
                    warning.assert_not_called()
                    error.assert_not_called()
                    self.assertIsNone(self.app.worker)
                    self.assertIn('尚未保存账号或密码', self.logs())

    def test_autostart_checkbox_starts_with_saved_form_and_disable_keeps_running(self):
        self.app.stop()
        self.app.username.set('updated-user')
        self.app.password.set('updated-password')
        self.app.startup_checked.set(True)
        self.app.toggle_startup()
        worker = self.app.worker
        self.assertIsNotNone(worker)
        self.assertEqual(worker.settings.username, 'updated-user')
        self.assertEqual(worker.settings.password, 'updated-password')
        worker.start.assert_called_once()
        self.assertTrue(self.app.run_store.load())
        self.app.startup_checked.set(False)
        self.app.toggle_startup()
        worker.stop.assert_not_called()
        self.assertTrue(self.app.run_store.load())

    def test_enabling_autostart_without_credentials_only_logs_and_saves_intent(self):
        self.app.stop()
        self.app.password.set('')
        self.app.startup_checked.set(True)
        with patch.object(gui.messagebox, 'showwarning') as warning:
            self.app.toggle_startup()
        warning.assert_not_called()
        self.startup.set_enabled.assert_called_once_with(True)
        self.assertIsNone(self.app.worker)
        self.assertTrue(self.app.run_store.load())
        self.assertIn('尚未保存账号或密码', self.logs())

    def test_failed_autostart_registration_does_not_start_or_enable_keepalive(self):
        self.app.stop()
        self.startup.set_enabled.side_effect = OSError('registry failure')
        self.app.startup_checked.set(True)
        with patch.object(gui.messagebox, 'showerror'):
            self.app.toggle_startup()
        self.assertIsNone(self.app.worker)
        self.assertFalse(self.app.run_store.load())

    def test_enabling_autostart_during_one_shot_waits_then_starts_once(self):
        self.app.stop()
        self.app.check_now()
        one_shot = self.app.worker
        self.app.startup_checked.set(True)
        self.app.toggle_startup()
        self.assertIs(self.app.worker, one_shot)
        self.assertTrue(self.app.pending_start)
        self.assertEqual(len(self.workers), 1)
        self.app.events.put(('done', None))
        self.app.poll()
        self.assertFalse(self.app.worker.once)
        self.assertEqual(len(self.workers), 2)
        self.app.worker.start.assert_called_once()
        self.assertTrue(self.app.run_store.load())

    def test_stop_cancels_a_queued_continuous_start(self):
        self.app.check_now()
        self.app.startup_checked.set(True)
        self.app.toggle_startup()
        self.app.stop()
        self.app.events.put(('next', (10180.0, 1180.0)))
        self.app.poll()
        self.assertIsNone(self.app.next_check)
        self.app.events.put(('done', None))
        self.app.poll()
        self.assertIsNone(self.app.worker)
        self.assertFalse(self.app.run_store.load())
        self.assertEqual(len(self.workers), 1)

    def test_enable_during_stop_resumes_only_after_old_worker_finishes(self):
        self.app.start()
        previous = self.app.worker
        self.app.stop()
        self.app.startup_checked.set(True)
        self.app.toggle_startup()
        self.assertIs(self.app.worker, previous)
        self.app.events.put(('done', None))
        self.app.poll()
        self.assertIsNot(self.app.worker, previous)
        self.assertTrue(self.app.run_store.load())
        self.assertEqual(len(self.workers), 2)

    def test_one_shot_completion_does_not_change_saved_pause(self):
        self.app.stop()
        self.app.check_now()
        self.app.events.put(('done', None))
        self.app.poll()
        self.reopen()
        self.assertIsNone(self.app.worker)
        self.assertFalse(self.app.run_store.load())

    def test_authentication_failure_remembers_pause_for_next_launch(self):
        self.app.start()
        self.app.events.put(('result', core.CheckResult(False, 'needs_attention', '账号需要处理')))
        self.app.events.put(('done', None))
        self.app.poll()
        self.assertFalse(self.app.run_store.load())
        self.reopen(autostart=True)
        self.assertIsNone(self.app.worker)

    def test_duplicate_start_and_launch_callback_do_not_duplicate_workers(self):
        self.app.start()
        self.app.resume_keepalive()
        self.app.startup_checked.set(True)
        self.app.toggle_startup()
        self.assertEqual(len(self.workers), 1)
        self.app.worker.start.assert_called_once()

    def test_stop_before_delayed_launch_callback_is_respected(self):
        self.app.stop()
        self.app.resume_keepalive()
        self.assertIsNone(self.app.worker)
        self.assertFalse(self.app.run_store.load())

    def test_bad_config_is_logged_without_startup_dialog_or_overwrite(self):
        self.store.load.side_effect = ValueError('读取配置失败')
        with patch.object(gui.messagebox, 'showerror') as error:
            self.reopen(autostart=True)
        error.assert_not_called()
        self.store.save.assert_not_called()
        self.assertIsNone(self.app.worker)
        self.assertIn('读取配置失败', self.logs())

    def test_corrupt_switch_is_preserved_until_explicit_start(self):
        path = Path(self.temp.name) / 'run_state.json'
        path.write_text('{broken', encoding='utf-8')
        self.reopen()
        self.assertIsNone(self.app.worker)
        self.assertIn('保活开关记录无法读取', self.logs())
        self.assertEqual(path.read_text(), '{broken')
        self.app.start()
        self.assertTrue(self.app.run_store.load())

    def test_failed_state_write_is_logged_without_preventing_stop(self):
        self.app.start()
        with patch.object(self.app.run_store, 'save', side_effect=OSError('disk failure')):
            self.app.stop()
        self.app.worker.stop.assert_called_once()
        self.assertFalse(self.app.desired_running)
        self.assertIn('保活开关未能保存', self.logs())

    def test_failed_worker_start_clears_running_switch_and_allows_retry(self):
        worker = Mock(once=False)
        worker.start.side_effect = RuntimeError('thread creation failed')
        with patch.object(core, 'KeepaliveWorker', return_value=worker):
            self.app.start()
        self.assertIsNone(self.app.worker)
        self.assertFalse(self.app.run_store.load())
        self.assertEqual(str(self.app.start_button.cget('state')), 'normal')
        self.app.start()
        self.assertIsNotNone(self.app.worker)

    def test_smoke_gui_initialization_does_not_start_worker_or_write_switch(self):
        self.reopen(initial_probe=False)
        self.assertIsNone(self.app.worker)
        self.assertFalse((Path(self.temp.name) / 'run_state.json').exists())
        self.assertEqual(len(self.workers), 0)

    def test_other_account_keeps_running_and_shows_next_check(self):
        worker = Mock(once=False)
        with patch.object(core, 'KeepaliveWorker', return_value=worker):
            self.app.start()
        self.app.events.put(('result', core.CheckResult(False, 'other_account', 'other')))
        self.app.events.put(('next', (10180.0, 1180.0)))
        self.app.poll()
        self.assertIs(self.app.worker, worker)
        self.assertIn('运行中', self.app.running_state.get())
        self.assertEqual(self.app.login_state.get(), '其他账号在线')
        self.assertEqual(self.app.next_check, (10180.0, 1180.0))
        self.assertEqual(str(self.app.stop_button.cget('state')), 'normal')
        worker.stop.assert_not_called()

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

    def test_online_status_and_logs_are_kept_without_saved_response(self):
        result = core.CheckResult(True, 'online', '已确认在线', 'teaching', 'test', '172.31.1.2',
                                  {'error':'raw-response-detail'}, checked_at='2026-10-10 15:00:00 +0800')
        self.app.show_result(result)
        self.assertIn('成功登录', self.app.login_state.get())
        self.assertIn('172.31.1.2', self.app.account_status.get())
        self.assertFalse(hasattr(self.app, 'capture_text'))
        logs = self.app.log_text.get('1.0', 'end')
        self.assertIn('已确认在线', logs)
        self.assertIn('2026-10-10 15:00:00 +0800', logs)
        self.assertNotIn('raw-response-detail', logs)
        self.assertFalse((Path(self.temp.name) / 'last_response.json').exists())

    def test_response_tab_is_removed(self):
        pending, tabs = [self.root], []
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            if isinstance(widget, gui.ttk.Notebook):
                tabs.extend(str(widget.tab(tab, 'text')).strip() for tab in widget.tabs())
        self.assertEqual(tabs, ['运行日志'])

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

    def test_exit_does_not_ask_to_turn_off_the_remembered_switch(self):
        self.app.start()
        worker = self.app.worker
        self.app.hide_window()
        with patch.object(gui.messagebox, 'askyesno') as confirm:
            self.app.tray_action('exit')
        confirm.assert_not_called()
        self.assertTrue(self.app.closed)
        worker.stop.assert_called_once()
        self.tray.close.assert_called_once()
        self.assertTrue(self.app.run_store.load())

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
        self.app.start()
        worker = self.app.worker
        with patch.object(gui.messagebox, 'askyesno') as confirm:
            self.app.tray_action('shutdown')
        confirm.assert_not_called()
        worker.stop.assert_called_once()
        self.assertTrue(self.app.closed)
        self.assertTrue(self.app.run_store.load())


if __name__ == '__main__':
    unittest.main()
