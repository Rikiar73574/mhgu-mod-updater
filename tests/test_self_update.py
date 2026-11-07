import contextlib
import ctypes
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import launcher
import self_update
import updater


def pe(marker=b'new', machine=0x8664):
    """Synthetic PE32+ image; never executed."""
    data = bytearray(1024)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 0x84, machine, 1, 0, 0, 0, 240, 0x22)
    struct.pack_into('<H', data, 0x98, 0x20b)
    struct.pack_into('<I', data, 0x98+16, 0x1000)
    struct.pack_into('<II', data, 0x98+56, 0x2000, 512)
    section = 0x98+240
    struct.pack_into('<IIII', data, section+8, 512, 0x1000, 512, 512)
    data[512:512+len(marker)] = marker
    return bytes(data)


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.game = self.root/'game'
        self.game.mkdir()
        (self.game/'mhgurecomp.exe').write_bytes(b'game fixture; never run')
        self.target = self.root/'MHGU-Mod-Updater.exe'
        self.old = pe(b'old')
        self.new = pe(b'new')
        self.target.write_bytes(self.old)
        self.payloads = {}
        self.requests = []
        outer = self

        class Client(updater.GitHubClient):
            def request(self, url, limit):
                outer.requests.append(url)
                return outer.payloads[url]

        self.client = Client()

    def release(self, version='v0.1.2', data=None, digest=None):
        spec = self_update.SELF
        data = self.new if data is None else data
        url = f'https://github.com/Rikiar73574/mhgu-mod-updater/releases/download/{version}/MHGU-Mod-Updater.exe'
        self.payloads[spec.api_url] = json.dumps({'tag_name': version, 'draft': False, 'prerelease': False, 'assets': [
            {'name': spec.asset, 'browser_download_url': url, 'size': len(data),
             'digest': 'sha256:'+(digest or updater.sha256(data))},
        ]}).encode()
        self.payloads[url] = data

    def run_check(self, **options):
        with patch.object(self_update, 'frozen_windows', return_value=True), \
             patch.object(self_update.sys, 'executable', str(self.target)), \
             patch.object(self_update, 'game_running', return_value=False):
            return self_update.maybe_self_update(game_dir=self.game, client=self.client, log=lambda _: None, **options)

    def job(self):
        return self_update.stage_job(self.target, self.new, self.game, no_pause=True)

    def test_only_greater_semantic_versions_upgrade(self):
        for version in ['v0.1.1', 'v0.1.0', 'v0.0.99']:
            with self.subTest(version=version):
                self.release(version)
                self.assertFalse(self.run_check())
        self.assertEqual(set(self.root.iterdir()), {self.game, self.target})
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(len(self.requests), 3)

    def test_source_and_one_hop_bypass_do_not_network(self):
        with patch.object(self_update, 'frozen_windows', return_value=False):
            self.assertFalse(self_update.maybe_self_update(game_dir=self.game, client=self.client))
        self.assertFalse(self.run_check(no_self_update=True))
        self.assertEqual(self.requests, [])

    def test_bad_checksum_changes_nothing_and_never_starts_helper(self):
        self.release(digest='0'*64)
        with patch.object(self_update.subprocess, 'Popen') as spawn, self.assertRaisesRegex(updater.UpdateError, 'checksum'):
            self.run_check()
        spawn.assert_not_called()
        self.assertEqual(set(self.root.iterdir()), {self.game, self.target})
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_check_verifies_new_image_but_never_stages_or_launches(self):
        self.release()
        with patch.object(self_update.subprocess, 'Popen') as spawn:
            self.assertFalse(self.run_check(check=True))
        spawn.assert_not_called()
        self.assertEqual(set(self.root.iterdir()), {self.game, self.target})

    def test_new_tag_with_identical_executable_continues_without_handoff(self):
        self.release(data=self.old)
        with patch.object(self_update.subprocess, 'Popen') as spawn:
            self.assertFalse(self.run_check())
        spawn.assert_not_called()

    def test_running_game_refused_before_network(self):
        with patch.object(self_update, 'frozen_windows', return_value=True), \
             patch.object(self_update, 'game_running', return_value=True), \
             self.assertRaisesRegex(updater.UpdateError, 'running'):
            self_update.maybe_self_update(game_dir=self.game, client=self.client)
        self.assertEqual(self.requests, [])

    def test_unknown_architecture_and_malformed_images_rejected(self):
        for data in [b'MZ', pe(machine=0xaa64), pe(machine=0x14c), pe()[:600], b'not a PE image']:
            with self.subTest(bytes=len(data)), self.assertRaises(updater.UpdateError):
                self_update.validate_pe(data)
        broken = bytearray(pe())
        struct.pack_into('<I', broken, 0x3c, 0xfffffff0)
        with self.assertRaises(updater.UpdateError):
            self_update.validate_pe(broken)

    def test_verified_new_release_hands_off_without_mod_changes(self):
        self.release()
        with patch.object(self_update.subprocess, 'Popen') as spawn:
            self.assertTrue(self.run_check(no_pause=True))
        args = spawn.call_args.args[0]
        self.assertEqual(args[1], '--apply-self-update')
        job = Path(args[2]); data = json.loads(job.read_text())
        self.assertEqual(Path(args[0]).read_bytes(), self.old)
        self.assertEqual((job.parent/'update.exe').read_bytes(), self.new)
        self.assertEqual(data['sha256'], updater.sha256(self.new))
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertFalse((self.game/'mods').exists())

    def test_helper_timeout_leaves_original_and_staged_files_untouched(self):
        job = self.job()
        with patch.object(self_update, 'wait_for_process', side_effect=updater.UpdateError('wait timeout')), \
             patch.object(self_update, 'restart') as restart, self.assertRaisesRegex(updater.UpdateError, 'timeout'):
            self_update.apply_update(job, job.parent/'helper.exe')
        restart.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual((job.parent/'update.exe').read_bytes(), self.new)
        self.assertFalse((job.parent/'previous.exe.bak').exists())

    def test_helper_reverifies_staged_hash_before_wait_or_replace(self):
        job = self.job(); (job.parent/'update.exe').write_bytes(pe(b'tampered'))
        with patch.object(self_update, 'wait_for_process') as wait, self.assertRaisesRegex(updater.UpdateError, 'checksum'):
            self_update.apply_update(job, job.parent/'helper.exe')
        wait.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_job_cannot_target_outside_adjacent_directory(self):
        job = self.job(); data = json.loads(job.read_text()); data['target'] = str(self.game/'victim.exe'); job.write_text(json.dumps(data))
        with patch.object(self_update, 'wait_for_process') as wait, self.assertRaises(updater.UpdateError):
            self_update.apply_update(job, job.parent/'helper.exe')
        wait.assert_not_called()
        self.assertFalse((self.game/'victim.exe').exists())

    def test_backup_failure_never_replaces_original(self):
        job = self.job()
        with patch.object(self_update, 'wait_for_process'), \
             patch.object(self_update, 'replace_file', side_effect=PermissionError('locked')), \
             self.assertRaises(PermissionError):
            self_update.apply_update(job, job.parent/'helper.exe')
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_replace_failure_restores_old_exe_and_retains_backup(self):
        job = self.job(); real_replace = self_update.replace_file

        def fail_new(source, target):
            if source.name == 'update.exe':
                raise OSError('synthetic promotion failure')
            return real_replace(source, target)

        with patch.object(self_update, 'wait_for_process'), patch.object(self_update, 'replace_file', side_effect=fail_new), \
             self.assertRaisesRegex(OSError, 'promotion'):
            self_update.apply_update(job, job.parent/'helper.exe')
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual((job.parent/'previous.exe.bak').read_bytes(), self.old)

    def test_promotion_failure_preserves_external_target(self):
        job = self.job(); real_replace = self_update.replace_file
        external = pe(b'external newer executable')

        def fail_new(source, target):
            if source.name == 'update.exe':
                target.write_bytes(external)
                raise OSError('synthetic promotion failure')
            return real_replace(source, target)

        with patch.object(self_update, 'wait_for_process'), patch.object(self_update, 'replace_file', side_effect=fail_new), \
             self.assertRaisesRegex(updater.UpdateError, 'changed during replacement'):
            self_update.apply_update(job, job.parent/'helper.exe')
        self.assertEqual(self.target.read_bytes(), external)
        self.assertEqual((job.parent/'previous.exe.bak').read_bytes(), self.old)

    def test_helper_waits_then_replaces_retains_backup_and_restarts_one_hop(self):
        job = self.job(); events = []
        def waiting(pid):
            self.assertEqual(self.target.read_bytes(), self.old)
            events.append(('wait', pid))
        def starting(target, game, no_pause):
            self.assertEqual(target.read_bytes(), self.new)
            events.append(('restart', game, no_pause))
        with patch.object(self_update, 'wait_for_process', side_effect=waiting), patch.object(self_update, 'restart', side_effect=starting):
            self_update.apply_update(job, job.parent/'helper.exe')
        self.assertEqual([event[0] for event in events], ['wait', 'restart'])
        self.assertEqual((job.parent/'previous.exe.bak').read_bytes(), self.old)
        with patch.object(self_update.subprocess, 'Popen') as spawn:
            self_update.restart(self.target, self.game, True)
        self.assertEqual(spawn.call_args.args[0], [str(self.target), '--game-dir', str(self.game), '--no-self-update', '--no-pause'])

    def test_second_helper_accepts_already_installed_bytes_without_restart(self):
        first, second = self.job(), self.job()
        with patch.object(self_update, 'wait_for_process'), patch.object(self_update, 'restart') as restart:
            self_update.apply_update(first, first.parent/'helper.exe')
            self_update.apply_update(second, second.parent/'helper.exe')
        restart.assert_called_once()
        self.assertEqual(self.target.read_bytes(), self.new)
        self.assertFalse((second.parent/'previous.exe.bak').exists())

    def test_external_target_change_during_wait_is_preserved(self):
        job = self.job(); changed = pe(b'external newer executable')
        def change_target(pid):
            self.target.write_bytes(changed)
        with patch.object(self_update, 'wait_for_process', side_effect=change_target), \
             patch.object(self_update, 'restart') as restart, self.assertRaisesRegex(updater.UpdateError, 'checksum'):
            self_update.apply_update(job, job.parent/'helper.exe')
        restart.assert_not_called()
        self.assertEqual(self.target.read_bytes(), changed)
        self.assertFalse((job.parent/'previous.exe.bak').exists())

    def test_restart_failure_rolls_back(self):
        job = self.job()
        with patch.object(self_update, 'wait_for_process'), patch.object(self_update, 'restart', side_effect=OSError('cannot start')), \
             self.assertRaisesRegex(OSError, 'cannot start'):
            self_update.apply_update(job, job.parent/'helper.exe')
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual((job.parent/'previous.exe.bak').read_bytes(), self.old)

    def test_restart_failure_does_not_overwrite_externally_changed_target(self):
        job = self.job(); changed = pe(b'newer update won')
        def failed_restart(*args):
            self.target.write_bytes(changed)
            raise OSError('cannot start')
        with patch.object(self_update, 'wait_for_process'), patch.object(self_update, 'restart', side_effect=failed_restart), \
             self.assertRaises(updater.UpdateError):
            self_update.apply_update(job, job.parent/'helper.exe')
        self.assertEqual(self.target.read_bytes(), changed)
        self.assertEqual((job.parent/'previous.exe.bak').read_bytes(), self.old)

    def test_helper_error_is_visible_and_points_to_retained_evidence(self):
        job = self.job()
        with patch.object(self_update, 'frozen_windows', return_value=True), \
             patch.object(self_update.sys, 'executable', str(job.parent/'helper.exe')), \
             patch.object(self_update, 'apply_update', side_effect=updater.UpdateError('wait timeout')), \
             patch.object(self_update, 'show_error') as alert:
            self.assertEqual(self_update.apply_job(job), 1)
        self.assertIn('helper.log', alert.call_args.args[0])
        self.assertIn('Mods were not changed', alert.call_args.args[0])

    def test_interruption_after_backup_rename_restores_original(self):
        job = self.job(); real_replace = self_update.replace_file
        def interrupt_after_backup(source, target):
            real_replace(source, target)
            if target.name == 'previous.exe.bak':
                raise KeyboardInterrupt()
        with patch.object(self_update, 'wait_for_process'), patch.object(self_update, 'replace_file', side_effect=interrupt_after_backup), \
             self.assertRaises(KeyboardInterrupt):
            self_update.apply_update(job, job.parent/'helper.exe')
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual((job.parent/'previous.exe.bak').read_bytes(), self.old)


