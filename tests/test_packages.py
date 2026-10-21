"""Exercise each download outside the source checkout, without real login."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import build_packages as packaging


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='campus-packages-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.directory = Path(cls.temp.name)
        cls.archives = dict(zip(('windows', 'linux'), packaging.build_packages(cls.directory / 'dist')))
        cls.contents = {}
        with zipfile.ZipFile(cls.archives['windows']) as archive:
            cls.contents['windows'] = {name: archive.read(name) for name in archive.namelist()}
            archive.extractall(cls.directory)
        with tarfile.open(cls.archives['linux']) as archive:
            cls.contents['linux'] = {item.name: archive.extractfile(item).read() for item in archive.getmembers()}
        for name, data in cls.contents['linux'].items():
            target = cls.directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def files(self, platform):
        prefix = f'szu-campus-keepalive-{platform}/'
        return {name.removeprefix(prefix): data for name, data in self.contents[platform].items()}

    def run_python(self, *arguments):
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        env['PYTHONIOENCODING'] = 'utf-8'
        completed = subprocess.run([sys.executable, '-B', *map(str, arguments)],
                                   cwd=self.directory, env=env, capture_output=True,
                                   text=True, encoding='utf-8', timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return completed.stdout

    def test_windows_contains_only_its_application_and_instructions(self):
        self.assertEqual(set(self.files('windows')), {
            'campus_core.py', 'campus_keepalive.py', 'campus_tray.py',
            '启动校园网保活.vbs', '排错启动.bat', 'README.md',
        })

    def test_linux_contains_its_core_and_complete_web_interface(self):
        self.assertEqual(set(self.files('linux')), {
            'campus_core.py', 'campus_linux.py', 'auth_linux.py',
            'platform_linux.py', 'desktop_linux.py', 'requirements.txt',
            'README.md', 'web/index.html', 'web/style.css', 'web/app.js',
        })

    def test_both_packages_use_the_canonical_shared_core(self):
        expected = (packaging.ROOT / 'outputs/campus_core.py').read_bytes()
        for platform in ('windows', 'linux'):
            with self.subTest(platform=platform):
                self.assertEqual(self.files(platform)['campus_core.py'], expected)

    def test_all_bundled_python_sources_compile(self):
        for platform, files in self.contents.items():
            for name, data in files.items():
                if name.endswith('.py'):
                    with self.subTest(platform=platform, file=name):
                        compile(data, name, 'exec')

    def test_linux_imports_work_without_windows_source_directory(self):
        # A stale neighbouring checkout must not override the bundled core.
        neighbour = self.directory / 'outputs'
        neighbour.mkdir(exist_ok=True)
        (neighbour / 'campus_core.py').write_text('raise RuntimeError("wrong shared core")\n', encoding='utf-8')
        folder = self.directory / 'szu-campus-keepalive-linux'
        for module in ('campus_linux', 'auth_linux', 'platform_linux'):
            with self.subTest(module=module):
                code = (
                    'import importlib, sys; from pathlib import Path; '
                    'root = Path(sys.argv[1]); sys.path.insert(0, str(root)); '
                    'importlib.import_module(sys.argv[2]); import campus_core; '
                    'assert Path(campus_core.__file__).parent == root; '
                    'import campus_linux; assert (campus_linux.WEB_DIR / "index.html").is_file()'
                )
                self.run_python('-c', code, folder, module)
        self.assertFalse((folder / 'data').exists())

    def test_linux_command_help_runs_from_extracted_package(self):
        folder = self.directory / 'szu-campus-keepalive-linux'
        output = self.run_python(folder / 'campus_linux.py', '--help')
        self.assertIn('portal-info', output)
        self.assertIn('install', output)
        self.assertFalse((folder / 'data').exists())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows entry imports native tray bindings')
    def test_windows_command_help_runs_without_opening_gui_or_data(self):
        folder = self.directory / 'szu-campus-keepalive-windows'
        output = self.run_python(folder / 'campus_keepalive.py', '--help')
        self.assertIn('--autostart', output)
        self.assertIn('--start', output)
        self.assertFalse((folder / 'data').exists())

    def test_bundled_instructions_use_the_standalone_layout(self):
        for platform in ('windows', 'linux'):
            readme = self.files(platform)['README.md'].decode('utf-8')
            self.assertNotIn('完整仓库根目录', readme)
            self.assertNotIn('python3 linux/campus_linux.py', readme)
            self.assertNotIn('unittest discover', readme)
            self.assertNotIn('GitHub Actions', readme)
        self.assertIn('[portal](https://www1.szu.edu.cn/nc/view.asp?id=639)',
                      self.files('linux')['README.md'].decode('utf-8'))

    def test_private_files_and_unlisted_source_are_never_packaged(self):
        with tempfile.TemporaryDirectory(prefix='campus-source-') as temporary:
            source = Path(temporary) / 'source'
            for mapping in packaging.PACKAGES.values():
                for filename in mapping:
                    target = source / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(packaging.ROOT / filename, target)
            for filename in ('outputs/data/settings.json', 'outputs/data/run_state.json',
                             'outputs/data/keepalive.log', 'outputs/__pycache__/extra.pyc',
                             'linux/secret.key', 'linux/portal', 'linux/portal.rar',
                             'linux/.venv/fake', 'tests/private.txt', '.git/config'):
                target = source / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('test-only excluded data', encoding='utf-8')
            actual = packaging.build_packages(Path(temporary) / 'dist', source=source)
            for platform, archive in zip(('windows', 'linux'), actual):
                self.assertEqual(archive.read_bytes(), self.archives[platform].read_bytes())

    def test_one_platform_can_be_built_without_the_other_archive(self):
        with tempfile.TemporaryDirectory(prefix='campus-single-package-') as temporary:
            for platform in ('windows', 'linux'):
                destination = Path(temporary) / platform
                built = packaging.build_packages(destination, platform=platform)
                self.assertEqual(len(built), 1)
                self.assertEqual(list(destination.iterdir()), built)
                self.assertEqual(built[0].name, self.archives[platform].name)


if __name__ == '__main__':
    unittest.main()
