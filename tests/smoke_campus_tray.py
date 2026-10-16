"""Opt-in live, read-only smoke test: hide for two timed checks, restore and exit.

Does not log in/out, save settings, change startup entries, or retain captures.
Run with no normal application instance open.
"""
from dataclasses import replace
import ctypes
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'outputs'))
import campus_core as core
import campus_keepalive as gui
import campus_tray as tray


def main():
    instance = core.SingleInstance()
    if instance.already_running:
        instance.close()
        raise RuntimeError('Close the normal app before running the smoke test.')
    try:
        settings = replace(core.SettingsStore().load(), interval_minutes=0.5)
        startup_before = core.AutoStart().state()
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        with tempfile.TemporaryDirectory() as directory:
            store = Mock()
            store.load.return_value = settings
            with patch.object(core, 'DATA_DIR', Path(directory)), \
                    patch.object(core, 'SettingsStore', return_value=store):
                root = tk.Tk()
                app = gui.CampusApp(root, initial_probe=False)
                if not app.tray:
                    app.close(force=True)
                    raise RuntimeError('No tray icon.')
                errors, results = [], []
                original_show = app.show_result

                def finish():
                    try:
                        assert root.state() == 'normal', 'Restore did not deiconify Tk.'
                        assert app.worker is worker and worker.thread.is_alive()
                        assert app.tray.ensure_visible(), 'Tray icon was lost.'
                        from PIL import ImageGrab
                        api = tray._user32()
                        hwnd = api.FindWindowW(None, core.APP_TITLE)
                        ImageGrab.grab(window=hwnd).save(
                            Path(__file__).resolve().parent / 'artifacts' / 'campus_gui.png')
                        with patch.object(gui.messagebox, 'askyesno', return_value=True):
                            app.tray_action('exit')
                        assert app.closed and worker.stop_event.is_set()
                        assert not tray.restore_existing_window(), 'Tray window survived exit.'
                        print('RESTORE_AND_EXPLICIT_EXIT_OK', flush=True)
                    except Exception as exc:
                        errors.append(exc)
                        if not app.closed:
                            app.close(force=True)

                def checked(result):
                    original_show(result)
                    results.append((result.state, result.portal, root.state()))
                    print(f'CHECK_{len(results)}: state={result.state}, portal={result.portal}, '
                          f'window={root.state()}, time={result.checked_at}', flush=True)
                    if root.state() != 'withdrawn':
                        errors.append(AssertionError('Window did not stay hidden.'))
                    if len(results) == 2:
                        if not tray.restore_existing_window():
                            errors.append(AssertionError('Restore message was not sent.'))
                        root.after(350, finish)

                def timeout():
                    errors.append(AssertionError('Two hidden checks did not finish within 100 seconds.'))
                    app.close(force=True)

                app.show_result = checked
                app.launch_worker(once=False, allow_login=False)
                worker = app.worker
                root.tk.call(root.protocol('WM_DELETE_WINDOW'))
                assert root.state() == 'withdrawn' and not worker.stop_event.is_set()
                print('HIDDEN_WITH_WORKER_RUNNING; interval=30s; login_disabled=True', flush=True)
                root.after(100_000, timeout)
                root.mainloop()
                worker.thread.join(timeout=2)
                assert not worker.thread.is_alive(), 'Worker survived explicit exit.'
                assert len(results) >= 2, 'Not enough checks.'
                assert not errors, errors
                store.save.assert_not_called()
        assert core.AutoStart().state() == startup_before, 'Startup entry changed.'
        print(f'PASS; startup_preserved={startup_before}; settings_not_saved=True', flush=True)
    finally:
        instance.close()


if __name__ == '__main__':
    main()
