#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Music Sorter - moves music files into Artist/Album folders using their tags.

Same idea as the original script:
    - The artist folder comes from the "performer" tag, then "album artist",
      then "artist" (you can pick a different order).
    - If a file with the same name is already in its album folder, it goes
      to a Duplicates folder instead.
New here: a preview before anything moves, editable artist/album per file,
more formats than MP3, undo like the Video Sorter, and two optional extras:
the year in album folders ("2004 - Album") and file names built from the
tags ("01 - Song Title.mp3"). Both are off by default.

Part of Media Toolbox; can also be run on its own:  python music_sorter.py
Requires:  pip install mutagen
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton,
)

from common import (
    DIM_COLOR, ERROR_COLOR, OK_COLOR, WARN_COLOR, Batcher, Column, FolderPicker, HistoryDialog,
    HistoryStore, RecordFilter, RecordModel, ToolTab, load_tool_settings, make_table_view,
    missing_library_banner, move_files, parse_extensions, remember_recent, revert_batch, check_exists, require_folder,
    run_standalone, sanitize_component, save_tool_settings, selected_records, unique_path,
)

# mutagen is optional: without it this tab explains how to install it,
# and every other tab keeps working.
try:
    import mutagen
    from mutagen.easyid3 import EasyID3
except ImportError:  # pragma: no cover - depends on the machine
    mutagen = None
    EasyID3 = None

SETTINGS_KEY = "music_sorter"

# Where the artist folder name comes from, tried in order until a tag has a value.
ARTIST_SOURCES = {
    "original": ("Performer, album artist, artist  (original)", ["performer", "albumartist", "artist"]),
    "albumartist": ("Album artist, then artist", ["albumartist", "artist"]),
    "artist": ("Artist only", ["artist"]),
}

CONFLICT_OPTIONS = {
    "duplicates": "Move to the Duplicates folder  (original)",
    "rename": "Keep both, rename as \"song (1).mp3\"",
    "skip": "Leave the file where it is",
}

UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_ALBUM = "Unknown Album"

# How the Artist/Album folders are named. When a file has no year tag, the
# album folder is just the album name.
FOLDER_LAYOUTS = {
    "artist_album": "Artist / Album  (original)",
    "artist_year_album": "Artist / Year - Album",
    "artist_album_year": "Artist / Album (Year)",
}

# How the files themselves are named. If a file lacks the tags a format
# needs (no title, or no track number), it keeps its current name.
FILE_NAMING = {
    "keep": "Keep the file name  (original)",
    "track_title": "01 - Title",
    "artist_title": "Artist - Title",
    "track_artist_title": "01 - Artist - Title",
}

DEFAULT_SETTINGS = {
    "recent_sources": [r"C:\Users\UlisesGM\Music\Music"],
    "recent_dests": [r"C:\Users\UlisesGM\Music\Sorted"],
    "extensions": [".mp3"],             # The original only sorted MP3s.
    "recursive": True,                  # The original went through every subfolder (os.walk).
    "artist_source": "original",
    "on_conflict": "duplicates",
    "duplicates_folder": "Duplicates",  # Inside the destination folder.
    "folder_layout": "artist_album",    # The original layout.
    "file_naming": "keep",              # The original kept file names as they were.
}


# =============================================================== core logic (no Qt here)

def read_tags(path: Path) -> dict[str, list[str]] | None:
    """Tags as {lowercase key: [values]}, or None if the file has no readable tags.

    mutagen's "easy" mode gives the same key names (artist, album, albumartist,
    performer) for MP3, FLAC, M4A, OGG and others. For MP3 it falls back to
    EasyID3 directly, exactly like the original script.
    """
    tags = None
    try:
        f = mutagen.File(str(path), easy=True)
        if f is not None and f.tags is not None:
            tags = {k.lower(): list(f.tags[k]) for k in f.tags.keys()}
    except Exception:  # noqa: BLE001 - damaged files are reported, not fatal
        tags = None
    if not tags and path.suffix.lower() == ".mp3" and EasyID3 is not None:
        try:
            t = EasyID3(str(path))
            tags = {k.lower(): list(t[k]) for k in t.keys()}
        except Exception:  # noqa: BLE001
            tags = None
    return tags or None


