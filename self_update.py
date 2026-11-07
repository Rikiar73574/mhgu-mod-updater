"""Verified Windows updater handoff; no shell scripts or game process control."""
import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

from updater import GitHubClient, UpdateError, assert_game_closed, download_asset, require_plain, sha256
from windows_ui import game_lock, game_running

VERSION = '0.1.1'


@dataclass(frozen=True)
class SelfRelease:
    name: str = 'MHGU Mod Updater'
    repo: str = 'mhgu-mod-updater'
    asset: str = 'MHGU-Mod-Updater.exe'
    api_url: str = 'https://api.github.com/repos/Rikiar73574/mhgu-mod-updater/releases/latest'


SELF = SelfRelease()


def frozen_windows():
    return os.name == 'nt' and getattr(sys, 'frozen', False)


def version_tuple(version):
    match = re.fullmatch(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version)
    if not match:
        raise UpdateError('Updater release does not have a stable semantic version')
    return tuple(int(value) for value in match.groups())


def validate_pe(data):
    """Require a bounded, executable AMD64 PE32+ image before staging it."""
    def invalid():
        raise UpdateError('Updater asset is not a valid 64-bit Windows executable')

    if len(data) < 64 or data[:2] != b'MZ':
        invalid()
    start = struct.unpack_from('<I', data, 0x3c)[0]
    if start < 64 or start+24 > len(data) or data[start:start+4] != b'PE\0\0':
        invalid()
    machine, sections, _, _, _, optional_size, flags = struct.unpack_from('<HHIIIHH', data, start+4)
    optional = start+24
    table = optional+optional_size
    if (machine != 0x8664 or not 1 <= sections <= 96 or optional_size < 112
            or table+40*sections > len(data) or not flags & 2 or flags & 0x2000):
        invalid()
    if struct.unpack_from('<H', data, optional)[0] != 0x20b:
        invalid()
    entry = struct.unpack_from('<I', data, optional+16)[0]
    image_size, headers = struct.unpack_from('<II', data, optional+56)
    if not 0 < entry < image_size or not table+40*sections <= headers <= len(data):
        invalid()
    for index in range(sections):
        size, address, raw_size, raw = struct.unpack_from('<IIII', data, table+40*index+8)
        if address+max(size, raw_size) > image_size or raw_size and (raw < headers or raw+raw_size > len(data)):
            invalid()


def plain_path(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        require_plain(item)
    return path.resolve()


def stage_job(target, data, game_dir, *, no_pause=False):
    validate_pe(data)
    target = plain_path(target)
    original = target.read_bytes()
    validate_pe(original)
    directory = Path(tempfile.mkdtemp(prefix='.mhgu-self-update-', dir=target.parent))
    try:
        (directory/'update.exe').write_bytes(data)
        shutil.copy2(target, directory/'helper.exe')
        job = directory/'job.json'
        job.write_text(json.dumps({
            'schema': 1, 'target': str(target), 'game_dir': str(Path(game_dir).resolve()),
            'pid': os.getpid(), 'sha256': sha256(data), 'old_sha256': sha256(original),
            'no_pause': bool(no_pause),
        }, indent=2)+'\n', encoding='utf-8')
        return job
    except BaseException:
        shutil.rmtree(directory)
        raise


def maybe_self_update(*, game_dir, check=False, no_self_update=False, client=None, log=print, no_pause=False):
    if no_self_update or not frozen_windows():
        return False
    game = Path(game_dir).resolve()
    if not game.is_dir() or not any(p.is_file() and p.name.casefold() == 'mhgurecomp.exe' for p in game.iterdir()):
        raise UpdateError('Choose the folder containing mhgurecomp.exe')
    with game_lock(game):
        assert_game_closed(game_running)
        client = client or GitHubClient()
        log(f'Checking MHGU Mod Updater (current {VERSION})...')
        release = client.latest(SELF)
        if release is None or version_tuple(release.version) <= version_tuple(VERSION):
            return False
        data = download_asset(client, release, SELF)
        validate_pe(data)
        log(f'MHGU Mod Updater: update available ({release.version}).')
        if check:
            return False
        assert_game_closed(game_running)
        if sha256(Path(sys.executable).read_bytes()) == sha256(data):
            return False
        job = stage_job(Path(sys.executable), data, game, no_pause=no_pause)
        environment = os.environ.copy()
        environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        with (job.parent/'helper.log').open('wb') as output:
            subprocess.Popen([str(job.parent/'helper.exe'), '--apply-self-update', str(job)],
                             cwd=job.parent, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=environment)
        log('Restarting the updated updater before changing mods...')
        return True


def _wait_for_process(pid, api, last_error, timeout_ms=60000):
    handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only; never terminate.
    if not handle:
        if last_error() == 87:  # The original process has already exited.
            return
        raise UpdateError('Could not wait for the original updater process')
    try:
        result = api.WaitForSingleObject(handle, timeout_ms)
        if result == 0x102:
            raise UpdateError('Original updater exit wait timeout; executable unchanged')
        if result != 0:
            raise UpdateError('Could not wait for the original updater process')
    finally:
        api.CloseHandle(handle)


def wait_for_process(pid):
    if os.name != 'nt':
        raise UpdateError('The self-update helper requires Windows')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    api.OpenProcess.restype = ctypes.c_void_p
    api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    api.WaitForSingleObject.restype = ctypes.c_uint32
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int
    _wait_for_process(pid, api, ctypes.get_last_error)


def replace_file(source, target):
    # The PyInstaller bootloader parent can retain the image after Python exits.
    deadline = time.monotonic()+10
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def restart(target, game, no_pause):
    command = [str(target), '--game-dir', str(game), '--no-self-update']
    if no_pause:
        command.append('--no-pause')
    environment = os.environ.copy()
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    subprocess.Popen(command, cwd=target.parent, creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0), env=environment)


