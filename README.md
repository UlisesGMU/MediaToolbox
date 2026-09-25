# Media Toolbox

A desktop app that bundles six tools for keeping a video and music library
in order — sorting downloads into folders, checking videos and series for
problems, listing folder contents, and sorting music by its tags — each in
its own tab of one window.

Built with the assistance of Claude (Anthropic).

**Version 3.4.0.** What changed in each version is in `CHANGELOG.md`.

## What it does

**Video Sorter** (called Folder Sorter in earlier versions) — sorts video
files into one folder per series, based on their names.

- Detects the series name from several naming styles, tried in order, each
  of which can be turned off or reordered:
  - `Series Title - 01 (1080p) [ABCD1234].mkv` (the original pattern, always first)
  - `[Group] Series Title - 01 (1080p).mkv`
  - `Series.Title.S01E02.720p.mkv`
  - `Series Title 1x02.mkv`
  - `Series Title Episode 02.mkv` (also `Episodio 02`, `Ep 02`, `Cap 02`, `Capitulo 02`)
  - `[Group] Series Title - ONA [1080p].mkv` (also OVA, OAD, Special: sorted with the series)
  - `[Group]_Series_Title_02_[64BEA878].mkv`
- Applies your own rules first (whole word, any text, or regex), such as the
  built-in `movie` → `Movies`, and accepts your own naming patterns as regexes.
- Finds movies whose name doesn't say "Movie": a file with no episode
  number that runs 60 minutes or more (adjustable) goes to `Movies`. This
  uses MediaInfo, like Video Check.
- Can create the folders in a separate output folder, and sends
  differently-spelled versions of one series to a single folder, even when
  one version has the `[Group]` tag and the other doesn't
  (`[Erai-raws] Shibou Yuugi…` and `Shibou Yuugi…`).
- Shows a preview first, with search, sorting, inline editing and per-file
  include/exclude; every run can be undone.
- **Include subfolders** (off by default) also sorts video files inside
  subfolders, e.g. downloads that arrived in their own folders. Files
  already in the folder they'd go to are left out, so scanning a whole
  library only picks up what's out of place; `Old versions` folders and a
  separate output folder inside the source are never touched.
- After sorting, **Check episodes…** opens the output folder in Episode Check.

**Video Check** — reads each video's frame rate information with MediaInfo.

- Flags files whose frame rate can't be read or is 0 (errors) and files with
  a variable or missing frame rate mode (warnings), like the original script.
- Can also warn when a file's minimum frame rate drops below a value you set.
- Can verify each file against the CRC32 code in its name (`[80186B58]`);
  a mismatch is reported as an error, since it usually means a damaged
  download. Off by default, because it reads whole files.
- Shows resolution, codec, duration, size and modified date for every file; filter by
  errors or warnings, re-check selected files, and export a CSV or a text
  report.

**Episode Check** — looks inside each series folder for missing episodes.

- Reports missing episode numbers as compact ranges (`3-4, 9`).
- Knows that `v2`, `v3`… are newer releases of the same episode: when `05`
  and `05v2` are both in a folder, `05` is listed as an older version. Two
  files with the same episode *and* version (e.g. from two release groups)
  are reported as duplicates.
- **Clean up older versions** moves those older files into an `Old versions`
  folder inside each series folder, and Undo puts them back. Deleting them
  instead is a separate option, off by default: with it on, files go to the
  Recycle Bin if `send2trash` is installed, and are deleted permanently
  otherwise (the confirmation says which).
- Counts each season separately (`S01E05` and `S02E05` are different
  episodes), showing ranges and gaps per season (`S02: 3-4`).
- Works on a folder of series (one row per series) or on a single series
  folder.
- Uses the original `- 01` pattern first, then optional patterns for other
  naming styles (drag to reorder), plus an optional custom regex.
- Can count from episode 1 or from the lowest episode found, and can look
  in nested folders; files it couldn't number are listed on hover.
- Shows the date of the newest file in each folder, which tells you when
  a series last got a new episode.