def first_tag(tags: dict | None, keys: list[str]) -> str:
    for k in keys:
        for v in (tags or {}).get(k, []):
            if str(v).strip():
                return str(v).strip()
    return ""


def track_number(value: str) -> int | None:
    """'3/12' -> 3, '07' -> 7, '' -> None."""
    m = re.match(r"\s*(\d+)", value or "")
    return int(m.group(1)) if m else None


def year_of(value: str) -> str:
    """'2004-05-01' -> '2004', '' -> ''."""
    m = re.search(r"\d{4}", value or "")
    return m.group(0) if m else ""


def album_folder(album: str, year: str, layout: str) -> str:
    name = sanitize_component(album) or UNKNOWN_ALBUM
    if year and layout == "artist_year_album":
        return f"{year} - {name}"
    if year and layout == "artist_album_year":
        return f"{name} ({year})"
    return name


def new_file_name(row: dict, naming: str) -> str:
    """The file's name at its destination. Falls back to the current name
    whenever the tags needed for the chosen format are missing."""
    current = sanitize_component(row["file"]) or row["file"]
    title, artist, track = row.get("title", ""), row.get("artist", ""), row.get("track")
    title = re.sub(r"\s*[/\\]\s*", "-", title)  # "Part 1/2" -> "Part 1-2" rather than "Part 12".
    if naming == "keep" or not title:
        return current
    base = None
    if naming == "track_title" and track:
        base = f"{track:02d} - {title}"
    elif naming == "artist_title" and artist:
        base = f"{artist} - {title}"
    elif naming == "track_artist_title" and track and artist:
        base = f"{track:02d} - {artist} - {title}"
    if not base:
        return current
    # sanitize also removes "/" from names like "AC/DC" or "Title 1/2".
    return (sanitize_component(base) or current) + Path(row["file"]).suffix.lower()


def destination_for(dest_root: Path, row: dict) -> Path:
    """dest/Artist/Album/file, with names made safe for Windows."""
    artist = sanitize_component(row["artist"]) or UNKNOWN_ARTIST
    album = album_folder(row["album"], row.get("year", ""), row.get("layout", "artist_album"))
    return dest_root / artist / album / new_file_name(row, row.get("naming", "keep"))


def plan_music(src_root: Path, dest_root: Path, settings: dict, progress=None, cancelled=None, emit=None) -> dict:
    """Read the tags of every music file under src_root and stream a plan row for each."""
    exts = {e.lower() for e in settings["extensions"]}
    keys = ARTIST_SOURCES.get(settings.get("artist_source", "original"), ARTIST_SOURCES["original"])[1]
    if progress:
        progress(0, 0, "Looking for music files…")
    require_folder(src_root)

    # Don't re-read files that are already sorted when the destination is inside the source.
    try:
        skip_dir = dest_root.resolve()
    except OSError:
        skip_dir = None
    files = []
    for folder, dirs, names in os.walk(src_root):
        if not settings.get("recursive", True):
            dirs[:] = []  # Only the files directly in the music folder.
        elif skip_dir is not None:
            dirs[:] = [d for d in dirs if (Path(folder) / d).resolve() != skip_dir]
        files.extend(Path(folder) / n for n in names if Path(n).suffix.lower() in exts)
    files.sort(key=lambda p: str(p).lower())

    batch = Batcher(emit)
    for n, path in enumerate(files):
        if cancelled and cancelled():
            break
        if progress:
            progress(n, len(files), path.name)
        tags = read_tags(path)
        artist = first_tag(tags, keys)
        album = first_tag(tags, ["album"])
        extra = {
            "title": first_tag(tags, ["title"]),
            "track": track_number(first_tag(tags, ["tracknumber"])),
            "year": year_of(first_tag(tags, ["date", "originaldate", "year"])),
            "layout": settings.get("folder_layout", "artist_album"),
            "naming": settings.get("file_naming", "keep"),
        }
        batch.add(make_row(path, src_root, dest_root, artist, album, has_tags=tags is not None, extra=extra))
    batch.flush()
    return {"found": len(files)}


