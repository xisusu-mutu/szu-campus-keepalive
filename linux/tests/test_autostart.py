"""Service ownership regressions; the systemd boundary is isolated from user units."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_linux as desktop
import platform_linux as platform


class UserManager:
    """A second configuration owns the same service name in the user manager."""
    def __init__(self, fragment):
        self.fragment = fragment
        self.enabled, self.active = True, False

    def __call__(self, arguments, **kwargs):
        if arguments[0] == 'loginctl':
            return subprocess.CompletedProcess(arguments, 0, 'yes\n', '')
        if arguments[:2] != ['systemctl', '--user'] or arguments[-1] != platform.UNIT_NAME:
            raise AssertionError(arguments)
        action = arguments[2]
        code, output = 0, ''
        if action == 'show':
            output = str(self.fragment) + '\n' if self.fragment else '\n'
        elif action == 'is-enabled':
            code, output = (0, 'enabled\n') if self.enabled else (1, 'disabled\n')
        elif action == 'is-active':
            code, output = (0, 'active\n') if self.active else (3, 'inactive\n')
        elif action in ('enable', 'disable'):
            self.enabled = action == 'enable'
        elif action == 'start':
            self.active = True
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(arguments, code, output, '')


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.paths = platform.Paths(self.root/'new/config', self.root/'new/state',
                                    self.root/'new/systemd/user'/platform.UNIT_NAME)
        self.manager = UserManager(self.root/'existing/systemd/user'/platform.UNIT_NAME)
        self.startup = platform.AutoStart(self.paths, self.manager)
        uid = patch.object(platform.os, 'getuid', return_value=1000, create=True)
        uid.start()
        self.addCleanup(uid.stop)

    def local_unit(self, content=platform.UNIT_MARKER+'\n[Service]\n'):
        self.paths.unit.parent.mkdir(parents=True, exist_ok=True)
        self.paths.unit.write_text(content, encoding='utf-8')

    def test_uninstalled_configuration_does_not_inherit_enabled_service(self):
        self.manager.active = True
        state = self.startup.status()
        self.assertFalse(state['installed'])
        self.assertFalse(state['enabled'])
        self.assertFalse(state['boot_enabled'])
        self.assertNotEqual(state['active'], 'active')
        self.assertFalse(state['error'])

    def test_local_copy_is_not_the_service_loaded_by_the_manager(self):
        self.local_unit()
        state = self.startup.status()
        self.assertFalse(state['enabled'])
        self.assertFalse(state['boot_enabled'])
        self.assertTrue(state['error'])

    def test_foreign_service_cannot_be_disabled_through_this_configuration(self):
        self.local_unit()
        with self.assertRaises(ValueError):
            self.startup.set_enabled(False)
        self.assertTrue(self.manager.enabled)

    def test_foreign_service_cannot_be_enabled_through_this_configuration(self):
        self.local_unit()
        self.manager.enabled = False
        with self.assertRaises(ValueError):
            self.startup.set_enabled(True)
        self.assertFalse(self.manager.enabled)

    def test_desktop_cannot_start_a_service_from_another_configuration(self):
        self.local_unit()
        ticket = {'app': platform.APP_NAME, 'port': 8765, 'code': 'x'*43}
        request = Mock(side_effect=[desktop.BackendUnavailable(), ticket])
        opened = []
        with patch.dict(os.environ, {'DISPLAY': ':test'}), self.assertRaises(ValueError):
            desktop.open_panel(self.paths, self.startup, request, lambda url: opened.append(url) or True)
        self.assertFalse(self.manager.active)
        self.assertEqual(opened, [])

    def test_unmanaged_local_file_is_not_reported_as_enabled(self):
        self.local_unit('[Service]\nExecStart=/unrelated\n')
        self.manager.fragment = self.paths.unit
        state = self.startup.status()
        self.assertFalse(state['enabled'])
        self.assertTrue(state['error'])

    def test_manager_without_a_fragment_does_not_claim_autostart(self):
        self.local_unit()
        self.manager.fragment = None
        state = self.startup.status()
        self.assertFalse(state['enabled'])
        self.assertTrue(state['error'])


if __name__ == '__main__':
    unittest.main()
