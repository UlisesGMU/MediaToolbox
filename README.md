# Media Toolbox

A desktop app that bundles five tools for keeping a video and music library
in order — sorting downloads into folders, checking videos and series for
problems, listing folder contents, and sorting music by its tags — each in
its own tab of one window.

Built with the assistance of Claude (Anthropic).

## What it does

**Folder Sorter** — sorts video files into one folder per series, based on
their names.

- Detects the series name from several naming styles, tried in order, each
  of which can be turned off or reordered:
  - `Series Title - 01 (1080p) [ABCD1234].mkv` (the original pattern, always first)
  - `[Group] Series Title - 01 (1080p).mkv`
  - `Series.Title.S01E02.720p.mkv`
  - `Series Title 1x02.mkv`
  - `Series Title Episode 02.mkv` (also `Ep 02`, `Cap 02`, `Capitulo 02`)
  - `[Group]_Series_Title_02_[64BEA878].mkv`
- Applies your own rules first (whole word, any text, or regex), such as the
  built-in `movie` → `Movies`, and accepts your own naming patterns as regexes.
- Can create the folders in a separate output folder, and sends
  differently-spelled versions of one series to a single folder.
- Shows a preview first, with search, sorting, inline editing and per-file
  include/exclude; every run can be undone.

**Video Check** — reads each video's frame rate information with MediaInfo.

- Flags files whose frame rate can't be read or is 0 (errors) and files with
  a variable or missing frame rate mode (warnings), like the original script.
- Can also warn when a file's minimum frame rate drops below a value you set.
- Shows resolution, codec, duration and size for every file; filter by
  errors or warnings, re-check selected files, and export a CSV or a text
  report.

**Episode Check** — looks inside each series folder for missing episodes.

- Reports missing episode numbers as compact ranges (`3-4, 9`) and also
  duplicates, such as `05` and `05v2` side by side.
- Uses the original `- 01` pattern first, then optional patterns for other
  naming styles (drag to reorder), plus an optional custom regex.
- Can count from episode 1 or from the lowest episode found, and can look
  in nested folders; files it couldn't number are listed on hover.
- Saves the same kind of report the original wrote, or a CSV.

**File List** — lists every folder and file under a directory.

- Keeps the original order and TXT layout (subfolders first, then files by
  extension and name, with a count per folder and a grand total).
- Adds size and modified-date columns, a search box, and a per-type summary
  you can click to show only that type.
- Exports TXT or CSV of exactly what's shown.

**Music Sorter** — moves music into `Artist/Album` folders using its tags.

- Takes the artist folder from performer, then album artist, then artist,
  like the original; other orders can be chosen.
- Files already present in their album folder go to a `Duplicates` folder,
  like the original, or can be renamed or left in place.
- Reads MP3 by default, and FLAC, M4A, OGG, Opus and others when their
  extensions are added.
- Shows a preview first, where artist and album can be edited per file or
  for many files at once; every run can be undone.

**In every tab:** long tasks run in the background with a progress bar and
a Cancel button, so the other tabs stay usable. Right-click any row to open
the file, reveal it in its folder, or copy names and paths. Folders can be
dropped onto a tab to start it, and F5 reruns the current tab.

## Requirements

- Windows 10 or 11, or Linux (macOS should work but is untested).
- Python 3.9+.
- PySide6 (`pip install PySide6`).
- Optional, for two of the tabs:
  - Video Check: `pip install pymediainfo`. On Linux it also needs the
    MediaInfo library from your package manager (`libmediainfo`).
  - Music Sorter: `pip install mutagen`.

  Without them, those tabs explain what to install and the rest of the app
  works normally.
- No administrator rights needed — the sorting tabs only move files and
  create folders in locations you choose, never delete a file, and only
  remove folders they created themselves, and only when they're empty. The
  other three tabs only read.

## Running it

```
pip install PySide6 pymediainfo mutagen
python media_toolbox.py
```

Each tool can also be opened on its own, for example
`python episode_check.py`. All the `.py` files need to stay in the same
folder.

## Building a standalone .exe (optional)

```
pip install pyinstaller
pyinstaller --onefile --noconsole --collect-all pymediainfo --name MediaToolbox media_toolbox.py
```

The resulting `.exe` (in `dist\`) runs without Python installed, but only
on machines matching the same architecture (32-bit vs 64-bit) as whichever
Python built it. `--collect-all pymediainfo` makes sure the MediaInfo
library travels inside the `.exe`. Expect it to be fairly large, since it
bundles Qt.

## Settings and history

Each tab keeps its own settings (recent folders, options, rules) and, for
the two sorting tabs, the history of the last 50 runs. They live in
`%APPDATA%\MediaToolbox\` on Windows and `~/.config/MediaToolbox/` on
Linux. Delete a tab's `.json` file to reset it to the defaults. Settings
and history from the standalone Folder Sorter are imported automatically
the first time.

## Known limitations

- The Folder Sorter only sorts files directly inside the scanned folder;
  subfolders are left alone, since they're usually what was already sorted.
- Folder Sorter rules run before series detection, so a series with
  "Movie" in its title goes to `Movies` unless a rule for that series is
  placed above the movie rule.
- The original naming pattern keeps a leading `[Group]` tag in folder names
  (`[SubsPlease] Frieren`); an option in the Name detection settings drops it.
- Episode Check treats all numbers in a folder as one sequence, so a folder
  holding several seasons numbered `S01E01`, `S02E01`… shows duplicates.
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
