import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
import urllib.error
import zipfile
from unittest.mock import patch

import updater


def nro(marker=b'new'):
    data = bytearray(4096)
    data[16:20] = b'NRO0'
    struct.pack_into('<I', data, 24, len(data))
    data[128:128+len(marker)] = marker
    return bytes(data)


def archive(files, extra=()):
    files = dict(files)
    if 'SHA256SUMS' not in files:
        files['SHA256SUMS'] = ''.join(
            f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in files.items()
        ).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
        for info, data in extra:
            z.writestr(info, data)
    return stream.getvalue()


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.game = Path(self.temporary.name) / 'game'
        self.game.mkdir()
        (self.game / 'mhgurecomp.exe').write_bytes(b'synthetic executable; never run')
        self.spec = updater.MODS[0]
        self.payloads = {}
        self.requests = []
        outer = self

        class Client(updater.GitHubClient):
            def request(self, url, limit):
                outer.requests.append(url)
                if url not in outer.payloads:
                    raise urllib.error.HTTPError(url, 404, 'not found', {}, None)
                data = outer.payloads[url]
                if isinstance(data, Exception):
                    raise data
                if len(data) > limit:
                    raise updater.UpdateError('download too large')
                return data

        self.client = Client()

    def release(self, spec=None, files=None, data=None, digest=True):
        spec = spec or self.spec
        files = files or {spec.nro_path: nro(), 'README.md': b'release', 'font/custom.otf': b'new font'}
        data = data if data is not None else archive(files)
        url = f'https://github.com/Rikiar73574/{spec.repo}/releases/download/v1/{spec.asset}'
        sums_url = f'https://github.com/Rikiar73574/{spec.repo}/releases/download/v1/SHA256SUMS'
        sums = f'{hashlib.sha256(data).hexdigest()}  {spec.asset}\n'.encode()
        metadata = {'tag_name': 'v1', 'draft': False, 'prerelease': False, 'assets': [
            {'name': spec.asset, 'browser_download_url': url, 'size': len(data), 'digest': 'sha256:'+hashlib.sha256(data).hexdigest() if digest else None},
            {'name': 'SHA256SUMS', 'browser_download_url': sums_url, 'size': len(sums), 'digest': 'sha256:'+hashlib.sha256(sums).hexdigest()},
        ]}
        self.payloads[spec.api_url] = json.dumps(metadata).encode()
        self.payloads[url] = data
        self.payloads[sums_url] = sums
        return url, metadata

    def install_old(self, spec=None, folder=None):
        spec = spec or self.spec
        root = self.game / 'mods' / (folder or spec.folder)
        p = root / spec.nro_path
        p.parent.mkdir(parents=True)
        p.write_bytes(nro(b'old'))
        (root/'my-settings.ini').write_text('personal settings')
        (root/'font').mkdir()
        (root/'font/custom.otf').write_bytes(b'old bundled font')
        (root/'font/mine.otf').write_bytes(b'user font')
        return root

    def run_update(self, **kwargs):
        return updater.update_game(self.game, client=self.client, running=lambda:False, log=lambda _:None, **kwargs)

    def test_no_stable_releases_is_polite_and_changes_nothing(self):
        result = self.run_update()
        self.assertEqual([r.status for r in result], ['no-release']*3)
        self.assertEqual([p.name for p in self.game.iterdir()], ['mhgurecomp.exe'])

    def test_check_downloads_and_validates_without_installing(self):
        self.release()
        result = self.run_update(check=True)
        self.assertEqual(result[0].status, 'available')
        self.assertFalse((self.game/'mods').exists())
        self.assertFalse((self.game/'backups').exists())

    def test_update_custom_named_folder_preserves_settings_fonts_other_mods_and_save(self):
        old = self.install_old(folder='my-armor')
        self.release()
        (self.game/'mods/load_order.txt').write_bytes(b'unchanged load order')
        untouched = self.game/'mods/other/config.ini'
        untouched.parent.mkdir()
        untouched.write_bytes(b'other mod')
        (self.game/'save').mkdir()
        (self.game/'save/system').write_bytes(b'precious save')
        result = self.run_update()
        self.assertEqual(result[0].status, 'updated')
        self.assertEqual((old/self.spec.nro_path).read_bytes(), nro())
        self.assertEqual((old/'my-settings.ini').read_text(), 'personal settings')
        self.assertEqual((old/'font/mine.otf').read_bytes(), b'user font')
        self.assertEqual((old/'font/custom.otf').read_bytes(), b'new font')
        self.assertEqual(untouched.read_bytes(), b'other mod')
        self.assertEqual((self.game/'save/system').read_bytes(), b'precious save')
        self.assertEqual((self.game/'mods/load_order.txt').read_bytes(), b'unchanged load order')
        backups=list((self.game/'backups').glob('mod-updater-*/my-armor'))
        self.assertEqual(len(backups),1)
        self.assertEqual((backups[0]/self.spec.nro_path).read_bytes(),nro(b'old'))

    def test_expected_nro_hash_current_skips_backup_and_swap(self):
        old=self.install_old()
        self.release()
        for name,data in updater.validate_archive(self.payloads[next(u for u in self.payloads if u.endswith('.zip'))],self.spec).items():
            path=old/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        self.assertEqual(self.run_update()[0].status,'current')
        self.assertFalse((self.game/'backups').exists())

    def test_new_install_uses_canonical_folder(self):
        self.release()
        messages=[]
        result=updater.update_game(self.game,client=self.client,running=lambda:False,log=messages.append)
        self.assertEqual(result[0].status,'installed')
        self.assertTrue((self.game/'mods'/self.spec.folder/self.spec.nro_path).is_file())

    def test_duplicate_expected_nros_refused_without_removing_either(self):
        a=self.install_old(folder='one');b=self.install_old(folder='two');self.release()
        with self.assertRaisesRegex(updater.UpdateError,'[Dd]uplicate'):
            self.run_update()
        self.assertEqual((a/self.spec.nro_path).read_bytes(),nro(b'old'))
        self.assertEqual((b/self.spec.nro_path).read_bytes(),nro(b'old'))

    def test_canonical_folder_without_matching_mod_is_not_overwritten(self):
        p=self.game/'mods'/self.spec.folder;p.mkdir(parents=True);(p/'unrelated').write_text('keep')
        self.release()
        with self.assertRaises(updater.UpdateError):self.run_update()
        self.assertEqual((p/'unrelated').read_text(),'keep')

    def test_running_game_blocks_before_network_and_before_install(self):
        self.release()
        with self.assertRaisesRegex(updater.UpdateError,'running'):
            updater.update_game(self.game,client=self.client,running=lambda:True,log=lambda _:None)
        self.assertEqual(self.requests,[])
        states=iter([False,True])
        with self.assertRaisesRegex(updater.UpdateError,'running'):
            updater.update_game(self.game,client=self.client,running=lambda:next(states),log=lambda _:None)
        self.assertFalse((self.game/'mods').exists())

    def test_all_downloads_validate_before_any_old_folder_moves(self):
        old=self.install_old();self.release();url,_=self.release(updater.MODS[2]);self.payloads[url]=b'corrupt'
        with self.assertRaises(updater.UpdateError):self.run_update()
        self.assertEqual((old/self.spec.nro_path).read_bytes(),nro(b'old'))
        self.assertFalse((self.game/'backups').exists())

    def test_bad_outer_digest_and_checksum_disagreement_fail_closed(self):
        old=self.install_old();url,metadata=self.release();self.payloads[url]+=b'changed'
        with self.assertRaisesRegex(updater.UpdateError,'[Cc]hecksum|SHA-256'):self.run_update()
        self.assertEqual((old/self.spec.nro_path).read_bytes(),nro(b'old'))

    def test_external_checksum_is_required_when_github_digest_absent(self):
        self.release(digest=False)
        self.assertEqual(self.run_update(check=True)[0].status,'available')
        metadata=json.loads(self.payloads[self.spec.api_url]);metadata['assets']=metadata['assets'][:1]
        self.payloads[self.spec.api_url]=json.dumps(metadata).encode()
        with self.assertRaisesRegex(updater.UpdateError,'checksum|SHA-256'):self.run_update(check=True)

    def test_prerelease_draft_wrong_asset_and_foreign_url_rejected(self):
        for change in ['prerelease','draft','asset','url']:
            with self.subTest(change=change):
                _,m=self.release()
                if change in ['prerelease','draft']:m[change]=True
                elif change=='asset':m['assets'][0]['name']='source.zip'
                else:m['assets'][0]['browser_download_url']='https://evil.invalid/file.zip'
                self.payloads[self.spec.api_url]=json.dumps(m).encode()
                with self.assertRaises(updater.UpdateError):self.run_update(check=True)

    def test_zip_paths_symlinks_case_collisions_and_bombs_rejected(self):
        bad_names=['../outside','/outside','C:/outside','file:stream','romfs\\evil','CON.txt','CON .txt','NUL .txt','CONIN$','CONOUT$.txt','COM¹.txt','LPT².log','romfs/../evil','trailing.','dir/file ']
        for name in bad_names:
            with self.subTest(name=name),self.assertRaises(updater.UpdateError):
                updater.validate_archive(archive({self.spec.nro_path:nro(),name:b'x'}),self.spec)
        link=zipfile.ZipInfo('link');link.create_system=3;link.external_attr=(stat.S_IFLNK|0o777)<<16
        for files,extra in [({self.spec.nro_path:nro(),'a':b'x','A':b'y'},()),({self.spec.nro_path:nro()},[(link,b'outside')]),({self.spec.nro_path:nro(),'bomb':b'0'*2_000_000},())]:
            with self.assertRaises(updater.UpdateError):updater.validate_archive(archive(files,extra),self.spec)

    def test_zip_exact_expected_nro_and_internal_manifest_required(self):
        for files in [{'other.nro':nro()},{self.spec.nro_path:b'not an nro'},{self.spec.nro_path:nro(),'other.nro':nro()},{self.spec.nro_path:nro(),'SHA256SUMS':b'0'*64+b'  missing\n'}]:
            with self.subTest(files=list(files)),self.assertRaises(updater.UpdateError):updater.validate_archive(archive(files),self.spec)

    @unittest.skipUnless(hasattr(os,'symlink'),'symlinks unavailable')
    def test_symlink_target_or_custom_file_refused(self):
        outside=Path(self.temporary.name)/'outside';outside.mkdir()
        mods=self.game/'mods';mods.mkdir();(mods/self.spec.folder).symlink_to(outside,target_is_directory=True)
        self.release()
        with self.assertRaises(updater.UpdateError):self.run_update()
        self.assertEqual(list(outside.iterdir()),[])
        (mods/self.spec.folder).unlink();old=self.install_old();(old/'user-link').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(updater.UpdateError):self.run_update()

    def test_swap_error_restores_old_folder_exactly(self):
        old=self.install_old();self.release();real=updater.os.replace
        def fail(source,target):
            if Path(target)==old and '.mhgu-update-' in str(source):raise OSError('simulated swap failure')
            return real(source,target)
        with patch.object(updater.os,'replace',side_effect=fail),self.assertRaises(updater.UpdateError):self.run_update()
        self.assertEqual((old/self.spec.nro_path).read_bytes(),nro(b'old'))
        self.assertEqual((old/'my-settings.ini').read_text(),'personal settings')

    def test_later_mod_failure_rolls_back_preceding_updates(self):
        first=self.install_old();third=self.install_old(updater.MODS[2]);self.release();self.release(updater.MODS[2]);real=updater.os.replace
        def fail(source,target):
            if Path(target)==third and '.mhgu-update-' in str(source):raise OSError('simulated later swap failure')
            return real(source,target)
        with patch.object(updater.os,'replace',side_effect=fail),self.assertRaises(updater.UpdateError):self.run_update()
        self.assertEqual((first/self.spec.nro_path).read_bytes(),nro(b'old'))
        self.assertEqual((third/updater.MODS[2].nro_path).read_bytes(),nro(b'old'))

    def test_two_managed_mods_in_one_folder_are_refused(self):
        folder=self.install_old(folder='merged')
        (folder/updater.MODS[2].nro_path).write_bytes(nro(b'old'))
        self.release();self.release(updater.MODS[2])
        with self.assertRaisesRegex(updater.UpdateError,'same folder|shared folder'):self.run_update()
        self.assertFalse((self.game/'backups').exists())

    def test_ctrl_c_between_directory_renames_restores_original(self):
        old=self.install_old();self.release();real=updater.os.replace
        def interrupt(source,target):
            if Path(target)==old and '.mhgu-update-' in str(source):raise KeyboardInterrupt()
            return real(source,target)
        with patch.object(updater.os,'replace',side_effect=interrupt):
            try:self.run_update()
            except BaseException:pass
        self.assertTrue(old.exists(),'Ctrl+C must restore the installed folder')
        self.assertEqual((old/self.spec.nro_path).read_bytes(),nro(b'old'))

    def test_ctrl_c_after_successful_old_rename_restores_original(self):
        old=self.install_old();self.release();real=updater.os.replace
        def interrupt(source,target):
            result=real(source,target)
            if Path(source)==old:raise KeyboardInterrupt()
            return result
        with patch.object(updater.os,'replace',side_effect=interrupt):
            try:self.run_update()
            except BaseException:pass
        self.assertTrue(old.exists(),'completed old rename must be recorded before it can be interrupted')
        self.assertEqual((old/self.spec.nro_path).read_bytes(),nro(b'old'))

    def test_rollback_displaced_new_folders_cannot_collide_with_custom_mod_names(self):
        roots=[]
        for spec,name in zip(updater.MODS,['same.failed-new','same','last']):
            roots.append(self.install_old(spec,folder=name));self.release(spec)
        real=updater.os.replace
        def fail(source,target):
            if Path(target)==roots[2] and '.mhgu-update-' in str(source):raise OSError('last swap failed')
            return real(source,target)
        with patch.object(updater.os,'replace',side_effect=fail),self.assertRaises(updater.UpdateError):self.run_update()
        for spec,root in zip(updater.MODS,roots):self.assertEqual((root/spec.nro_path).read_bytes(),nro(b'old'))

    def test_matching_nro_missing_bundled_font_is_repaired(self):
        root=self.install_old();(root/self.spec.nro_path).write_bytes(nro());(root/'font/custom.otf').unlink();self.release()
        self.assertEqual(self.run_update()[0].status,'updated')
        self.assertEqual((root/'font/custom.otf').read_bytes(),b'new font')

    def test_windows_uppercase_alias_collision_is_rejected(self):
        with self.assertRaises(updater.UpdateError):
            updater.validate_archive(archive({self.spec.nro_path:nro(),'LICENSE':b'one','LıCENSE':b'two'}),self.spec)

    def test_double_wrapped_mod_is_promoted_and_backed_up(self):
        inner=self.install_old(folder='Custom Armor/package/release');self.release()
        outer=self.game/'mods/Custom Armor';(outer/'outer-settings.ini').write_bytes(b'outer settings')
        result=self.run_update()
        self.assertEqual(result[0].status,'repaired')
        self.assertEqual((outer/self.spec.nro_path).read_bytes(),nro())
        self.assertEqual((outer/'my-settings.ini').read_text(),'personal settings')
        self.assertEqual((outer/'font/mine.otf').read_bytes(),b'user font')
        self.assertEqual((outer/'outer-settings.ini').read_bytes(),b'outer settings')
        self.assertEqual(list(outer.rglob(self.spec.nro)),[outer/self.spec.nro_path])
        backup=next((self.game/'backups').glob('mod-updater-*/Custom Armor'))
        self.assertEqual((backup/'package/release'/self.spec.nro_path).read_bytes(),nro(b'old'))

    def test_current_payload_in_wrong_canonical_wrapper_still_repairs(self):
        inner=self.install_old(folder=self.spec.folder+'/nested');self.release()
        files=updater.validate_archive(next(data for url,data in self.payloads.items() if url.endswith('.zip')),self.spec)
        for name,data in files.items():
            path=inner/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        before={str(p.relative_to(self.game)):p.read_bytes() for p in self.game.rglob('*') if p.is_file()}
        self.assertEqual(self.run_update(check=True)[0].status,'repair-needed')
        self.assertEqual(before,{str(p.relative_to(self.game)):p.read_bytes() for p in self.game.rglob('*') if p.is_file()})
        self.assertEqual(self.run_update()[0].status,'repaired')
        self.assertFalse(inner.exists())
        before={str(p.relative_to(self.game)):p.read_bytes() for p in self.game.rglob('*') if p.is_file()}
        self.assertEqual(self.run_update()[0].status,'current')
        self.assertEqual(before,{str(p.relative_to(self.game)):p.read_bytes() for p in self.game.rglob('*') if p.is_file()})

    def test_bare_core_in_custom_folder_gains_correct_layout(self):
        outer=self.game/'mods/Custom Armor';outer.mkdir(parents=True)
        (outer/self.spec.nro).write_bytes(nro(b'old'));(outer/'personal.ini').write_bytes(b'keep')
        self.release();self.assertEqual(self.run_update()[0].status,'repaired')
        self.assertEqual((outer/self.spec.nro_path).read_bytes(),nro())
        self.assertEqual((outer/'personal.ini').read_bytes(),b'keep')
        self.assertFalse((outer/self.spec.nro).exists())

    def test_loose_core_under_mods_is_backed_up_without_moving_mods_root(self):
        mods=self.game/'mods';mods.mkdir();loose=mods/self.spec.nro;loose.write_bytes(nro(b'old'))
        (mods/'load_order.txt').write_bytes(b'keep');self.release()
        self.assertEqual(self.run_update()[0].status,'repaired')
        self.assertFalse(loose.exists());self.assertEqual((mods/'load_order.txt').read_bytes(),b'keep\n'+self.spec.folder.encode()+b'\n')
        self.assertEqual((mods/self.spec.folder/self.spec.nro_path).read_bytes(),nro())
        backup=next((self.game/'backups').glob('mod-updater-*'))
        self.assertEqual((backup/self.spec.nro).read_bytes(),nro(b'old'))
        self.assertEqual((backup/'load_order.txt').read_bytes(),b'keep')

    def test_shared_wrapper_splits_managed_mods_and_preserves_other_mod(self):
        for spec in updater.MODS:self.install_old(spec,folder='Bundle/'+spec.folder);self.release(spec)
        other=self.game/'mods/Bundle/Other/romfs/nativeNX/plugins/other.nro'
        other.parent.mkdir(parents=True);other.write_bytes(nro(b'other'))
        self.assertEqual([r.status for r in self.run_update()],['repaired']*3)
        self.assertEqual(other.read_bytes(),nro(b'other'))
        for spec in updater.MODS:
            self.assertEqual((self.game/'mods'/spec.folder/spec.nro_path).read_bytes(),nro())
            self.assertFalse((self.game/'mods/Bundle'/spec.folder).exists())
            self.assertEqual((self.game/'mods'/spec.folder/'my-settings.ini').read_text(),'personal settings')

    def test_romfs_directly_under_mods_is_split_without_touching_unmanaged_files(self):
        for spec in updater.MODS:
            p=self.game/'mods'/spec.nro_path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(nro(b'old'))
            self.release(spec)
        other=self.game/'mods/romfs/other.bin';other.write_bytes(b'keep')
        self.assertEqual([r.status for r in self.run_update()],['repaired']*3)
        self.assertEqual(other.read_bytes(),b'keep')
        for spec in updater.MODS:
            self.assertFalse((self.game/'mods'/spec.nro_path).exists())
            self.assertEqual((self.game/'mods'/spec.folder/spec.nro_path).read_bytes(),nro())

    def test_recursive_duplicates_refuse_without_changes(self):
        a=self.install_old(folder='Custom');b=self.install_old(folder='Other/deep/wrapper');self.release()
        with self.assertRaisesRegex(updater.UpdateError,'[Dd]uplicate'):self.run_update()
        self.assertEqual((a/self.spec.nro_path).read_bytes(),nro(b'old'))
        self.assertEqual((b/self.spec.nro_path).read_bytes(),nro(b'old'))
        self.assertFalse((self.game/'backups').exists())

    def test_repair_rollback_restores_original_nested_layout(self):
        for spec in updater.MODS:self.install_old(spec,folder='Bundle/'+spec.folder);self.release(spec)
        before={str(p.relative_to(self.game)):p.read_bytes() for p in self.game.rglob('*') if p.is_file()}
        real=updater.os.replace;last=self.game/'mods'/updater.MODS[-1].folder
        def fail(source,target):
            if Path(target)==last and '.mhgu-update-' in str(source):raise OSError('last repair swap failed')
            return real(source,target)
        with patch.object(updater.os,'replace',side_effect=fail),self.assertRaises(updater.UpdateError):self.run_update()
        after={str(p.relative_to(self.game)):p.read_bytes() for p in self.game.rglob('*') if p.is_file() and 'backups' not in p.relative_to(self.game).parts}
        self.assertEqual(after,before)

    def test_correct_custom_folder_keeps_its_additional_plugin(self):
        root=self.install_old(folder='Custom Armor');self.release()
        helper=root/'romfs/nativeNX/plugins/helper.nro';helper.write_bytes(nro(b'helper'))
        self.assertEqual(self.run_update()[0].status,'updated')
        self.assertEqual(helper.read_bytes(),nro(b'helper'))

    def test_conflicting_custom_settings_stop_repair_before_changes(self):
        inner=self.install_old(folder='Custom Armor/package');outer=inner.parent
        (outer/'my-settings.ini').write_bytes(b'different outer preferences');self.release()
        with self.assertRaisesRegex(updater.UpdateError,'[Cc]onflicting custom'):
            self.run_update()
        self.assertEqual((inner/self.spec.nro_path).read_bytes(),nro(b'old'))
        self.assertEqual((outer/'my-settings.ini').read_bytes(),b'different outer preferences')
        self.assertFalse((self.game/'backups').exists())

    def test_all_three_missing_mods_are_installed_in_a_fresh_game(self):
        for spec in updater.MODS:self.release(spec)
        self.assertFalse((self.game/'mods').exists())
        self.assertEqual([r.status for r in self.run_update()],['installed']*3)
        for spec in updater.MODS:
            self.assertEqual((self.game/'mods'/spec.folder/spec.nro_path).read_bytes(),nro())

    def test_new_install_is_added_to_manual_load_order_without_reordering(self):
        mods=self.game/'mods';mods.mkdir();order=mods/'load_order.txt'
        original=b'Other mod\r\n# my ordering\r\n';order.write_bytes(original);self.release()
        self.run_update(check=True);self.assertEqual(order.read_bytes(),original)
        self.run_update()
        self.assertEqual(order.read_bytes(),original+self.spec.folder.encode()+b'\r\n')
        backup=next((self.game/'backups').glob('mod-updater-*/load_order.txt'))
        self.assertEqual(backup.read_bytes(),original)

    def test_new_install_keeps_ui_generated_load_order_unchanged(self):
        mods=self.game/'mods';mods.mkdir();order=mods/'load_order.txt'
        original=b'# Written by the Mods tab of the settings menu\nOther mod\n';order.write_bytes(original)
        self.release();self.run_update();self.assertEqual(order.read_bytes(),original)

    def test_install_preserves_explicitly_disabled_mod_preference(self):
        mods=self.game/'mods';mods.mkdir();order=mods/'load_order.txt'
        original=b'# off: armor-fusion\nOther mod\n';order.write_bytes(original)
        self.release();self.run_update();self.assertEqual(order.read_bytes(),original)

    def test_load_order_swap_failure_restores_both_order_and_mods(self):
        mods=self.game/'mods';mods.mkdir();order=mods/'load_order.txt';order.write_bytes(b'Other\n')
        self.release();real=updater.os.replace
        def fail(source,target):
            if Path(target)==order and '.mhgu-update-' in str(source):raise OSError('order write failed')
            return real(source,target)
        with patch.object(updater.os,'replace',side_effect=fail),self.assertRaises(updater.UpdateError):self.run_update()
        self.assertEqual(order.read_bytes(),b'Other\n')
        self.assertFalse((mods/self.spec.folder).exists())

    def test_disabled_ui_wrapper_stays_disabled_when_split(self):
        for spec in updater.MODS:self.install_old(spec,folder='Bundle/'+spec.folder);self.release(spec)
        order=self.game/'mods/load_order.txt';original=b'# Written by the Mods tab\n# off: Bundle\n'
        order.write_bytes(original);self.run_update()
        self.assertTrue(order.read_bytes().startswith(original))
        for spec in updater.MODS:self.assertIn('# off: '+spec.folder,order.read_text().splitlines())

    def test_unlisted_manual_wrapper_does_not_enable_relocated_targets(self):
        for spec in updater.MODS:self.install_old(spec,folder='Bundle/'+spec.folder);self.release(spec)
        order=self.game/'mods/load_order.txt';order.write_bytes(b'Other\n')
        self.run_update();self.assertEqual(order.read_bytes(),b'Other\n')

    def test_underscore_wrapper_keeps_off_state_without_previous_order_file(self):
        for spec in updater.MODS:self.install_old(spec,folder='_Bundle/'+spec.folder);self.release(spec)
        other=self.game/'mods/_Other/personal.ini';other.parent.mkdir();other.write_bytes(b'keep')
        self.run_update();order=self.game/'mods/load_order.txt'
        self.assertTrue(order.read_text().startswith('# Written by the Mods tab'))
        for name in [spec.folder for spec in updater.MODS]+['_Other']:
            self.assertIn('# off: '+name,order.read_text().splitlines())
        self.assertEqual(other.read_bytes(),b'keep')


if __name__=='__main__':unittest.main()
