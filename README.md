# MHGU Mod Updater

A portable Windows updater for [Armor Fusion](https://github.com/Rikiar73574/mhgu-armor-fusion),
[HuntLog](https://github.com/Rikiar73574/mhgu-huntlog) and
[Hunter's Compendium](https://github.com/Rikiar73574/mhgu-compendium).

[Download MHGU-Mod-Updater.exe](https://github.com/Rikiar73574/mhgu-mod-updater/releases/latest/download/MHGU-Mod-Updater.exe)

## Run

1. Close MHGURecomp.
2. Put `MHGU-Mod-Updater.exe` beside `mhgurecomp.exe` and run it.
3. Read the result, then start the game normally.

If the updater is elsewhere, select `mhgurecomp.exe` when prompted. No Python
installation is needed. It checks the three projects' latest stable GitHub
releases each time you run it; draft and pre-release builds are skipped.

It also checks for a newer updater release. A verified self-update keeps a
backup of the previous EXE, restarts the new version, then continues with the
mods. Users of v0.1.0 must download v0.1.1 or newer once to gain this feature.

Downloads are verified before installation. Existing mod folders are backed up
under the game's `backups` directory. Saves, journals, wishlist data and other
mods are preserved. Duplicate copies of a managed plugin must be resolved
before updating. The updater does not close the game or change its executable.

Core plugin files are found recursively, regardless of folder names. Extra
wrapper folders and loose core files are repaired into the correct layout;
custom fonts and settings move with the mod. Bundles containing separate mod
folders are split into working installations. Conflicting custom files or
duplicate core files stop the update before anything is replaced.

Missing mods are installed automatically. Existing mod order and disabled
choices are retained, including disabled wrappers that must be split during
repair. Older handwritten load-order lists are updated only as needed for new
installations. Keep the EXE and run it again whenever you want updates.

The whetstone icon uses the same MHGU pixel-sprite style as the game and its
updater. See the bundled icon notice for the artwork's provenance and rights.

## Source

The updater uses Python's standard library. Its Windows executable bundles
CPython and the PyInstaller bootloader. Authored code is MIT; bundled components
retain their own licenses. The executable's `--licenses` option displays them.

Regression cases run on Windows CPython under Wine. The packaged EXE
installed all three public releases in a disposable game folder without Python
installed, preserved its save/settings/other-mod fixtures, and left every file
unchanged on a second run. Running-game refusal and rollback were also tested.
Physical Windows hardware was not available for this release's validation.

Run the regression suite with `python -m unittest discover -s tests -v`.
To build on Windows, use official x64 CPython 3.12.10, install
`pyinstaller==6.22.3`, then run `python build_windows.py`. The result is
`dist/MHGU-Mod-Updater.exe` with its licenses embedded.
