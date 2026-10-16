"""Native tray checks use only our own temporary icon, never restart Explorer."""
import ctypes
from pathlib import Path
import sys
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import campus_tray as tray


@unittest.skipUnless(sys.platform == 'win32', 'Windows tray')
class NativeTrayTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.update_idletasks()
        self.emit = Mock()
        self.icon = tray.TrayIcon(self.emit)

    def tearDown(self):
        self.icon.close()
        self.root.destroy()

    def test_native_icon_created_and_cleanup_is_idempotent(self):
        self.assertTrue(self.icon.added)
        self.assertTrue(self.icon.ensure_visible())
        self.assertEqual(self.icon.hwnd, self.icon.api.FindWindowW(tray.WINDOW_CLASS, tray.WINDOW_TITLE))
        self.icon.close()
        self.icon.close()
        self.assertIsNone(self.icon.api.FindWindowW(tray.WINDOW_CLASS, tray.WINDOW_TITLE))
        self.assertFalse(self.icon.ensure_visible())

    def test_duplicate_launch_posts_restore_to_tk_thread(self):
        self.assertTrue(tray.restore_existing_window())
        self.emit.assert_not_called()
        self.root.update()
        self.emit.assert_called_once_with('show')

    def test_version_four_click_and_keyboard_actions(self):
        for event in (0x400, 0x401, 0x203):
            self.icon._window_proc(self.icon.hwnd, tray.TRAY_MESSAGE, 0, (1 << 16) | event)
        self.assertEqual(self.emit.call_count, 3)
        self.emit.assert_called_with('show')

    def test_right_click_selects_explicit_exit_without_calling_tk(self):
        with patch.object(self.icon.api, 'TrackPopupMenu', return_value=4):
            self.icon._window_proc(self.icon.hwnd, tray.TRAY_MESSAGE, 0, (1 << 16) | 0x7b)
        self.emit.assert_called_once_with('exit')
        self.assertFalse(self.icon.closed)

    def test_all_menu_actions_and_dismissal(self):
        for command, expected in ((1, 'show'), (2, 'start'), (3, 'stop'), (0, None)):
            self.emit.reset_mock()
            with patch.object(self.icon.api, 'TrackPopupMenu', return_value=command):
                self.icon._menu()
            if expected:
                self.emit.assert_called_once_with(expected)
            else:
                self.emit.assert_not_called()

    def test_explorer_recovery_recreates_our_removed_icon(self):
        data = self.icon._data()
        self.icon.shell.Shell_NotifyIconW(tray.NIM_DELETE, ctypes.byref(data))
        self.icon._window_proc(self.icon.hwnd, self.icon.taskbar_created, 0, 0)
        self.assertTrue(self.icon.added)
        self.assertTrue(self.icon.ensure_visible())
        self.emit.assert_not_called()

    def test_failed_explorer_recovery_asks_gui_to_restore(self):
        with patch.object(self.icon, '_add_icon', return_value=False):
            self.icon._window_proc(self.icon.hwnd, self.icon.taskbar_created, 0, 0)
        self.emit.assert_called_once_with('unavailable')

    def test_ensure_visible_recreates_a_lost_icon(self):
        self.icon.shell.Shell_NotifyIconW(tray.NIM_DELETE, ctypes.byref(self.icon._data()))
        self.assertTrue(self.icon.ensure_visible())
        self.assertTrue(self.icon.added)

    def test_tooltip_has_safe_length_and_can_change(self):
        self.assertTrue(self.icon.update_tooltip('保活运行中 ' + 'x' * 200))
        self.assertEqual(len(self.icon.tooltip), 127)
        self.assertTrue(self.icon.update_tooltip('保活已停止'))

    def test_native_close_is_a_queued_explicit_exit(self):
        self.icon._window_proc(self.icon.hwnd, 0x10, 0, 0)
        self.emit.assert_called_once_with('exit')
        self.assertFalse(self.icon.closed)

    def test_session_end_is_queued_and_does_not_block_signout(self):
        self.assertEqual(self.icon._window_proc(self.icon.hwnd, 0x11, 0, 0), 1)
        self.icon._window_proc(self.icon.hwnd, 0x16, 1, 0)
        self.emit.assert_called_once_with('shutdown')


if __name__ == '__main__':
    unittest.main()