- Saves the same kind of report the original wrote, or a CSV.

**File List** — lists every folder and file under a directory.

- Keeps the original order and TXT layout (subfolders first, then files by
  extension and name, with a count per folder and a grand total).
- Shows the size of every file and every folder (a folder counts
  everything inside it, at any depth), both friendly (`1.2 GB`) and exact
  in bytes, plus its modified date, in the table, the TXT and the CSV. The CSV keeps bytes as a
  plain number so Excel can sum and sort it.
- Can compute a checksum for every file. The default, CRC32, is fast and
  is the same 8-character code release groups put in file names
  (`[80186B58]`), so each file is checked against its name: a mismatch
  means a corrupted or incomplete download. xxHash (the fastest; needs
  `pip install xxhash`), MD5, SHA-1 and SHA-256 are also available. Off by
  default, since every byte has to be read.
- **Find duplicates** shows identical files anywhere in the list, even with
  different names, grouped and sorted biggest first, with the space the
  extra copies use. Only files that share a size are read.
- Adds a modified-date column, a search box, and a per-type summary you can
  click to show only that type.
- Exports TXT or CSV of exactly what's shown, with totals at the end.

**Music Sorter** — moves music into `Artist/Album` folders using its tags.

- Takes the artist folder from performer, then album artist, then artist,
  like the original; other orders can be chosen.
- Files already present in their album folder go to a `Duplicates` folder,
  like the original, or can be renamed or left in place.
- Reads MP3 by default, and FLAC, M4A, OGG, Opus and others when their
  extensions are added.
- Goes through subfolders, like the original; **Include subfolders** can
  be turned off to sort only the files directly in the music folder.
- Shows a preview first, where artist and album can be edited per file or
  for many files at once; every run can be undone.
- Optional, off by default: the year in album folders (`2004 - Album` or
  `Album (2004)`), and file names built from the tags (`01 - Title`,
  `Artist - Title`, `01 - Artist - Title`). Files without the needed tags
  keep their name.

**Music Check** — finds songs with missing tags (title, artist, album,
year, track number), no cover art (embedded in the file, or a `cover.jpg` /
`folder.jpg` next to it), and duplicate songs: the same artist and title,
even with different file names or formats. It only reads; nothing is changed.
Filter by problem, search, and export CSV or TXT.

