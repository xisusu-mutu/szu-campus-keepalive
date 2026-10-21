"""Build independent Windows and Linux downloads from an explicit file list."""
from __future__ import annotations

import argparse
import gzip
import io
from pathlib import Path
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    'windows': {
        'outputs/campus_keepalive.py': 'campus_keepalive.py',
        'outputs/campus_core.py': 'campus_core.py',
        'outputs/campus_tray.py': 'campus_tray.py',
        'outputs/启动校园网保活.vbs': '启动校园网保活.vbs',
        'outputs/排错启动.bat': '排错启动.bat',
        'outputs/使用说明.md': 'README.md',
    },
    'linux': {
        'outputs/campus_core.py': 'campus_core.py',
        'linux/campus_linux.py': 'campus_linux.py',
        'linux/auth_linux.py': 'auth_linux.py',
        'linux/platform_linux.py': 'platform_linux.py',
        'linux/desktop_linux.py': 'desktop_linux.py',
        'linux/requirements.txt': 'requirements.txt',
        'linux/README.md': 'README.md',
        'linux/web/index.html': 'web/index.html',
        'linux/web/style.css': 'web/style.css',
        'linux/web/app.js': 'web/app.js',
    },
}


def package_files(platform, source):
    prefix = f'szu-campus-keepalive-{platform}'
    for original, target in PACKAGES[platform].items():
        yield f'{prefix}/{target}', (source / original).read_bytes()


def build_packages(destination, source=ROOT, platform='all'):
    destination, source = Path(destination), Path(source)
    destination.mkdir(parents=True, exist_ok=True)
    built = []
    for name in PACKAGES if platform == 'all' else (platform,):
        prefix = f'szu-campus-keepalive-{name}'
        if name == 'windows':
            path = destination / (prefix + '.zip')
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for member, data in package_files(name, source):
                    info = zipfile.ZipInfo(member, date_time=(2020, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
        else:
            path = destination / (prefix + '.tar.gz')
            with path.open('wb') as stream, gzip.GzipFile(fileobj=stream, mode='wb', mtime=0, filename='') as compressed:
                with tarfile.open(fileobj=compressed, mode='w') as archive:
                    for member, data in package_files(name, source):
                        info = tarfile.TarInfo(member)
                        info.size = len(data)
                        info.mode = 0o644
                        archive.addfile(info, io.BytesIO(data))
        built.append(path)
    return built


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('all', 'windows', 'linux'), default='all')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    for path in build_packages(args.output, platform=args.platform):
        print(f'{path.name}: {path.stat().st_size} bytes')


if __name__ == '__main__':
    main()