class WaitTests(unittest.TestCase):
    class Kernel:
        def __init__(self, result=0, handle=91):
            self.result = result; self.handle = handle; self.closed = []
        def OpenProcess(self, access, inherit, pid):
            assert (access, inherit, pid) == (0x00100000, False, 123)
            return self.handle
        def WaitForSingleObject(self, handle, timeout):
            assert (handle, timeout) == (91, 60000)
            return self.result
        def CloseHandle(self, handle):
            self.closed.append(handle)

    def test_wait_uses_only_synchronize_and_always_closes_handle(self):
        for result in [0, 0x102, 0xffffffff]:
            api = self.Kernel(result)
            with self.subTest(result=result):
                if result:
                    with self.assertRaises(updater.UpdateError):
                        self_update._wait_for_process(123, api, lambda: 5)
                else:
                    self_update._wait_for_process(123, api, lambda: 0)
                self.assertEqual(api.closed, [91])

    def test_missing_process_is_finished_but_access_denied_is_not(self):
        self_update._wait_for_process(123, self.Kernel(handle=0), lambda: 87)
        with self.assertRaises(updater.UpdateError):
            self_update._wait_for_process(123, self.Kernel(handle=0), lambda: 5)


class LauncherSelfUpdateTests(unittest.TestCase):
    def test_handoff_exits_before_mods_and_without_pause(self):
        with patch.object(launcher, 'maybe_self_update', return_value=True), patch.object(launcher, 'update_game') as mods, \
             patch('builtins.input', side_effect=AssertionError('old process must exit')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(['--game-dir', 'chosen']), 0)
        mods.assert_not_called()

    def test_one_hop_bypass_continues_mod_updates(self):
        with patch.object(launcher, 'maybe_self_update', return_value=False) as own, \
             patch.object(launcher, 'update_game', return_value=[]) as mods, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(['--game-dir', 'chosen', '--no-self-update', '--no-pause']), 0)
        self.assertTrue(own.call_args.kwargs['no_self_update'])
        mods.assert_called_once()

    def test_licenses_and_hidden_helper_do_not_check_network_or_update_mods(self):
        with patch.object(launcher, 'maybe_self_update') as own, patch.object(launcher, 'update_game') as mods, \
             patch.object(launcher, 'show_licenses'), patch.object(launcher, 'apply_job', return_value=0) as helper:
            self.assertEqual(launcher.main(['--licenses']), 0)
            self.assertEqual(launcher.main(['--apply-self-update', 'job.json']), 0)
        own.assert_not_called(); mods.assert_not_called(); helper.assert_called_once_with(Path('job.json'))


if __name__ == '__main__':
    unittest.main()