**In every tab:** every exported file gets the date and time in its
suggested name (`episode_report_2026-09-24_14-05-33.txt`) and a "Created"
line inside, so exports never overwrite each other and are easy to date.
**Open last CSV** and **Open last report** reopen the newest ones, even after a restart.
Anything that touches the disk runs in the background with a progress bar
and a Cancel button, so the window never freezes and the other tabs stay
usable. That includes scanning, checking, moving and undoing, saving
exports, and even checking which recent folders still exist, which on a
disconnected network drive can otherwise hang for many seconds. Right-click any row to open
the file, reveal it in its folder, copy names and paths, or send its folder
to another tab ("Check episodes in this folder", "List files in this
folder"…).

**Checksums are remembered.** Every checksum calculated (in File List or
Video Check) is stored with the file's size and date, so running again on
the same library is instant for files that haven't changed; a changed file
is always read again. File > Clear checksum cache forgets them. Folders can be
dropped onto a tab to start it, and F5 reruns the current tab.

## Requirements

- Windows 10 or 11, or Linux (macOS should work but is untested).
- Python 3.9+.
- PySide6 (`pip install PySide6`).
- Optional:
  - Video Check, and finding movies by length in Video Sorter:
    `pip install pymediainfo`. On Linux it also needs the MediaInfo library
    from your package manager (`libmediainfo`).
  - Music Sorter: `pip install mutagen`.
  - Faster checksums in File List: `pip install xxhash`.
  - Deleting older versions to the Recycle Bin instead of permanently:
    `pip install send2trash`.

  Without them, those tabs explain what to install and the rest of the app
  works normally. **Help > About** shows which ones are missing and can
  install them with one click (it runs pip for you), then **Restart** the
  app so they're loaded. On Linux, a system Python often refuses pip
  installs; the dialog then suggests your package manager or a virtual
  environment instead. The built `.exe` can't install packages: install
  them on the PC that builds it and run `build_exe.py` again.
- No administrator rights needed — the tools only move files and create
  folders in locations you choose, and only remove folders they created
  themselves, and only when they're empty. The one exception is deleting
  older episode versions, which only happens if you turn that option on.

## Running it

```
pip install PySide6 pymediainfo mutagen xxhash send2trash
python media_toolbox.py
```

Each tool can also be opened on its own, for example
`python episode_check.py`. All the `.py` files need to stay in the same
folder.

## Version

The version is shown in the window title and in Help > About, which also
lists the optional packages installed on the PC. Every exported report and
CSV records the version that created it, and so does a settings backup.
`python media_toolbox.py --version` prints it.

To release a new version, change `APP_VERSION` in `common.py` (the only
place it's written) and add an entry to `CHANGELOG.md`: the first number for
big changes, the second for new features, the third for fixes.

## Building a standalone .exe (optional)

```
pip install pyinstaller
python build_exe.py
```

The script lists which optional packages it found, includes them (with the
MediaInfo library pymediainfo needs), and writes `dist\MediaToolbox.exe`.
Use `python build_exe.py --onedir` for a folder instead of a single file,
which starts faster. On Windows the version is written into the `.exe`
(right-click > Properties > Details). The result runs without Python installed, but only on
machines matching the same OS and architecture (32-bit vs 64-bit) as the
Python that built it. Expect it to be fairly large, since it bundles Qt.

## Settings and history

Each tab keeps its own settings (recent folders, options, rules) and, for
the tabs that move files, the history of the last 50 runs. They live in
`%APPDATA%\MediaToolbox\` on Windows and `~/.config/MediaToolbox/` on
Linux. Delete a tab's `.json` file to reset it to the defaults. Settings
and history from the Folder Sorter (both the standalone version and the
earlier tab) are imported automatically the first time.

**File > Export settings** saves every tab's settings in one file, and
**Import settings** restores them, e.g. to move your rules and patterns to
another PC. Histories and the checksum cache aren't included, since they
refer to files on this PC.

## Known limitations

- Video Sorter rules run before series detection, so a series with
  "Movie" in its title goes to `Movies` unless a rule for that series is
  placed above the movie rule.
- The original naming pattern keeps a leading `[Group]` tag in folder names
  (`[Az-Animex] Colorful`); an option in the Name detection settings drops it.
- Files named with only a number (`01-Episodio-1.mkv`) have no series name
  to sort by, so they stay unmatched; right-click → Change destination.
- Finding movies by length also catches long single files that aren't
  movies (e.g. a 90-minute special without "Special" in its name); the
  Reason column says "Movie by length" so they're easy to spot.
- Episode Check only knows the season when the name has one (`S02E05`,
  `2x05`); a folder mixing `Title - 05` with `Title S02E05` shows them as
  "No season" and "S02".
- Find duplicates uses the checksum type chosen in File List. With CRC32,
  two different files of exactly the same size could in theory share a
  checksum (about 1 in 4 billion); pick SHA-256 for certainty.
- Checksums on large video folders take a while even with CRC32, because
  every byte is read and a regular hard drive is the limit; Cancel stops
  within a few seconds.
- The CRC check only works for files whose name contains an 8-character
  code in square brackets; other files just get their checksum.
- Moving to a folder on a different drive copies each file and then deletes
  the original, which is much slower than a move on the same drive.
  Reverting such a run is equally slow.
- A revert can only restore files that are still where the tool put them.
  Files moved or renamed by hand afterwards are skipped and listed.

## License

GPL-3.0. See the `LICENSE` file (or add one from
[gnu.org](https://www.gnu.org/licenses/gpl-3.0.txt) if you haven't yet) —
note that GPL-3.0 is copyleft: anything you build on top of this and
distribute must also be released under GPL-3.0.
