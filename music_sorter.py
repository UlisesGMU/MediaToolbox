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
more formats than MP3, and undo like the Folder Sorter.

Part of Media Toolbox; can also be run on its own:  python music_sorter.py
Requires:  pip install mutagen
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton,
)

from common import (
    DIM_COLOR, ERROR_COLOR, OK_COLOR, WARN_COLOR, Batcher, Column, FolderPicker, HistoryDialog,
    HistoryStore, RecordFilter, RecordModel, ToolTab, load_tool_settings, make_table_view,
    missing_library_banner, move_files, parse_extensions, remember_recent, revert_batch,
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

DEFAULT_SETTINGS = {
    "recent_sources": [r"C:\Users\UlisesGM\Music\Music"],
    "recent_dests": [r"C:\Users\UlisesGM\Music\Sorted"],
    "extensions": [".mp3"],             # The original only sorted MP3s.
    "artist_source": "original",
    "on_conflict": "duplicates",
    "duplicates_folder": "Duplicates",  # Inside the destination folder.
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


def destination_for(dest_root: Path, artist: str, album: str, filename: str) -> Path:
    """dest/Artist/Album/file, with names made safe for Windows."""
    a = sanitize_component(artist) or UNKNOWN_ARTIST
    b = sanitize_component(album) or UNKNOWN_ALBUM
    return dest_root / a / b / (sanitize_component(filename) or filename)


def plan_music(src_root: Path, dest_root: Path, settings: dict, progress=None, cancelled=None, emit=None) -> dict:
    """Read the tags of every music file under src_root and stream a plan row for each."""
    exts = {e.lower() for e in settings["extensions"]}
    keys = ARTIST_SOURCES.get(settings.get("artist_source", "original"), ARTIST_SOURCES["original"])[1]
    if progress:
        progress(0, 0, "Looking for music files…")

    # Don't re-read files that are already sorted when the destination is inside the source.
    try:
        skip_dir = dest_root.resolve()
    except OSError:
        skip_dir = None
    files = []
    for folder, dirs, names in os.walk(src_root):
        if skip_dir is not None:
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
        batch.add(make_row(path, src_root, dest_root, artist, album, has_tags=tags is not None))
    batch.flush()
    return {"found": len(files)}


def make_row(path: Path, src_root: Path, dest_root: Path, artist: str, album: str,
             has_tags: bool = True, manual: bool = False) -> dict:
    row = {"path": str(path), "file": path.name, "include": True, "manual": manual,
           "has_tags": has_tags, "artist": artist, "album": album}
    try:
        row["from"] = str(path.parent.relative_to(src_root))
    except ValueError:
        row["from"] = str(path.parent)
    update_destination(row, dest_root)
    return row


def update_destination(row: dict, dest_root: Path) -> None:
    """Recompute destination and status after the artist or album changed."""
    dst = destination_for(dest_root, row["artist"], row["album"], row["file"])
    row["dst"] = str(dst)
    try:
        row["dest"] = str(dst.parent.relative_to(dest_root))
    except ValueError:
        row["dest"] = str(dst.parent)
    same_file = dst.exists() and Path(row["path"]).resolve() == dst.resolve()
    if same_file:
        row["status"], row["include"] = "Already in place", False
    elif dst.exists():
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
    Column("status", "Status"),
    Column("from", "From", width=160),
]


class MusicSorterTab(ToolTab):
    title = "Music Sorter"

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
        self.ext_edit.setText(", ".join(self.settings["extensions"]))
        self.settings["artist_source"] = self.artist_combo.currentData()
        self.settings["on_conflict"] = self.conflict_combo.currentData()

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
        self.update_summary()

    def _edited(self, row: dict, key: str, value) -> bool:
        if key == "include":
            _color(row)
            return True
        row[key] = str(value).strip()
        row["manual"] = True  # Edited by hand: "No tags" no longer applies.
        if self.dest_root:
            update_destination(row, self.dest_root)
        return True

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
        if not src or not src.is_dir():
            QMessageBox.warning(self, "Folder not found", f"'{self.source.text()}' doesn't exist or isn't a folder.")
            return
        if not dest or not dest.is_absolute():
            QMessageBox.warning(self, "Destination", "Choose where the sorted folders should go (a full path).")
            return
        self._read_options()
        self.settings["recent_sources"] = remember_recent(self.settings["recent_sources"], src)
        self.settings["recent_dests"] = remember_recent(self.settings["recent_dests"], dest)
        save_tool_settings(SETTINGS_KEY, self.settings)
        self.source.set_recent(self.settings["recent_sources"])
        self.dest.set_recent(self.settings["recent_dests"])
        self.source.set_text(str(src))
        self.dest.set_text(str(dest))
        self.src_root, self.dest_root = src, dest
        self.model.set_rows([])
        self.table.setSortingEnabled(False)  # Faster while rows stream in.

        def partial(rows):
            self.model.add_rows(rows)
            self.update_summary()

        def finished(result):
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
        # Refresh "Duplicate" status right before moving; files may have changed since the scan.
        for r in todo:
            update_destination(r, self.dest_root)
        dups = sum(1 for r in todo if r["status"] == "Duplicate")
        what = CONFLICT_OPTIONS[self.settings["on_conflict"]].split("  (")[0].lower()
        msg = f"Move {len(todo)} file(s) into Artist/Album folders in:\n{self.dest_root}"
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
        a = m.addAction("Open destination folder", lambda: self.safe_open(dest_dir))
        a.setEnabled(dest_dir.is_dir())
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
        for r in rows:
            self._edited(r, key, text)
        self.model.refresh_all()
        self.update_summary()


if __name__ == "__main__":
    run_standalone(MusicSorterTab)
