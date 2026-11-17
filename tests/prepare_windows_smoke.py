"""Prepare a real packaged GUI for native UI verification without touching the user's app.

Only the window title, mutex and tray identities change in this temporary copy.
No saved settings are copied, so normal startup cannot authenticate to the campus network.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    artifact = ROOT / 'tests' / 'artifacts'
    artifact.mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='campus-gui-smoke-'))
    with zipfile.ZipFile(ROOT / 'dist' / 'szu-campus-keepalive-windows.zip') as archive:
        archive.extractall(temporary)
    folder = temporary / 'szu-campus-keepalive-windows'
    suffix = uuid.uuid4().hex
    title = 'SZU Windows Launcher Verification'
    core = folder / 'campus_core.py'
    text = core.read_text(encoding='utf-8')
    text = text.replace("APP_TITLE = '深圳大学校园网保活'", f'APP_TITLE = {title!r}')
    text = text.replace(r'Local\SZUCampusKeepaliveGui', 'Local\\CampusSmoke_' + suffix)
    core.write_text(text, encoding='utf-8')
    tray = folder / 'campus_tray.py'
    text = tray.read_text(encoding='utf-8')
    text = text.replace('SZUCampusKeepalive.TrayWindow', 'CampusSmokeWindow_' + suffix)
    text = text.replace('SZUCampusKeepalive.Tray', 'CampusSmokeTray_' + suffix)
    tray.write_text(text, encoding='utf-8')
    env = dict(os.environ)
    env['CAMPUS_PYTHON'] = sys.executable
    for name in ('VIRTUAL_ENV', 'CONDA_PREFIX', 'PYTHONHOME', 'PYTHONPATH'):
        env.pop(name, None)
    system = Path(os.environ['SystemRoot']) / 'System32'
    env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + str(system)
    process = subprocess.Popen([str(system / 'wscript.exe'), str(folder / '启动校园网保活.vbs')], env=env)
    if process.wait(timeout=20) != 0:
        raise RuntimeError((folder / 'data' / 'launcher.log').read_text(encoding='utf-8-sig'))
    metadata = {'temporary': str(temporary), 'folder': str(folder), 'title': title,
                'python': sys.executable, 'namespace': suffix}
    (artifact / 'windows-smoke-session.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == '__main__':
    main()