def make_row(path: Path, src_root: Path, dest_root: Path, artist: str, album: str,
             has_tags: bool = True, manual: bool = False, extra: dict | None = None) -> dict:
    row = {"path": str(path), "file": path.name, "include": True, "manual": manual,
           "has_tags": has_tags, "artist": artist, "album": album,
           "title": "", "track": None, "year": "", "layout": "artist_album", "naming": "keep"}
    row.update(extra or {})
    try:
        row["from"] = str(path.parent.relative_to(src_root))
    except ValueError:
        row["from"] = str(path.parent)
    update_destination(row, dest_root)
    return row


def update_destination(row: dict, dest_root: Path, exists: bool | None = None) -> None:
    """Recompute destination and status after the artist or album changed.

    exists: whether the destination file is already there, when the caller has
    checked that in the background; None checks it here (fine for one file).
    """
    dst = destination_for(dest_root, row)
    row["dst"] = str(dst)
    row["new_name"] = dst.name
    try:
        row["dest"] = str(dst.parent.relative_to(dest_root))
    except ValueError:
        row["dest"] = str(dst.parent)
    if exists is None:
        exists = dst.exists()
    same_file = exists and Path(row["path"]).resolve() == dst.resolve()
    if same_file:
        row["status"], row["include"] = "Already in place", False
    elif exists:
        row["status"] = "Duplicate"
    elif not row["has_tags"] and not row["manual"]:
        row["status"] = "No tags"
    else:
        row["status"] = "Pending"
    row["moved"] = False
    _color(row)


def _color(row: dict) -> None:
    s = row["status"]
    row["_color"] = {"Duplicate": WARN_COLOR, "No tags": WARN_COLOR, "Already in place": DIM_COLOR}.get(s)
    if s.startswith("Moved") or s == "Duplicate moved":
        row["_color"] = OK_COLOR
    elif s.startswith("Error") or s == "Missing":
        row["_color"] = ERROR_COLOR
    elif not row["include"] and s == "Pending":
        row["_color"] = DIM_COLOR
    row["_tip"] = f"{row['path']}\n->  {row['dst']}"


def run_moves(jobs: list[tuple[Path, Path]], dest_root: Path, src_root: Path, settings: dict,
              progress=None, cancelled=None):
    mode = settings.get("on_conflict", "duplicates")
    dup_dir = dest_root / (sanitize_component(settings.get("duplicates_folder", "")) or "Duplicates")
    if mode == "duplicates":
        # Like the original, but never overwrite a file already in Duplicates.
        on_exists = lambda d: (unique_path(dup_dir / d.name), "Duplicate moved")  # noqa: E731
    elif mode == "rename":
        on_exists = lambda d: (unique_path(d), "Moved (renamed)")  # noqa: E731
    else:
        on_exists = None
    return move_files(jobs, on_exists, label="Music Sorter", root=str(src_root), output=str(dest_root),
                      progress=progress, cancelled=cancelled)


# =============================================================== the tab

COLUMNS = [
    Column("include", "", check=True),
    Column("file", "File", stretch=True),
    Column("artist", "Artist", editable=True, width=180),
    Column("album", "Album", editable=True, width=180),
    Column("dest", "Destination", width=240),
    Column("new_name", "New name", width=220),
    Column("status", "Status"),
    Column("from", "From", width=160),
]


