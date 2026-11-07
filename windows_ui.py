"""Small Windows API boundary; no GUI framework or game process control."""
import contextlib
import ctypes
import hashlib
import os
from pathlib import Path
import tempfile


DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
HANDLE = ctypes.c_void_p
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_NO_MORE_FILES = 18
ERROR_ALREADY_EXISTS = 183


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', DWORD), ('cntUsage', DWORD), ('th32ProcessID', DWORD),
        ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', DWORD),
        ('cntThreads', DWORD), ('th32ParentProcessID', DWORD),
        ('pcPriClassBase', ctypes.c_int32), ('dwFlags', DWORD),
        ('szExeFile', ctypes.c_wchar * 260),
    ]


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ('lStructSize', DWORD), ('hwndOwner', HANDLE), ('hInstance', HANDLE),
        ('lpstrFilter', ctypes.c_wchar_p),
        ('lpstrCustomFilter', ctypes.POINTER(ctypes.c_wchar)),
        ('nMaxCustFilter', DWORD), ('nFilterIndex', DWORD),
        ('lpstrFile', ctypes.POINTER(ctypes.c_wchar)), ('nMaxFile', DWORD),
        ('lpstrFileTitle', ctypes.POINTER(ctypes.c_wchar)),
        ('nMaxFileTitle', DWORD), ('lpstrInitialDir', ctypes.c_wchar_p),
        ('lpstrTitle', ctypes.c_wchar_p), ('Flags', DWORD),
        ('nFileOffset', WORD), ('nFileExtension', WORD),
        ('lpstrDefExt', ctypes.c_wchar_p), ('lCustData', ctypes.c_ssize_t),
        ('lpfnHook', HANDLE), ('lpTemplateName', ctypes.c_wchar_p),
        ('pvReserved', HANDLE), ('dwReserved', DWORD), ('FlagsEx', DWORD),
    ]


def _snapshot_contains_game(api, last_error):
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, 0, INVALID_HANDLE_VALUE):
        raise RuntimeError(f'Could not inspect running games (Windows error {last_error()}).')
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        found = api.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.casefold() == 'mhgurecomp.exe':
                return True
            found = api.Process32NextW(snapshot, ctypes.byref(entry))
        error = last_error()
        if error != ERROR_NO_MORE_FILES:
            raise RuntimeError(f'Could not finish checking running games (Windows error {error}).')
        return False
    finally:
        if not api.CloseHandle(snapshot):
            raise RuntimeError('Could not close the Windows process snapshot.')


def game_running():
    """Return whether MHGURecomp is running; inspection failures refuse updates."""
    if os.name != 'nt':
        return False
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [DWORD, DWORD]
    api.CreateToolhelp32Snapshot.restype = HANDLE
    for name in ('Process32FirstW', 'Process32NextW'):
        function = getattr(api, name)
        function.argtypes = [HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        function.restype = ctypes.c_int
    api.CloseHandle.argtypes = [HANDLE]
    api.CloseHandle.restype = ctypes.c_int
    return _snapshot_contains_game(api, ctypes.get_last_error)


def _pick_game_dir(api):
    buffer = ctypes.create_unicode_buffer(32768)
    options = OPENFILENAMEW()
    options.lStructSize = ctypes.sizeof(options)
    options.lpstrFilter = 'MHGURecomp (mhgurecomp.exe)\0mhgurecomp.exe\0\0'
    options.lpstrFile = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_wchar))
    options.nMaxFile = len(buffer)
    options.lpstrTitle = 'Choose your MHGURecomp game executable'
    options.Flags = 0x80000 | 0x1000 | 0x800 | 0x8
    if not api.GetOpenFileNameW(ctypes.byref(options)):
        error = api.CommDlgExtendedError()
        if error:
            raise RuntimeError(f'Could not open the game picker (Windows error 0x{error:04x}).')
        return None
    return Path(buffer.value).parent


def choose_game_dir():
    """Select the game executable without changing files or starting the game."""
    if os.name != 'nt':
        raise RuntimeError('The native game picker requires Windows. Use --game-dir instead.')
    api = ctypes.WinDLL('comdlg32', use_last_error=True)
    api.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    api.GetOpenFileNameW.restype = ctypes.c_int
    api.CommDlgExtendedError.argtypes = []
    api.CommDlgExtendedError.restype = DWORD
    return _pick_game_dir(api)


@contextlib.contextmanager
def _windows_game_lock(name, api, last_error):
    mutex = api.CreateMutexW(None, True, name)
    error = last_error()
    if not mutex:
        raise RuntimeError(f'Could not lock the game folder (Windows error {error}).')
    if error == ERROR_ALREADY_EXISTS:
        api.CloseHandle(mutex)
        raise RuntimeError('Another mod updater is already using this game folder.')
    try:
        yield
    finally:
        released = api.ReleaseMutex(mutex)
        closed = api.CloseHandle(mutex)
        if not released or not closed:
            raise RuntimeError('Could not release the game-folder update lock.')


@contextlib.contextmanager
def game_lock(game_dir):
    """Hold one OS-owned lock per canonical game folder for the entire update."""
    key = hashlib.sha256(str(Path(game_dir).resolve()).casefold().encode('utf-8')).hexdigest()
    if os.name == 'nt':
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateMutexW.argtypes = [HANDLE, ctypes.c_int, ctypes.c_wchar_p]
        api.CreateMutexW.restype = HANDLE
        for name in ('ReleaseMutex', 'CloseHandle'):
            function = getattr(api, name)
            function.argtypes = [HANDLE]
            function.restype = ctypes.c_int
        with _windows_game_lock(f'Local\\MHGU-Mod-Updater-{key}', api, ctypes.get_last_error):
            yield
        return
    # Source tests and explicit CLI use on Linux get the same exclusion rule.
    import fcntl
    descriptor = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(Path(tempfile.gettempdir()) / f'mhgu-mod-updater-{key}.lock', flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another mod updater is already using this game folder.') from error
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
