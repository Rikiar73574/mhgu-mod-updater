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

Downloads are verified before installation. Existing mod folders are backed up
under the game's `backups` directory. Saves, journals, wishlist data and other
mods are preserved. Duplicate copies of a managed plugin must be resolved
before updating. The updater does not close the game or change its executable.

Existing custom mod-folder names are detected from their plugin files. A first
installation uses the standard folder name; enable the mod in Forge if needed.
Keep the executable and run it again whenever you want to check for updates.

## Source

The updater uses Python's standard library. Its Windows executable bundles
CPython and the PyInstaller bootloader. Authored code is MIT; bundled components
retain their own licenses. The executable's `--licenses` option displays them.

All 37 regression cases passed on Windows CPython under Wine. The packaged EXE
installed all three public releases in a disposable game folder without Python
installed, preserved its save/settings/other-mod fixtures, and left every file
unchanged on a second run. Running-game refusal and rollback were also tested.
Physical Windows hardware was not available for this release's validation.

Run the regression suite with `python -m unittest discover -s tests -v`.
To build on Windows, use official x64 CPython 3.12.10, install
`pyinstaller==6.22.3`, then run `python build_windows.py`. The result is
`dist/MHGU-Mod-Updater.exe` with its licenses embedded.