class MusicSorterTab(ToolTab):
    title = "Music Sorter"
    key = "music_sorter"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = load_tool_settings(SETTINGS_KEY, DEFAULT_SETTINGS)
        self.history = HistoryStore(SETTINGS_KEY)
        self.src_root: Path | None = None
        self.dest_root: Path | None = None
        lay = self.body_layout

        if mutagen is None:
            lay.addWidget(missing_library_banner("mutagen"))

        self.source = FolderPicker("Music:", self.settings["recent_sources"],
                                   "Folder with the music to sort (subfolders included)", "Choose music folder")
        self.source.chosen.connect(lambda _: self.scan())
        self.dest = FolderPicker("Sort into:", self.settings["recent_dests"],
                                 "Where the Artist/Album folders go", "Choose destination folder")
        lay.addWidget(self.source)
        lay.addWidget(self.dest)

        # --- options
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Extensions:"))
        self.ext_edit = QLineEdit(", ".join(self.settings["extensions"]))
        self.ext_edit.setToolTip("mutagen reads mp3, flac, m4a, ogg, opus, wma, wav and more")
        self.ext_edit.setMaximumWidth(200)
        opts.addWidget(self.ext_edit)
        self.recursive_chk = QCheckBox("Include subfolders")
        self.recursive_chk.setToolTip("Off: only the files directly in the music folder.")
        self.recursive_chk.setChecked(self.settings.get("recursive", True))
        opts.addWidget(self.recursive_chk)
        opts.addWidget(QLabel("Artist from:"))
        self.artist_combo = QComboBox()
        for key, (label, _keys) in ARTIST_SOURCES.items():
            self.artist_combo.addItem(label, key)
        self.artist_combo.setCurrentIndex(max(0, self.artist_combo.findData(self.settings["artist_source"])))
        opts.addWidget(self.artist_combo)
        opts.addWidget(QLabel("If the file exists:"))
        self.conflict_combo = QComboBox()
        for key, label in CONFLICT_OPTIONS.items():
            self.conflict_combo.addItem(label, key)
        self.conflict_combo.setCurrentIndex(max(0, self.conflict_combo.findData(self.settings["on_conflict"])))
        opts.addWidget(self.conflict_combo)
        lay.addLayout(opts)

        # Optional extras (the originals are the defaults): year in the album
        # folder, and file names built from the tags.
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Folders:"))
        self.layout_combo = QComboBox()
        for key, label in FOLDER_LAYOUTS.items():
            self.layout_combo.addItem(label, key)
        self.layout_combo.setCurrentIndex(max(0, self.layout_combo.findData(self.settings["folder_layout"])))
        opts.addWidget(self.layout_combo)
        opts.addWidget(QLabel("File names:"))
        self.naming_combo = QComboBox()
        for key, label in FILE_NAMING.items():
            self.naming_combo.addItem(label, key)
        self.naming_combo.setCurrentIndex(max(0, self.naming_combo.findData(self.settings["file_naming"])))
        self.naming_combo.setToolTip("Files missing the needed tags (title, track number) keep their name.")
        opts.addWidget(self.naming_combo)
        # Changing either updates the preview right away; no need to scan again.
        self.layout_combo.currentIndexChanged.connect(lambda _: self._naming_changed())
        self.naming_combo.currentIndexChanged.connect(lambda _: self._naming_changed())
        opts.addStretch()
        b_scan = QPushButton("Scan")
        b_scan.setToolTip("Read tags and preview where each file goes. Nothing is moved yet. (F5)")
        b_scan.clicked.connect(self.scan)
        b_scan.setEnabled(mutagen is not None)
        opts.addWidget(b_scan)
        lay.addLayout(opts)

        frow = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search file, artist, album or status (try \"Duplicate\" or \"No tags\")")
        self.search.setClearButtonEnabled(True)
        self.summary = QLabel("")
        frow.addWidget(self.search, 1)
        frow.addWidget(self.summary)
        lay.addLayout(frow)

        # --- table: artist and album are editable; the destination follows.
        self.model = RecordModel(COLUMNS)
        self.model.can_edit = lambda row, key: not row["moved"]
        self.model.on_edit = self._edited
        self.proxy = RecordFilter(self.model, self)
        self.table = make_table_view(self.proxy, COLUMNS)
        self.table.customContextMenuRequested.connect(self.show_menu)
        self.table.doubleClicked.connect(self._double_clicked)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.search.textChanged.connect(lambda _: self.update_summary())
        self.model.dataChanged.connect(lambda *_: self.update_summary())
        lay.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        b_all = QPushButton("Select all")
        b_all.clicked.connect(lambda: self._set_include(self.proxy.visible_rows(), True))
        b_none = QPushButton("Select none")
        b_none.clicked.connect(lambda: self._set_include(self.proxy.visible_rows(), False))
        b_hist = QPushButton("History…")
        b_hist.clicked.connect(lambda: HistoryDialog(self).exec())
        self.b_undo = QPushButton("Undo last")
        self.b_undo.clicked.connect(self.undo_last)
        self.b_go = QPushButton("Sort")
        self.b_go.setDefault(True)
        self.b_go.clicked.connect(self.organize)
        for b in (b_all, b_none, b_hist):
            bottom.addWidget(b)
        bottom.addStretch()
        bottom.addWidget(self.b_undo)
        bottom.addWidget(self.b_go)
        lay.addLayout(bottom)

        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        QShortcut(QKeySequence("F5"), self, activated=self.scan, context=ctx)
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.undo_last, context=ctx)
        self.on_idle()

    # ---------------------------------------------------------- helpers
    def _read_options(self):
        self.settings["extensions"] = parse_extensions(self.ext_edit.text()) or DEFAULT_SETTINGS["extensions"]
        self.settings["recursive"] = self.recursive_chk.isChecked()
        self.ext_edit.setText(", ".join(self.settings["extensions"]))
        self.settings["artist_source"] = self.artist_combo.currentData()
        self.settings["on_conflict"] = self.conflict_combo.currentData()
        self.settings["folder_layout"] = self.layout_combo.currentData()
        self.settings["file_naming"] = self.naming_combo.currentData()

    def _pending(self) -> list[dict]:
        return [r for r in self.model.rows if r["include"] and not r["moved"]
                and r["status"] in ("Pending", "Duplicate", "No tags")]

    def update_summary(self):
        rows = self.model.rows
        todo = len(self._pending())
        dups = sum(1 for r in rows if r["status"] == "Duplicate")
        untagged = sum(1 for r in rows if r["status"] == "No tags")
        moved = sum(1 for r in rows if r["moved"])
        text = f"{len(rows)} files,  {todo} to sort,  {dups} duplicates,  {untagged} without tags"
        if moved:
            text += f",  {moved} moved"
        self.summary.setText(text)
        self.b_go.setText(f"Sort {todo} file{'s' * (todo != 1)}" if todo else "Sort")
        self.b_go.setEnabled(todo > 0 and not self.busy)

    def on_idle(self):
        self.b_undo.setEnabled(bool(self.history.batches) and not self.busy)
        # Sorting is paused while rows stream in; back on even if the scan failed.
        if not self.busy and not self.table.isSortingEnabled():
            self.table.setSortingEnabled(True)
        self.update_summary()

    def _edited(self, row: dict, key: str, value) -> bool:
        if key == "include":
            _color(row)
            return True
        row[key] = str(value).strip()
        row["manual"] = True  # Edited by hand: "No tags" no longer applies.
        if self.dest_root:
            self._recheck_destinations([row])  # "Already there?" is checked in the background.
        return True

    def _naming_changed(self):
        self._read_options()
        save_tool_settings(SETTINGS_KEY, self.settings)
        rows = [r for r in self.model.rows if not r["moved"]]
        if not rows or not self.dest_root:
            return
        for r in rows:
            r["layout"], r["naming"] = self.settings["folder_layout"], self.settings["file_naming"]
        self._recheck_destinations(rows)

    def _recheck_destinations(self, rows: list[dict]):
        """Show new destinations at once, then check in the background which already exist."""
        dest_root = self.dest_root
        for r in rows:
            update_destination(r, dest_root, exists=False)
        self.model.refresh_all()
        self.update_summary()
        if self.busy:
            return  # The check before sorting catches duplicates anyway.

        def checked(existing: set[str]):
            for r in rows:
                if not r["moved"]:
                    update_destination(r, dest_root, exists=r["dst"] in existing)
            self.model.refresh_all()
            self.update_summary()

        self.run_task(check_exists, ([Path(r["dst"]) for r in rows],), checked,
                      f"Checking {len(rows)} destination(s)…")

    def _set_include(self, rows: list[dict], value: bool):
        for r in rows:
            if not r["moved"] and r["status"] != "Already in place":
                r["include"] = value
                _color(r)
        self.model.refresh_all()
        self.update_summary()

    def _double_clicked(self, proxy_index):
        col = COLUMNS[proxy_index.column()]
        if col.editable:
            return  # Double-click on Artist/Album edits them.
        row = self.model.rows[self.proxy.mapToSource(proxy_index).row()]
        self.safe_open(Path(row["dst"] if row["moved"] else row["path"]))

    def folder_dropped(self, path: Path):
        self.source.set_text(str(path))
        self.scan()

    # ---------------------------------------------------------- actions
    def scan(self):
        if self.busy or mutagen is None:
            return
        src, dest = self.source.path(), self.dest.path()
        if not src:
            QMessageBox.warning(self, "No folder", "Choose the music folder first.")
            return
        if not dest or not dest.is_absolute():
            QMessageBox.warning(self, "Destination", "Choose where the sorted folders should go (a full path).")
            return
        self._read_options()
        save_tool_settings(SETTINGS_KEY, self.settings)
        # Whether the music folder exists is checked in the background task.
        self.src_root, self.dest_root = src, dest
        self.model.set_rows([])
        self.table.setSortingEnabled(False)  # Faster while rows stream in.

        def partial(rows):
            self.model.add_rows(rows)
            self.update_summary()

        def finished(result):
            # The folders are real: now they're worth remembering.
            self.settings["recent_sources"] = remember_recent(self.settings["recent_sources"], src)
            self.settings["recent_dests"] = remember_recent(self.settings["recent_dests"], dest)
            save_tool_settings(SETTINGS_KEY, self.settings)
            self.source.set_recent(self.settings["recent_sources"])
            self.dest.set_recent(self.settings["recent_dests"])
            self.source.set_text(str(src))
            self.dest.set_text(str(dest))
            self.table.setSortingEnabled(True)
            self.update_summary()
            self.write_log(f"Read tags from {len(self.model.rows)} of {result['found']} file(s) in {src}.")

        self.run_task(plan_music, (src, dest, dict(self.settings)), finished, "Reading tags…", partial)

    def organize(self):
        if self.busy or not self.dest_root:
            return
        self._read_options()
        save_tool_settings(SETTINGS_KEY, self.settings)
        todo = self._pending()
        if not todo:
            return
        # Refresh "Duplicate" status right before moving (files may have changed since
        # the scan). Checking thousands of destinations takes a moment, so it runs in
        # the background and the confirmation appears when it's done.
        dest_root = self.dest_root

        def checked(existing: set[str]):
            for r in todo:
                update_destination(r, dest_root, exists=r["dst"] in existing)
            self.model.refresh_all()
            todo[:] = [r for r in todo if r["status"] != "Already in place"]
            if todo:
                self._confirm_and_move(todo, dest_root)

        self.run_task(check_exists, ([Path(r["dst"]) for r in todo],), checked,
                      f"Checking {len(todo)} destination(s)…")

    def _confirm_and_move(self, todo: list[dict], dest_root: Path):
        dups = sum(1 for r in todo if r["status"] == "Duplicate")
        what = CONFLICT_OPTIONS[self.settings["on_conflict"]].split("  (")[0].lower()
        msg = f"Move {len(todo)} file(s) into Artist/Album folders in:\n{dest_root}"
        if dups:
            msg += f"\n\n{dups} already exist there. For those: {what}."
        if QMessageBox.question(self, "Sort music", msg) != QMessageBox.StandardButton.Yes:
            return

        jobs = [(Path(r["path"]), Path(r["dst"])) for r in todo]

        def finished(result):
            batch, results = result
            for r in todo:
                status, new_path = results.get(r["path"], ("Error", None))
                r["status"] = status
                if new_path:
                    r["moved"], r["include"] = True, False
                    r["dst"] = str(new_path)
                    try:
                        r["dest"] = str(Path(new_path).parent.relative_to(self.dest_root))
                    except ValueError:
                        pass
                _color(r)
                if not status.startswith("Moved"):
                    self.write_log(f"{status}: {r['file']}" + (f"  ->  {new_path}" if new_path else ""))
            self.model.refresh_all()
            if batch:
                self.history.add(batch)
                self.write_log(f"Moved {len(batch['moves'])} file(s). Use Undo last to put them back.")

        self.run_task(run_moves, (jobs, self.dest_root, self.src_root, dict(self.settings)), finished,
                      f"Moving {len(jobs)} file(s)…")

    def undo_last(self):
        if self.history.batches and not self.busy:
            self.revert(self.history.last())

    def revert(self, batch: dict, after=None):
        if self.busy:
            return
        n = len(batch["moves"])
        if QMessageBox.question(self, "Revert", f"Put back {n} file(s) moved on {batch['time']}?") \
                != QMessageBox.StandardButton.Yes:
            return

        def finished(result):
            ok, errors, remaining = result
            if remaining:
                batch["moves"] = remaining
                self.history.save()
            else:
                self.history.remove(batch["id"])
            self.write_log(f"Reverted {ok} file(s).")
            for e in errors:
                self.write_log(f"  {e}")
            if errors:
                QMessageBox.warning(self, "Revert finished with problems",
                                    f"Reverted {ok} of {n}.\n\n" + "\n".join(errors[:15]))
            if after:
                after()
            if self.src_root and batch.get("root") == str(self.src_root):
                self.scan()

        self.run_task(revert_batch, (batch,), finished, f"Reverting {n} file(s)…")

    # ---------------------------------------------------------- context menu
    def show_menu(self, pos):
        rows = selected_records(self.table, self.proxy)
        if not rows:
            return
        first = rows[0]
        editable = [r for r in rows if not r["moved"]]
        m = QMenu(self)
        current = Path(first["dst"] if first["moved"] else first["path"])
        m.addAction("Play / open", lambda: self.safe_open(current))
        m.addAction("Show in folder", lambda: self.safe_open(current, reveal=True))
        dest_dir = Path(first["dst"]).parent
        # Not checked on disk when the menu opens (slow on network drives);
        # opening a folder that isn't there yet just shows a message.
        m.addAction("Open destination folder", lambda: self.safe_open(dest_dir))
        m.addSeparator()
        a = m.addAction(f"Set artist… ({len(editable)})", lambda: self._bulk_set(editable, "artist"))
        a.setEnabled(bool(editable))
        a = m.addAction(f"Set album… ({len(editable)})", lambda: self._bulk_set(editable, "album"))
        a.setEnabled(bool(editable))
        a = m.addAction("Include", lambda: self._set_include(editable, True))
        a.setEnabled(bool(editable))
        a = m.addAction("Exclude", lambda: self._set_include(editable, False))
        a.setEnabled(bool(editable))
        m.addSeparator()
        m.addAction("Copy file name" + ("s" if len(rows) > 1 else ""),
                    lambda: QApplication.clipboard().setText("\n".join(r["file"] for r in rows)))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _bulk_set(self, rows: list[dict], key: str):
        """Give several files the same artist or album (e.g. a compilation)."""
        values = sorted({r[key] for r in self.model.rows if r[key]}, key=str.lower)
        current = rows[0][key]
        if current in values:
            values.remove(current)
        values.insert(0, current)
        text, ok = QInputDialog.getItem(self, f"Set {key}", f"{key.capitalize()} for {len(rows)} file(s):",
                                        values, 0, True)
        if not ok:
            return
        # Update names at once; the "already there?" check for many files runs in the background.
        for r in rows:
            r[key] = str(text).strip()
            r["manual"] = True
        if self.dest_root and rows:
            self._recheck_destinations(rows)


if __name__ == "__main__":
    run_standalone(MusicSorterTab)
