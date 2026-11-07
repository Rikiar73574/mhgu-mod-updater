"""Console entry point for the portable Windows updater."""
import argparse
from pathlib import Path
import sys

from updater import update_game
from windows_ui import choose_game_dir


def default_game_dir():
    folder = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
    return folder if (folder/'mhgurecomp.exe').is_file() else None


def show_licenses():
    folder = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'licenses'
    print(f'Bundled license notices: {folder}')
    for path in sorted(folder.rglob('*')):
        if path.is_file():
            print(f'\n--- {path.relative_to(folder)} ---\n')
            print(path.read_text(encoding='utf-8', errors='replace'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Update Armor Fusion, HuntLog and Hunter\'s Compendium from their latest stable GitHub releases.')
    parser.add_argument('--game-dir', type=Path, help='Folder containing mhgurecomp.exe')
    parser.add_argument('--check', action='store_true', help='Check and verify releases without installing or prompting')
    parser.add_argument('--no-pause', action='store_true', help='Do not wait for Enter when finished')
    parser.add_argument('--licenses', action='store_true', help='Print the bundled software license notices')
    args = parser.parse_args(argv)
    if args.licenses:
        show_licenses()
        return 0
    code = 0
    print('MHGU Mod Updater\nArmor Fusion | HuntLog | Hunter\'s Compendium\n')
    try:
        game = args.game_dir or default_game_dir()
        if game is None:
            if args.check:
                raise RuntimeError('Use --game-dir with the folder containing mhgurecomp.exe.')
            game = choose_game_dir()
            if game is None:
                print('Cancelled. No mods changed.')
                return 0
            print(f'Selected game folder: {game}')
            if input('Update mods in this folder? [y/N] ').strip().casefold() not in {'y', 'yes'}:
                print('Cancelled. No mods changed.')
                return 0
        print(f'Game folder: {game}\n')
        result = update_game(game, check=args.check)
        if args.check:
            for item in result:
                print(f'{item.name}: {item.status}' + (f' ({item.version})' if item.version else ''))
            print('\nCheck finished. No mods changed.')
        else:
            print('\nFinished. Saves, load order and other mods were preserved.')
    except (Exception, KeyboardInterrupt) as error:
        print(f'\nERROR: {error}')
        code = 1
    finally:
        if not args.no_pause and not args.check:
            try:
                input('\nPress Enter to close...')
            except (EOFError, KeyboardInterrupt):
                pass
    return code


if __name__ == '__main__':
    raise SystemExit(main())