def apply_update(job_path, helper):
    """Only fixed adjacent files from this running helper may be replaced."""
    job_path, helper = plain_path(job_path), plain_path(helper)
    directory = job_path.parent
    if job_path.name != 'job.json' or helper != directory/'helper.exe' or not directory.name.startswith('.mhgu-self-update-'):
        raise UpdateError('Invalid self-update helper location')
    job = json.loads(job_path.read_text(encoding='utf-8'))
    if (job.get('schema') != 1 or type(job.get('pid')) is not int or not 0 < job['pid'] <= 0xffffffff
            or type(job.get('no_pause')) is not bool
            or any(not isinstance(job.get(key), str) for key in ('target', 'game_dir', 'sha256', 'old_sha256'))):
        raise UpdateError('Invalid self-update job')
    target = plain_path(job['target'])
    if (target.parent != directory.parent or target.suffix.casefold() != '.exe'
            or target == helper or not target.is_file()):
        raise UpdateError('Invalid self-update target')
    staged, backup = directory/'update.exe', directory/'previous.exe.bak'
    for path in (staged, backup):
        plain_path(path)
    if backup.exists():
        raise UpdateError('Self-update backup already exists; refusing to overwrite it')
    data, old = staged.read_bytes(), target.read_bytes()
    if sha256(data) != job['sha256'] or sha256(helper.read_bytes()) != job['old_sha256']:
        raise UpdateError('Self-update checksum mismatch; executable unchanged')
    validate_pe(data)
    validate_pe(old)
    if sha256(old) == job['sha256']:
        return  # Another helper already installed exactly these verified bytes.
    if sha256(old) != job['old_sha256']:
        raise UpdateError('Self-update checksum mismatch; executable unchanged')
    wait_for_process(job['pid'])
    with game_lock(Path(job['game_dir'])):
        # Recheck after the wait, under the same game lock as normal updates.
        current = sha256(target.read_bytes())
        if current == job['sha256']:
            return
        if sha256(staged.read_bytes()) != job['sha256'] or current != job['old_sha256']:
            raise UpdateError('Self-update checksum changed while waiting')
        try:
            replace_file(target, backup)
            replace_file(staged, target)
        except BaseException:
            # Keep the old backup even when restoring it to the original name.
            if backup.exists():
                if target.exists() and sha256(target.read_bytes()) not in (job['old_sha256'], job['sha256']):
                    raise UpdateError(f'Updater changed during replacement; retained backup at {backup}')
                restore = directory/'restore.exe'
                shutil.copy2(backup, restore)
                replace_file(restore, target)
            raise
    try:
        restart(target, Path(job['game_dir']), job['no_pause'])
    except BaseException:
        with game_lock(Path(job['game_dir'])):
            if sha256(target.read_bytes()) != job['sha256']:
                raise UpdateError(f'Updater changed after replacement; retained backup at {backup}')
            restore = directory/'restore.exe'
            shutil.copy2(backup, restore)
            replace_file(restore, target)
        raise


def show_error(message):
    print(message)
    if os.name == 'nt':
        api = ctypes.WinDLL('user32', use_last_error=True)
        api.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        api.MessageBoxW.restype = ctypes.c_int
        api.MessageBoxW(None, message, 'MHGU Mod Updater', 0x10)


def apply_job(path):
    if not frozen_windows():
        print('ERROR: The self-update helper requires the packaged Windows updater.')
        return 1
    try:
        apply_update(path, Path(sys.executable))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        show_error(f'Self-update failed: {str(error)[:1000]}\n\nMods were not changed by this handoff.\n'
                   f'Old executable backup (if created): {Path(path).parent / "previous.exe.bak"}\n'
                   f'Details: {Path(path).parent / "helper.log"}')
        return 1
