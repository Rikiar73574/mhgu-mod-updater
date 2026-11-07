"""Build the single-file updater with official Windows CPython and PyInstaller."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import ssl
import sys


ROOT = Path(__file__).resolve().parent


def copy_distribution_license(distribution, suffix, target):
    package = importlib.metadata.distribution(distribution)
    matches = [item for item in package.files or () if str(item).replace('\\', '/').endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(f'Cannot identify exact {distribution} license: {matches}')
    shutil.copyfile(package.locate_file(matches[0]), target)


def main():
    if sys.platform != 'win32' or platform.machine().upper() not in ('AMD64', 'X86_64'):
        raise SystemExit('Build with the official 64-bit Windows CPython runtime.')
    if sys.version_info[:3] != (3, 12, 10):
        raise SystemExit('This release build requires Windows CPython 3.12.10.')
    if importlib.metadata.version('pyinstaller') != '6.22.3':
        raise SystemExit('This release build requires PyInstaller 6.22.3.')
    if not (ROOT / 'launcher.py').is_file():
        raise SystemExit('Missing launcher.py entrypoint.')

    resources = ROOT / 'build' / 'licenses'
    resources.mkdir(parents=True, exist_ok=True)
    for item in resources.iterdir():
        if item.is_file():
            item.unlink()
    shutil.copyfile(Path(sys.base_prefix) / 'LICENSE.txt', resources / 'CPython-Windows-3.12.10-LICENSE.txt')
    shutil.copyfile(ROOT / 'LICENSE', resources / 'MHGU-Mod-Updater-LICENSE.txt')
    copy_distribution_license('pyinstaller', '/licenses/COPYING.txt', resources / 'PyInstaller-6.22.3-COPYING.txt')
    copy_distribution_license('pyinstaller-hooks-contrib', '/licenses/LICENSE', resources / 'PyInstaller-hooks-contrib-LICENSE.txt')
    for item in (ROOT / 'tools' / 'licenses').iterdir():
        if item.is_file():
            shutil.copyfile(item, resources / item.name)
    (resources / 'THIRD_PARTY_NOTICES.txt').write_text(
        'MHGU Mod Updater bundles unmodified CPython 3.12.10 and its standard library.\n'
        'The executable is packaged by PyInstaller 6.22.3 using its bootloader exception.\n'
        'No modifications to Python, OpenSSL, or the PyInstaller bootloader are made.\n'
        'Windows runtime redistribution terms are included in CPython-Windows-3.12.10-LICENSE.txt.\n'
        f'HTTPS runtime: {ssl.OPENSSL_VERSION}.\n'
        'CPython source: https://github.com/python/cpython/tree/v3.12.10\n'
        'OpenSSL source/license: https://github.com/openssl/openssl/tree/openssl-3.0.16\n'
        'PyInstaller source/license: https://github.com/pyinstaller/pyinstaller/tree/v6.22.3\n'
        'PyInstaller hooks: https://github.com/pyinstaller/pyinstaller-hooks-contrib\n',
        encoding='utf-8',
    )
    source_hashes = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ('launcher.py', 'updater.py', 'self_update.py', 'windows_ui.py', 'build_windows.py',
                     'assets/updater.ico', 'assets/updater.png')
    }
    buildinfo = {
        'application': 'MHGU-Mod-Updater',
        'python': sys.version,
        'python_platform': sys.platform,
        'machine': platform.machine(),
        'pyinstaller': importlib.metadata.version('pyinstaller'),
        'openssl': ssl.OPENSSL_VERSION,
        'python_installer_url': 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe',
        'python_installer_sha256': '67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb',
        'source_sha256': source_hashes,
        'license_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(resources.iterdir())},
    }
    (resources / 'BUILDINFO.json').write_text(json.dumps(buildinfo, indent=2) + '\n', encoding='utf-8')

    import PyInstaller.__main__
    PyInstaller.__main__.run([
        '--noconfirm', '--clean', '--noupx', '--onefile', '--console',
        '--name', 'MHGU-Mod-Updater', '--icon', str(ROOT / 'assets' / 'updater.ico'),
        '--distpath', str(ROOT / 'dist'),
        '--workpath', str(ROOT / 'build' / 'windows-work'),
        '--specpath', str(ROOT / 'build'),
        '--add-data', f'{resources};licenses',
        '--exclude-module', 'PySide6', '--exclude-module', 'shiboken6',
        '--exclude-module', 'tkinter', '--exclude-module', '_tkinter',
        '--paths', str(ROOT), str(ROOT / 'launcher.py'),
    ])
    artifact = ROOT / 'dist' / 'MHGU-Mod-Updater.exe'
    buildinfo['artifact_sha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    buildinfo['artifact_bytes'] = artifact.stat().st_size
    (ROOT / 'build' / 'windows-provenance.json').write_text(json.dumps(buildinfo, indent=2) + '\n', encoding='utf-8')
    print(f"{buildinfo['artifact_sha256']}  {artifact.name}")


if __name__ == '__main__':
    main()
