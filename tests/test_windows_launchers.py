"""Run the shipped launchers on Windows, with a real isolated Python installation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import venv
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SYSTEM = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'


@unittest.skipUnless(sys.platform == 'win32', 'Windows launchers')
class WindowsLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installation = tempfile.TemporaryDirectory(prefix='campus-python-')
        cls.addClassCleanup(cls.installation.cleanup)
        cls.python_home = Path(cls.installation.name) / 'Python 空格 with spaces'
        venv.EnvBuilder(with_pip=False).create(cls.python_home)
        cls.python = cls.python_home / 'Scripts' / 'python.exe'
        downloads = Path(cls.installation.name) / 'downloads'
        subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'tools' / 'build_packages.py'),
                        '--platform', 'windows', '--output', str(downloads)],
                       check=True, capture_output=True, timeout=20)
        cls.archive = downloads / 'szu-campus-keepalive-windows.zip'

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='campus-launchers-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name) / '校园网 & (test) with spaces'
        with zipfile.ZipFile(self.archive) as archive:
            archive.extractall(self.temporary.name)
        (Path(self.temporary.name) / 'szu-campus-keepalive-windows').rename(self.folder)
        shutil.copyfile(ROOT / 'tests' / 'launcher_probe.py', self.folder / 'campus_keepalive.py')
        self.env = dict(os.environ)
        for key in ('VIRTUAL_ENV', 'CONDA_PREFIX', 'PYTHONHOME', 'PYTHONPATH', 'CAMPUS_PYTHON'):
            self.env.pop(key, None)
        # No Anaconda path, no py.exe or pyw.exe: only this real Python and Windows.
        self.env['PATH'] = str(self.python.parent) + os.pathsep + str(SYSTEM)
        self.addCleanup(self.stop_probe)

    def stop_probe(self):
        (self.folder / 'stop-probe').touch()
        time.sleep(0.25)

    def start(self, entry=None, command=None):
        if command is not None:
            pass
        elif entry.endswith('.vbs'):
            command = [str(SYSTEM / 'cscript.exe'), '//B', '//NoLogo', str(self.folder / entry)]
        else:
            command = f'"{SYSTEM / "cmd.exe"}" /d /s /c ""{self.folder / entry}""'
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        self.output = (self.folder / 'launcher-output.txt').open('wb')
        self.addCleanup(self.output.close)
        process = subprocess.Popen(command, cwd=self.folder, env=self.env,
                                   stdin=subprocess.DEVNULL, stdout=self.output, stderr=self.output,
                                   creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
        self.addCleanup(lambda: process.wait(timeout=5) if process.poll() is not None else self.kill(process))
        return process

    @staticmethod
    def kill(process):
        process.terminate()
        process.wait(timeout=5)

    def ready(self, timeout=12):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                return json.loads((self.folder / 'probe.json').read_text(encoding='utf-8'))
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.05)
        output = (self.folder / 'launcher-output.txt').read_bytes().decode('utf-8', errors='replace')
        log = self.folder / 'data' / 'launcher.log'
        if log.exists():
            output += log.read_text(encoding='utf-8', errors='replace')
        self.fail('Launcher did not start the application. ' + output)

    def assert_python_and_no_console(self, result):
        self.assertEqual(Path(result['prefix']).resolve(), self.python_home.resolve(),
                         'Use the discovered Python, not a hardcoded Anaconda installation')
        self.assertEqual(result['console'], 0, 'GUI must not share a console that can close it')

    def test_vbs_uses_python_on_path_without_py_launcher(self):
        process = self.start('启动校园网保活.vbs')
        result = self.ready()
        self.assert_python_and_no_console(result)
        self.assertEqual(process.wait(timeout=8), 0)

    def test_bat_application_survives_launcher_exit_without_console(self):
        process = self.start('排错启动.bat')
        result = self.ready()
        self.assert_python_and_no_console(result)
        self.assertEqual(process.wait(timeout=8), 0, 'Diagnostic launcher must release the GUI')
        time.sleep(0.3)
        later = self.ready()
        self.assertEqual(later['pid'], result['pid'])
        self.assertGreater(later['tick'], result['tick'], 'GUI must keep running after launcher exit')

    def without_pythonw(self):
        windowed = self.python.with_name('pythonw.exe')
        if windowed.exists():
            saved = windowed.with_suffix('.saved')
            windowed.rename(saved)
            self.addCleanup(lambda: saved.rename(windowed))

    def test_vbs_without_pythonw_still_starts_without_console(self):
        self.without_pythonw()
        process = self.start('启动校园网保活.vbs')
        self.assert_python_and_no_console(self.ready())
        self.assertEqual(process.wait(timeout=8), 0)

    def test_custom_python_path_without_path_or_py(self):
        self.env['PATH'] = str(SYSTEM)
        self.env['CAMPUS_PYTHON'] = str(self.python)
        process = self.start('启动校园网保活.vbs')
        self.assert_python_and_no_console(self.ready())
        self.assertEqual(process.wait(timeout=8), 0)

    def test_active_venv_without_python_on_path(self):
        self.env['PATH'] = str(SYSTEM)
        self.env['VIRTUAL_ENV'] = str(self.python_home)
        process = self.start('启动校园网保活.vbs')
        self.assert_python_and_no_console(self.ready())
        self.assertEqual(process.wait(timeout=8), 0)

    def test_stale_pythonhome_does_not_break_selected_python(self):
        self.env['PYTHONHOME'] = str(self.folder / 'removed-python-installation')
        process = self.start('启动校园网保活.vbs')
        self.assert_python_and_no_console(self.ready())
        self.assertEqual(process.wait(timeout=8), 0)

    def test_external_pythonpath_does_not_override_application_imports(self):
        external = self.folder / 'unrelated-python-modules'
        external.mkdir()
        (external / 'json.py').write_text('raise RuntimeError("unrelated json module")\n', encoding='utf-8')
        self.env['PYTHONPATH'] = str(external)
        process = self.start('启动校园网保活.vbs')
        self.assert_python_and_no_console(self.ready())
        self.assertEqual(process.wait(timeout=8), 0)

    def test_invalid_explicit_python_reports_error_instead_of_launching_other_python(self):
        self.env['CAMPUS_PYTHON'] = str(self.folder / 'missing-python.exe')
        process = self.start('排错启动.bat')
        self.assertNotEqual(process.wait(timeout=12), 0)
        self.assertFalse((self.folder / 'probe.json').exists())
        log = (self.folder / 'data' / 'launcher.log').read_text(encoding='utf-8-sig')
        self.assertIn('Python 3.10+', log)
        self.assertIn('CAMPUS_PYTHON', log)

    def test_broken_path_candidate_does_not_hide_working_python(self):
        broken = self.folder / 'broken'
        broken.mkdir()
        (broken / 'python.exe').write_bytes(b'not an executable')
        self.env['PATH'] = str(broken) + os.pathsep + self.env['PATH']
        self.start('启动校园网保活.vbs')
        self.assert_python_and_no_console(self.ready())

    def test_autostart_without_pythonw_uses_same_console_free_launch(self):
        self.without_pythonw()
        sys.path.insert(0, str(ROOT / 'outputs'))
        import campus_core as core
        startup = core.AutoStart(script=self.folder / 'campus_keepalive.py', executable=self.python)
        process = self.start(command=startup.command)
        result = self.ready()
        self.assertEqual(result['args'], ['--autostart'])
        self.assert_python_and_no_console(result)
        self.assertEqual(process.wait(timeout=8), 0)

    def test_autostart_with_pythonw_ignores_stale_pythonhome(self):
        sys.path.insert(0, str(ROOT / 'outputs'))
        import campus_core as core
        self.env['PYTHONHOME'] = str(self.folder / 'removed-python-installation')
        startup = core.AutoStart(script=self.folder / 'campus_keepalive.py', executable=self.python)
        self.start(command=startup.command)
        result = self.ready()
        self.assertEqual(result['args'], ['--autostart'])
        self.assert_python_and_no_console(result)

    def test_registry_install_without_path_or_launcher(self):
        import winreg
        # Only this new UUID-named fixture key is written; real install/startup keys stay untouched.
        key = 'Software\\CampusLauncherTests_' + uuid.uuid4().hex
        install_key = key + r'\Software\Python\TestVendor\3.12\InstallPath'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, install_key) as registry:
            winreg.SetValueEx(registry, 'ExecutablePath', 0, winreg.REG_SZ, str(self.python))
        def clean_registry():
            def remove(path):
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as handle:
                    children = []
                    for index in range(winreg.QueryInfoKey(handle)[0]):
                        children.append(winreg.EnumKey(handle, index))
                for child in children:
                    remove(path + '\\' + child)
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            remove(key)
        self.addCleanup(clean_registry)
        self.env['PATH'] = str(SYSTEM)
        script = str(self.folder / 'campus_launcher.ps1').replace("'", "''")
        # Bind process-local registry drives to the real fixture tree. No discovery functions are mocked.
        code = (
            "Remove-PSDrive HKCU,HKLM; "
            f"New-PSDrive -Name HKCU -PSProvider Registry -Root 'HKEY_CURRENT_USER\\{key}' | Out-Null; "
            f"New-PSDrive -Name HKLM -PSProvider Registry -Root 'HKEY_CURRENT_USER\\{key}' | Out-Null; "
            f"& '{script}'"
        )
        process = self.start(command=[str(SYSTEM / 'WindowsPowerShell/v1.0/powershell.exe'),
                                     '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', code])
        self.assert_python_and_no_console(self.ready())
        self.assertEqual(process.wait(timeout=8), 0)


if __name__ == '__main__':
    unittest.main()
