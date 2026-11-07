"""Windows API boundary tests; no game process is started or stopped."""
import ctypes
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import windows_ui


class Kernel:
    def __init__(self, names=(), snapshot=123, error=18):
        self.names = iter(names)
        self.snapshot = snapshot
        self.error = error
        self.closed = []

    def CreateToolhelp32Snapshot(self, flags, pid):
        assert (flags, pid) == (2, 0)
        return self.snapshot

    def _next(self, handle, pointer):
        assert handle == self.snapshot
        entry = pointer._obj
        assert entry.dwSize == ctypes.sizeof(windows_ui.PROCESSENTRY32W)
        try:
            entry.szExeFile = next(self.names)
            return 1
        except StopIteration:
            return 0

    Process32FirstW = _next
    Process32NextW = _next

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


class Dialog:
    def __init__(self, filename=None, error=0):
        self.filename = filename
        self.error = error

    def GetOpenFileNameW(self, pointer):
        options = pointer._obj
        assert options.lStructSize == ctypes.sizeof(windows_ui.OPENFILENAMEW)
        assert options.Flags & 0x1000  # existing file
        assert options.Flags & 0x800  # existing folder
        assert options.Flags & 0x8  # preserve process working directory
        assert options.nMaxFile >= 32768
        if self.filename is None:
            return 0
        buffer = ctypes.create_unicode_buffer(self.filename)
        ctypes.memmove(options.lpstrFile, buffer, ctypes.sizeof(buffer))
        return 1

    def CommDlgExtendedError(self):
        return self.error


class ProcessTests(unittest.TestCase):
    def test_running_game_case_insensitive_and_handle_closed(self):
        api = Kernel(['explorer.exe', 'MHGURecomp.EXE', 'notepad.exe'])
        self.assertTrue(windows_ui._snapshot_contains_game(api, lambda: api.error))
        self.assertEqual(api.closed, [123])

    def test_other_processes_do_not_block(self):
        api = Kernel(['explorer.exe', 'mhgurecomp-helper.exe'])
        self.assertFalse(windows_ui._snapshot_contains_game(api, lambda: api.error))
        self.assertEqual(api.closed, [123])

    def test_snapshot_failure_refuses_to_assume_game_closed(self):
        for handle in [None, 0, ctypes.c_void_p(-1).value]:
            with self.subTest(handle=handle), self.assertRaises(RuntimeError):
                windows_ui._snapshot_contains_game(Kernel(snapshot=handle), lambda: 5)

    def test_enumeration_failure_refuses_to_assume_game_closed(self):
        api = Kernel(['explorer.exe'], error=5)
        with self.assertRaises(RuntimeError):
            windows_ui._snapshot_contains_game(api, lambda: api.error)
        self.assertEqual(api.closed, [123])


class PickerTests(unittest.TestCase):
    def test_selected_existing_executable_returns_folder(self):
        filename = str(Path('game folder') / 'mhgurecomp.exe')
        self.assertEqual(windows_ui._pick_game_dir(Dialog(filename)), Path(filename).parent)

    def test_cancel_returns_none(self):
        self.assertIsNone(windows_ui._pick_game_dir(Dialog()))

    def test_dialog_failure_is_reported(self):
        with self.assertRaises(RuntimeError):
            windows_ui._pick_game_dir(Dialog(error=0x3003))


class Mutex:
    def __init__(self, handle=77):
        self.handle = handle
        self.closed = []
        self.released = []

    def CreateMutexW(self, security, initial_owner, name):
        assert security is None and initial_owner is True
        assert name.startswith('Local\\MHGU-Mod-Updater-')
        return self.handle

    def ReleaseMutex(self, handle):
        self.released.append(handle)
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


class LockTests(unittest.TestCase):
    def test_existing_windows_mutex_refuses_and_closes_unowned_handle(self):
        api = Mutex()
        with self.assertRaises(RuntimeError):
            with windows_ui._windows_game_lock('Local\\MHGU-Mod-Updater-test', api, lambda: 183):
                self.fail('An existing updater lock was acquired')
        self.assertEqual(api.closed, [77])
        self.assertEqual(api.released, [])

    def test_windows_mutex_released_after_operation_error(self):
        api = Mutex()
        with self.assertRaises(ValueError):
            with windows_ui._windows_game_lock('Local\\MHGU-Mod-Updater-test', api, lambda: 0):
                raise ValueError('simulated update failure')
        self.assertEqual(api.released, [77])
        self.assertEqual(api.closed, [77])

    def test_windows_mutex_creation_failure_refuses_update(self):
        api = Mutex(handle=None)
        with self.assertRaises(RuntimeError):
            with windows_ui._windows_game_lock('Local\\MHGU-Mod-Updater-test', api, lambda: 5):
                self.fail('Failed mutex creation was accepted')

    def test_second_process_cannot_update_same_game_folder(self):
        code = '''import sys
from windows_ui import game_lock
try:
    with game_lock(sys.argv[1]):
        sys.exit(10)
except RuntimeError:
    sys.exit(0)
'''
        with tempfile.TemporaryDirectory() as temporary:
            with windows_ui.game_lock(Path(temporary)):
                result = subprocess.run([sys.executable, '-c', code, temporary],
                                        cwd=Path(__file__).resolve().parents[1],
                                        capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            # OS releases the lock immediately after the owning context exits.
            with windows_ui.game_lock(Path(temporary)):
                pass


if __name__ == '__main__':
    unittest.main()
