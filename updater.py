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


def download_asset(client, release, spec):
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
    return data


def download_release(client, release, spec):
    return validate_archive(download_asset(client, release, spec), spec)


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


def discover_plugins(mods):
    """Find core files at any depth without following links or junctions."""
    require_plain(mods)
    if mods.exists() and not mods.is_dir():
        raise UpdateError(f'Mods path is not a directory: {mods}')
    plain_tree(mods)
    return [Path(directory)/name for directory, _, names in os.walk(mods, followlinks=False)
            for name in names if name.casefold().endswith('.nro')]


@dataclass(frozen=True)
class Installation:
    target: Path
    core: Path | None
    content: Path | None
    misplaced: bool


def top_folder(mods, path):
    return mods / path.relative_to(mods).parts[0]


def find_target(game, spec, plugins):
    mods = game / 'mods'
    found = [p for p in plugins if p.name.casefold() == spec.nro.casefold()]
    if len(found) > 1:
        raise UpdateError(f'Duplicate {spec.nro} installations; resolve these copies first: ' + ', '.join(map(str, found)))
    canonical = mods / spec.folder
    for child in mods.iterdir() if mods.exists() else []:
        if child.name.upper().casefold() == spec.folder.upper().casefold():
            canonical = child
            require_plain(child)
    if found:
        core = found[0]
        proper = tuple(p.casefold() for p in core.parts[-4:-1]) == ('romfs', 'nativenx', 'plugins')
        content = core.parents[3] if proper else core.parent
        outer = top_folder(mods, core)
        # Keep a custom folder name when it belongs to this mod alone. A shared
        # wrapper is split into canonical folders without promoting other mods.
        isolated = outer.is_dir() and sum(p.is_relative_to(outer) for p in plugins) == 1
        target = outer if content == outer or content != mods and isolated else canonical
        if target.exists() and not core.is_relative_to(target) and (not target.is_dir() or any(target.iterdir())):
            raise UpdateError(f'{target} exists without the expected mod; refusing to overwrite it')
        if content != mods and content != target and any(p != core and p.is_relative_to(content) for p in plugins):
            raise UpdateError(f'Multiple plugins share the same folder: {content}; separate them before updating')
        return Installation(target, core, content, core != target/spec.nro_path)
    require_plain(canonical)
    if canonical.exists():
        if not canonical.is_dir() or any(canonical.iterdir()):
            raise UpdateError(f'{canonical} exists without the expected mod; refusing to overwrite it')
    return Installation(canonical, None, None, False)


def promote_content(source, target, core, managed):
    """Move custom files into the usable layout; never guess between settings."""
    for directory, dirs, names in os.walk(source):
        relative = Path(directory).relative_to(source)
        destination = target/relative
        if destination.exists() and not destination.is_dir():
            raise UpdateError(f'Custom file conflicts with repaired directory: {destination}')
        destination.mkdir(parents=True, exist_ok=True)
        for name in names:
            original = Path(directory)/name
            if original == core:
                continue  # The verified release supplies the core in its proper path.
            relative_file = relative/name
            path = target/relative_file
            if path.exists():
                if relative_file.as_posix() in managed:
                    continue
                if not path.is_file() or path.read_bytes() != original.read_bytes():
                    raise UpdateError(f'Conflicting custom files while repairing: {relative_file}')
            else:
                shutil.copy2(original, path)


def stage_changes(mods, temporary, plans):
    """Stage whole top-level units, including shared wrappers, exactly once."""
    units = {}
    for _, installation, _, _ in plans:
        paths = [installation.target]
        if installation.misplaced:
            paths.append(top_folder(mods, installation.core))
        for path in paths:
            if path in units:
                continue
            stage = Path(temporary)/str(len(units))
            if path.is_dir():
                plain_tree(path)
                shutil.copytree(path, stage)
            elif path.is_file():
                shutil.copy2(path, stage)
            else:
                stage.mkdir()
            units[path] = stage
    # Remove misplaced originals in the staged copy before promoting anything;
    # a target folder can also contain another mod's old wrapper.
    for _, installation, _, _ in plans:
        if not installation.misplaced:
            continue
        content = installation.content
        remove = content if content != mods and content != installation.target else installation.core
        unit = top_folder(mods, remove)
        path = units[unit]/remove.relative_to(unit)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        parent = path.parent
        while parent != units[unit] and parent.is_relative_to(units[unit]) and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    for _, installation, files, _ in plans:
        stage = units[installation.target]
        if installation.misplaced and installation.content not in (mods, installation.target):
            promote_content(installation.content, stage, installation.core, files)
        for name, data in files.items():
            path = stage/name
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                raise UpdateError(f'Installed custom directory conflicts with release file: {name}')
            path.write_bytes(data)
    for stage in units.values():
        if stage.is_dir() and not any(stage.iterdir()):
            stage.rmdir()  # Retire an empty extraction wrapper into the backup.
    return units


