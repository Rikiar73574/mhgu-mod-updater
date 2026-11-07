"""Verified updates for three fixed MHGU Forge mods; Python standard library only."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from windows_ui import game_lock, game_running

OWNER = 'Rikiar73574'
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_MEMBER = 64 * 1024 * 1024
MAX_ENTRIES = 2048


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mod:
    name: str
    repo: str
    asset: str
    folder: str
    nro: str

    @property
    def nro_path(self):
        return 'romfs/nativeNX/plugins/' + self.nro

    @property
    def api_url(self):
        return f'https://api.github.com/repos/{OWNER}/{self.repo}/releases/latest'


MODS = (
    Mod('Armor Fusion', 'mhgu-armor-fusion', 'native-armor-fusion.zip', 'armor-fusion', 'armor_fusion.nro'),
    Mod('HuntLog', 'mhgu-huntlog', 'huntlog-mod.zip', 'HuntLog', 'hunter_hud.nro'),
    Mod("Hunter's Compendium", 'mhgu-compendium', 'hunters-compendium.zip', 'hunters-compendium', 'hunters_compendium.nro'),
)


@dataclass(frozen=True)
class Release:
    version: str
    asset: dict
    checksum: dict | None


@dataclass
class Result:
    name: str
    status: str
    version: str = ''
    folder: str = ''


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def asset_url(spec, tag, asset):
    url = asset.get('browser_download_url', '')
    expected = f'https://github.com/{OWNER}/{spec.repo}/releases/download/{urllib.parse.quote(tag, safe="")}/{asset["name"]}'
    if url != expected:
        raise UpdateError(f'{spec.name}: unexpected release download URL')
    return url


class SecureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != 'https' or parsed.hostname not in {
            'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'
        }:
            raise UpdateError('GitHub redirected outside its HTTPS release hosts')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHubClient:
    def __init__(self):
        self.opener = urllib.request.build_opener(SecureRedirect())

    def request(self, url, limit):
        request = urllib.request.Request(url, headers={
            'User-Agent': 'MHGU-Mod-Updater/1.0',
            'Accept': 'application/vnd.github+json' if '/api.github.com/' in url else 'application/octet-stream',
        })
        with self.opener.open(request, timeout=45) as response:
            if urllib.parse.urlsplit(response.url).scheme != 'https':
                raise UpdateError('An HTTPS download is required')
            declared = response.headers.get('Content-Length')
            if declared and int(declared) > limit:
                raise UpdateError('Download exceeds the permitted size')
            data = response.read(limit + 1)
        if len(data) > limit:
            raise UpdateError('Download exceeds the permitted size')
        return data

    def latest(self, spec):
        try:
            data = json.loads(self.request(spec.api_url, 2 * 1024 * 1024))
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 404:
                return None
            raise UpdateError(f'{spec.name}: GitHub HTTP {error.code}; try again later') from error
        except (ValueError, OSError) as error:
            raise UpdateError(f'{spec.name}: could not read GitHub release: {error}') from error
        if not isinstance(data, dict) or data.get('draft') or data.get('prerelease'):
            raise UpdateError(f'{spec.name}: GitHub did not return a stable public release')
        tag = data.get('tag_name')
        if not isinstance(tag, str) or not tag:
            raise UpdateError(f'{spec.name}: release has no version')
        assets = data.get('assets', [])
        if not isinstance(assets, list):
            raise UpdateError(f'{spec.name}: invalid release asset list')
        selected = [a for a in assets if isinstance(a, dict) and a.get('name') == spec.asset]
        sums = [a for a in assets if isinstance(a, dict) and a.get('name') == 'SHA256SUMS']
        if len(selected) != 1 or len(sums) > 1:
            raise UpdateError(f'{spec.name}: expected exactly one {spec.asset} release asset')
        asset_url(spec, tag, selected[0])
        if sums:
            asset_url(spec, tag, sums[0])
        return Release(tag, selected[0], sums[0] if sums else None)


def digest_of(asset):
    digest = asset.get('digest')
    if digest is None:
        return None
    if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
        raise UpdateError('Release does not provide a valid SHA-256 digest')
    return digest[7:].lower()


def parse_sums(data):
    try:
        lines = data.decode('utf-8').splitlines()
    except UnicodeError as error:
        raise UpdateError('Invalid SHA256SUMS encoding') from error
    result = {}
    for line in lines:
        if not line.strip():
            continue
        match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *](.+)', line)
        if not match or match[2] in result:
            raise UpdateError('Invalid or duplicate SHA256SUMS entry')
        result[match[2]] = match[1].lower()
    return result


def download_release(client, release, spec):
    expected = digest_of(release.asset)
    if release.checksum:
        sums = client.request(release.checksum['browser_download_url'], 1024 * 1024)
        sums_digest = digest_of(release.checksum)
        if sums_digest and sha256(sums) != sums_digest:
            raise UpdateError(f'{spec.name}: SHA-256 mismatch for SHA256SUMS')
        listed = parse_sums(sums).get(spec.asset)
        if not listed or expected and listed != expected:
            raise UpdateError(f'{spec.name}: missing or conflicting release checksum')
        expected = listed
    if not expected:
        raise UpdateError(f'{spec.name}: no trusted SHA-256 checksum; refusing download')
    size = release.asset.get('size')
    if not isinstance(size, int) or not 0 < size <= MAX_DOWNLOAD:
        raise UpdateError(f'{spec.name}: invalid release asset size')
    data = client.request(release.asset['browser_download_url'], MAX_DOWNLOAD)
    if sha256(data) != expected:
        raise UpdateError(f'{spec.name}: SHA-256 checksum mismatch; nothing installed')
    if len(data) != size:
        raise UpdateError(f'{spec.name}: release asset length mismatch')
    return validate_archive(data, spec)


def safe_zip_name(name):
    if not name or '\\' in name or '\x00' in name or name.startswith('/'):
        raise UpdateError(f'Unsafe ZIP path: {name!r}')
    trimmed = name[:-1] if name.endswith('/') else name
    parts = trimmed.split('/')
    if (len(trimmed) > 240 or any(not p or p in {'.', '..'} or p[-1] in ' .'
            or any(ord(c) < 32 or c in '<>:"|?*' for c in p)
            or re.fullmatch(r'(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])', p.split('.', 1)[0].rstrip(' '), re.I)
            for p in parts)):
        raise UpdateError(f'Unsafe ZIP path: {name!r}')
    return '/'.join(parts)


def validate_archive(data, spec):
    files, seen, kinds = {}, set(), {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES or sum(i.file_size for i in entries) > MAX_EXPANDED:
                raise UpdateError('ZIP exceeds file count or expanded size limits')
            for info in entries:
                name = safe_zip_name(info.orig_filename)
                if info.orig_filename != info.filename:
                    raise UpdateError('ZIP contains an embedded NUL filename')
                # Uppercase first also conservatively folds Windows device/
                # filename aliases such as ASCII I and the dotless i.
                key = unicodedata.normalize('NFC', name).upper().casefold()
                if key in seen:
                    raise UpdateError(f'ZIP contains duplicate/case-colliding paths: {name}')
                seen.add(key)
                mode = (info.external_attr >> 16) & 0xffff
                kind = stat.S_IFMT(mode)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or info.external_attr & 0x400:
                    raise UpdateError(f'ZIP contains a link or special file: {name}')
                if info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise UpdateError('Encrypted or unsupported ZIP member')
                if info.file_size > MAX_MEMBER or info.file_size > max(1, info.compress_size) * 500:
                    raise UpdateError('ZIP member exceeds size or compression ratio limits')
                for parent in PurePosixPath(key).parents:
                    parent = str(parent)
                    if parent == '.':
                        continue
                    if kinds.get(parent) == 'file':
                        raise UpdateError('ZIP file/directory path collision')
                    kinds[parent] = 'dir'
                file_kind = 'dir' if info.is_dir() else 'file'
                if key in kinds and kinds[key] != file_kind:
                    raise UpdateError('ZIP file/directory path collision')
                kinds[key] = file_kind
                if not info.is_dir():
                    files[name] = archive.read(info)
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError) as error:
        if isinstance(error, UpdateError):
            raise
        raise UpdateError(f'Invalid ZIP: {error}') from error
    nros = [name for name in files if name.lower().endswith('.nro')]
    if nros != [spec.nro_path]:
        raise UpdateError(f'{spec.name}: ZIP must contain only {spec.nro_path}')
    image = files[spec.nro_path]
    if len(image) < 128 or image[16:20] != b'NRO0' or struct.unpack_from('<I', image, 24)[0] != len(image):
        raise UpdateError(f'{spec.name}: invalid NRO image')
    if 'SHA256SUMS' not in files:
        raise UpdateError(f'{spec.name}: ZIP is missing SHA256SUMS')
    sums = parse_sums(files['SHA256SUMS'])
    if set(sums) != set(files) - {'SHA256SUMS'}:
        raise UpdateError('ZIP checksum list does not match its files')
    if any(sha256(files[name]) != digest for name, digest in sums.items()):
        raise UpdateError('ZIP internal SHA-256 checksum mismatch')
    return files


def is_link(path):
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, 'st_file_attributes', 0) & 0x400)


def require_plain(path):
    if path.exists() or path.is_symlink():
        if is_link(path):
            raise UpdateError(f'Links/junctions are not supported here: {path}')


def plain_tree(root):
    require_plain(root)
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(directory) / name
            require_plain(path)
            mode = path.stat().st_mode
            if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
                raise UpdateError(f'Special file in installed mod: {path}')


def find_target(game, spec):
    mods = game / 'mods'
    require_plain(mods)
    if mods.exists() and not mods.is_dir():
        raise UpdateError(f'Mods path is not a directory: {mods}')
    found = []
    canonical = mods / spec.folder
    for child in mods.iterdir() if mods.exists() else []:
        if child.name.casefold() == spec.folder.casefold():
            canonical = child
            require_plain(child)
        candidate = child / spec.nro_path
        if candidate.is_file():
            require_plain(child)
            for parent in candidate.parents:
                if parent == mods:
                    break
                require_plain(parent)
            require_plain(candidate)
            found.append(child)
    if len(found) > 1:
        raise UpdateError(f'Duplicate {spec.nro} installations; resolve these folders first: ' + ', '.join(map(str, found)))
    if found:
        return found[0], True
    require_plain(canonical)
    if canonical.exists():
        if not canonical.is_dir() or any(canonical.iterdir()):
            raise UpdateError(f'{canonical} exists without the expected mod; refusing to overwrite it')
    return canonical, False


def assert_game_closed(running):
    if running():
        raise UpdateError('MHGURecomp is running. Close the game, then run the updater again.')


def update_game(game_dir, *, check=False, client=None, running=None, log=print):
    game = Path(game_dir).resolve()
    if not game.is_dir() or not any(p.is_file() and p.name.casefold() == 'mhgurecomp.exe' for p in game.iterdir()):
        raise UpdateError('Choose the folder containing mhgurecomp.exe')
    running = running or game_running
    client = client or GitHubClient()
    with game_lock(game):
        assert_game_closed(running)
        results, plans, targets = [], [], {}
        for spec in MODS:
            log(f'Checking {spec.name}...')
            release = client.latest(spec)
            if release is None:
                log(f'{spec.name}: no stable release is available; skipped.')
                results.append(Result(spec.name, 'no-release'))
                continue
            target, existing = find_target(game, spec)
            key = str(target).casefold()
            if key in targets:
                raise UpdateError(f'{spec.name} and {targets[key]} share the same folder: {target}; separate them before updating')
            targets[key] = spec.name
            log(f'{spec.name}: downloading and verifying {release.version}...')
            files = download_release(client, release, spec)
            if existing:
                plain_tree(target)
            current = existing and all(
                (target/name).is_file() and sha256((target/name).read_bytes()) == sha256(data)
                for name, data in files.items()
            )
            if current:
                log(f'{spec.name}: already up to date ({release.version}).')
                results.append(Result(spec.name, 'current', release.version, str(target)))
                continue
            result = Result(spec.name, 'available', release.version, str(target))
            results.append(result)
            plans.append((spec, target, existing, files, result))
        if check or not plans:
            return results
        # Nothing in installed mod folders changes until every download, path,
        # NRO and checksum has passed validation.
        assert_game_closed(running)
        require_plain(game / 'backups')
        with tempfile.TemporaryDirectory(prefix='.mhgu-update-', dir=game) as temporary:
            staged = []
            for index, (spec, target, existing, files, result) in enumerate(plans):
                stage = Path(temporary) / str(index)
                if existing:
                    plain_tree(target)
                    shutil.copytree(target, stage)
                else:
                    stage.mkdir()
                for name, data in files.items():
                    path = stage / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path.is_dir():
                        raise UpdateError(f'Installed custom directory conflicts with release file: {name}')
                    path.write_bytes(data)
                staged.append((spec, target, existing, stage, result))
            assert_game_closed(running)
            backup = game / 'backups' / ('mod-updater-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            backup.mkdir(parents=True)
            moved = []
            try:
                for spec, target, existing, stage, result in staged:
                    assert_game_closed(running)
                    require_plain(target)
                    require_plain(target.parent)
                    target.parent.mkdir(exist_ok=True)
                    saved = backup / target.name
                    had_folder = target.exists()
                    # Record intent BEFORE either rename. Ctrl+C can arrive
                    # after an OS call completed but before Python continues.
                    moved.append((target, saved, had_folder, stage))
                    if had_folder:
                        os.replace(target, saved)
                    os.replace(stage, target)
                    result.status = 'updated' if existing else 'installed'
                    log(f'{spec.name}: {result.status} to {result.version}.')
                    if not existing:
                        log(f'{spec.name}: enable {target.name} in the game\'s mod settings. Existing load order was preserved.')
            except BaseException as error:
                failures = []
                for target, saved, had_folder, stage in reversed(moved):
                    try:
                        # A missing saved folder means the first rename never
                        # completed: leave the original target exactly alone.
                        if had_folder and not saved.exists():
                            continue
                        if target.exists():
                            # Its original indexed stage is vacant after the
                            # swap; no user-chosen folder name can collide.
                            os.replace(target, stage)
                        if had_folder:
                            os.replace(saved, target)
                    except OSError as restore_error:
                        failures.append(str(restore_error))
                detail = '; rollback needs attention: ' + '; '.join(failures) if failures else '; previous mods restored'
                reason = 'cancelled' if isinstance(error, KeyboardInterrupt) else str(error)
                raise UpdateError(f'Update failed: {reason}{detail}. Backup: {backup}') from error
            log(f'Backup: {backup}')
        return results