def stage_load_order(mods, temporary, units, plans):
    """Keep disabled source wrappers off while making fresh installs usable."""
    order = mods/'load_order.txt'
    original = order.read_bytes() if order.is_file() else b''
    try:
        lines = original.decode('utf-8').splitlines()
    except UnicodeError as error:
        raise UpdateError('Cannot read load_order.txt as UTF-8; nothing installed') from error
    modern = bool(lines and lines[0].lstrip(' \t').startswith('# Written by the Mods tab'))
    listed = {line.strip() for line in lines if not line.lstrip().startswith('#')}
    disabled = {line.strip()[7:].casefold() for line in lines if line.strip().startswith('# off: ')}
    added = []
    for _, location, _, _ in plans:
        source = top_folder(mods, location.core) if location.core else None
        name = location.target.name
        if source == location.target or name in listed or name.casefold() in disabled:
            continue
        source_off = source is not None and source.is_dir() and (
            source.name.casefold() in disabled or
            (not modern and order.is_file() and source.name not in listed) or
            (not order.exists() and source.name.startswith('_')))
        if source_off:
            if modern or not order.exists():
                added.append('# off: '+name)
        elif order.exists() and not modern:
            added.append(name)
    if not added:
        return
    if not order.exists():
        # A new UI-style list changes unlisted '_' folders to enabled too, so
        # carry all their existing default-off choices into the new list.
        original = b'# Written by the Mods tab\n'
        added = ['# off: '+p.name for p in sorted(mods.iterdir())
                 if p.is_dir() and p.name.startswith('_')] + added
    ending = b'\r\n' if b'\r\n' in original else b'\n'
    prefix = original + (ending if original and not original.endswith((b'\n', b'\r')) else b'')
    stage = Path(temporary)/str(len(units))
    stage.write_bytes(prefix + ending.join(name.encode('utf-8') for name in added) + ending)
    units[order] = stage


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
        mods = game/'mods'
        plugins = discover_plugins(mods)
        results, plans, targets = [], [], {}
        for spec in MODS:
            log(f'Checking {spec.name}...')
            release = client.latest(spec)
            if release is None:
                log(f'{spec.name}: no stable release is available; skipped.')
                results.append(Result(spec.name, 'no-release'))
                continue
            installation = find_target(game, spec, plugins)
            target = installation.target
            existing = installation.core is not None
            key = str(target).casefold()
            if key in targets:
                raise UpdateError(f'{spec.name} and {targets[key]} share the same folder: {target}; separate them before updating')
            targets[key] = spec.name
            log(f'{spec.name}: downloading and verifying {release.version}...')
            files = download_release(client, release, spec)
            if existing:
                plain_tree(target)
            current = existing and not installation.misplaced and all(
                (target/name).is_file() and sha256((target/name).read_bytes()) == sha256(data)
                for name, data in files.items()
            )
            if current:
                log(f'{spec.name}: already up to date ({release.version}).')
                results.append(Result(spec.name, 'current', release.version, str(target)))
                continue
            result = Result(spec.name, 'repair-needed' if installation.misplaced else 'available', release.version, str(target))
            results.append(result)
            plans.append((spec, installation, files, result))
        if check or not plans:
            return results
        # Nothing in installed mod folders changes until every download, path,
        # NRO and checksum has passed validation.
        assert_game_closed(running)
        require_plain(game / 'backups')
        with tempfile.TemporaryDirectory(prefix='.mhgu-update-', dir=game) as temporary:
            staged = stage_changes(mods, temporary, plans)
            stage_load_order(mods, temporary, staged, plans)
            assert_game_closed(running)
            backup = game / 'backups' / ('mod-updater-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            backup.mkdir(parents=True)
            moved = []
            try:
                for target, stage in staged.items():
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
                    if stage.exists():
                        os.replace(stage, target)
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
            for spec, installation, _, result in plans:
                result.status = 'repaired' if installation.misplaced else 'updated' if installation.core else 'installed'
                log(f'{spec.name}: {result.status} to {result.version}.')
            log(f'Backup: {backup}')
        return results
