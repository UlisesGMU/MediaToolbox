#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Music Check - finds music files with missing tags, no cover art, or that are
duplicates of another song (same artist and title, whatever the file name or
format). Read-only: it never changes a file.

Part of Media Toolbox; can also be run on its own:  python music_check.py
Requires:  pip install mutagen
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
)

from common import (
    ERROR_COLOR, WARN_COLOR, Batcher, Column, FolderPicker, RecordFilter, RecordModel, ToolTab,
    created_line, export_csv, load_tool_settings, make_table_view, missing_library_banner,
    parse_extensions, remember_recent, require_folder, run_standalone, save_text, save_tool_settings,
    selected_records,
)
from music_sorter import first_tag, read_tags, track_number, year_of

try:
    import mutagen
except ImportError:  # pragma: no cover - depends on the machine
    mutagen = None

SETTINGS_KEY = "music_check"

DEFAULT_SETTINGS = {
    "recent_folders": [r"C:\Users\UlisesGM\Music\Sorted"],
    "extensions": [".mp3", ".flac", ".m4a", ".ogg", ".opus"],
    "recursive": True,
    "folder_image_ok": True,   # A cover.jpg next to the songs counts as cover art.
}

# Image files that players show as the album cover when a song has none embedded.
FOLDER_IMAGES = {"cover.jpg", "cover.png", "folder.jpg", "folder.png", "front.jpg", "front.png", "album.jpg"}


# =============================================================== core logic (no Qt here)

def embedded_cover(path: Path) -> bool | None:
    """True/False: is there a picture inside the file? None: the file can't be read.
    Each format stores it differently, so each is checked."""
    try:
        f = mutagen.File(str(path))
    except Exception:  # noqa: BLE001 - damaged files are reported, not fatal
        return None
    if f is None:
        return None
    if getattr(f, "pictures", None):  # FLAC
        return True
    tags = getattr(f, "tags", None)
    if not tags:
        return False
    keys = [str(k) for k in tags.keys()]
    return any(k.startswith("APIC") for k in keys) or any(
        k in keys for k in ("covr", "metadata_block_picture", "WM/Picture"))  # MP3, M4A, OGG/Opus, WMA


def song_key(artist: str, title: str) -> str:
    """'The Seatbelts' + 'Tank!' -> 'theseatbeltstank': ignores case, spaces and punctuation."""
    return re.sub(r"[\W_]+", "", f"{artist} {title}".casefold())


def check_song(path: Path, root: Path, has_folder_image: bool, settings: dict) -> dict:
    tags = read_tags(path)
    artist = first_tag(tags, ["artist", "albumartist", "performer"])
    title = first_tag(tags, ["title"])
    album = first_tag(tags, ["album"])
    year = year_of(first_tag(tags, ["date", "originaldate", "year"]))
    track = track_number(first_tag(tags, ["tracknumber"]))
    cover = embedded_cover(path)

    problems = []
    if tags is None:
        problems.append("No tags")
    else:
        problems += [f"No {name}" for name, value in
                     (("title", title), ("artist", artist), ("album", album), ("year", year), ("track number", track))
                     if not value]
    if cover:
        cover_text = "Embedded"
    elif has_folder_image and settings.get("folder_image_ok", True):
        cover_text = "Folder image"
    else:
        cover_text = "None"
        problems.append("No cover art")
    try:
        rel = str(path.parent.relative_to(root))
    except ValueError:
        rel = str(path.parent)
    return {
        "path": str(path), "file": path.name, "folder": "" if rel == "." else rel,
        "artist": artist, "title": title, "album": album, "year": year,
        "track": track, "track_text": f"{track:02d}" if track else "", "cover": cover_text,
        "problems": problems, "key": song_key(artist, title) if artist and title else "",
        "_tip": str(path),
    }


def finish_rows(rows: list[dict]) -> None:
    """Mark duplicate songs (needs every row), then set result and color."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["key"]:
            groups.setdefault(r["key"], []).append(r)
    for same in groups.values():
        if len(same) > 1:
            for r in same:
                r["problems"].append(f"Duplicate song ({len(same)} copies)")
    for r in rows:
        r["issues"] = "; ".join(r["problems"])
        if not r["problems"]:
            r["result"], r["_color"] = "OK", None
        elif any(p.startswith(("No tags", "Duplicate")) for p in r["problems"]):
            r["result"], r["_color"] = "Problem", ERROR_COLOR
        else:
            r["result"], r["_color"] = "Missing info", WARN_COLOR


def scan_music(root: Path, settings: dict, progress=None, cancelled=None) -> list[dict]:
    """Returned all at once (not streamed): duplicates are only known at the end."""
    if progress:
        progress(0, 0, "Looking for music files…")
    require_folder(root)
    exts = {e.lower() for e in settings["extensions"]}
    files: list[tuple[Path, bool]] = []
    for folder, dirs, names in os.walk(root):
        if not settings.get("recursive", True):
            dirs[:] = []
        # One look per folder for a cover.jpg, instead of one per song.
        has_image = any(n.lower() in FOLDER_IMAGES for n in names)
        files += [(Path(folder) / n, has_image) for n in names if Path(n).suffix.lower() in exts]
    files.sort(key=lambda x: str(x[0]).lower())
    rows = []
    for n, (p, img) in enumerate(files):
        if cancelled and cancelled():
            break
        if progress:
            progress(n, len(files), p.name)
        rows.append(check_song(p, root, img, settings))
    finish_rows(rows)
    return rows


def text_report(rows: list[dict], root: str) -> str:
    bad = [r for r in rows if r["problems"]]
    lines = [f"Music check for: {root}", created_line(), "",
             f"Songs checked: {len(rows)}   With problems: {len(bad)}", ""]
    lines += [f" - {r['path']}\n    -> {r['issues']}" for r in bad] or ["All songs are OK."]
    return "\n".join(lines) + "\n"


# =============================================================== the tab

COLUMNS = [
    Column("result", "Result"),
    Column("file", "File", stretch=True),
    Column("artist", "Artist", width=150),
    Column("title", "Title", width=170),
    Column("album", "Album", width=150),
    Column("year", "Year"),
    Column("track_text", "Track", sort_key="track", numeric=True),
    Column("cover", "Cover"),
    Column("issues", "Issues", width=240),
    Column("folder", "Folder", width=160),
]
SHOW = ["All songs", "Problems only", "Missing cover", "Missing tags", "Duplicates"]


class MusicCheckTab(ToolTab):
    title = "Music Check"
    key = "music_check"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = load_tool_settings(SETTINGS_KEY, DEFAULT_SETTINGS)
        self.scanned_root: Path | None = None
        lay = self.body_layout
        if mutagen is None:
            lay.addWidget(missing_library_banner("mutagen", "Or use Help > About > Install missing packages."))

        self.folder = FolderPicker("Folder:", self.settings["recent_folders"], "Music folder (or drop it here)")
        self.folder.chosen.connect(lambda _: self.start())
        lay.addWidget(self.folder)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("Extensions:"))
        self.ext_edit = QLineEdit(", ".join(self.settings["extensions"]))
        self.ext_edit.setMaximumWidth(260)
        opts.addWidget(self.ext_edit)
        self.recursive_chk = QCheckBox("Include subfolders")
        self.recursive_chk.setChecked(self.settings.get("recursive", True))
        self.image_chk = QCheckBox("cover.jpg in the folder counts as cover art")
        self.image_chk.setChecked(self.settings.get("folder_image_ok", True))
        opts.addWidget(self.recursive_chk)
        opts.addWidget(self.image_chk)
        opts.addStretch()
        b = QPushButton("Check music")
        b.setDefault(True)
        b.setEnabled(mutagen is not None)
        b.clicked.connect(self.start)
        opts.addWidget(b)
        lay.addLayout(opts)

        frow = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search file, artist, title, album or issue…")
        self.search.setClearButtonEnabled(True)
        self.show_combo = QComboBox()
        self.show_combo.addItems(SHOW)
        self.summary = QLabel("")
        frow.addWidget(self.search, 1)
        frow.addWidget(self.show_combo)
        frow.addWidget(self.summary)
        lay.addLayout(frow)

        self.model = RecordModel(COLUMNS)
        self.proxy = RecordFilter(self.model, self)
        self.proxy.extra = self._show_filter
        self.table = make_table_view(self.proxy, COLUMNS)
        self.table.customContextMenuRequested.connect(self.show_menu)
        self.table.doubleClicked.connect(
            lambda i: self.safe_open(Path(self.model.rows[self.proxy.mapToSource(i).row()]["path"])))
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.search.textChanged.connect(lambda _: self.update_summary())
        self.show_combo.currentIndexChanged.connect(lambda _: (self.proxy.refilter(), self.update_summary()))
        lay.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        bottom.addStretch()
        for text, slot in (("Export CSV…", self.export_csv), ("Save report…", self.export_txt)):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            bottom.addWidget(btn)
        self.add_open_last_buttons(bottom)
        lay.addLayout(bottom)
        QShortcut(QKeySequence("F5"), self, activated=self.start,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

    def _show_filter(self, r: dict) -> bool:
        i = self.show_combo.currentIndex()
        return (i == 0 or (i == 1 and bool(r["problems"])) or (i == 2 and r["cover"] == "None")
                or (i == 3 and any(p.startswith("No ") and p != "No cover art" for p in r["problems"]))
                or (i == 4 and any(p.startswith("Duplicate") for p in r["problems"])))

    def update_summary(self):
        rows = self.model.rows
        text = (f"{len(rows)} songs,  {sum(1 for r in rows if r['problems'])} with problems,  "
                f"{sum(1 for r in rows if r['cover'] == 'None')} without cover")
        if self.proxy.rowCount() != len(rows):
            text += f",  {self.proxy.rowCount()} shown"
        self.summary.setText(text)

    def folder_dropped(self, path: Path):
        self.folder.set_text(str(path))
        self.start()

    def start(self):
        if self.busy or mutagen is None:
            return
        root = self.folder.path()
        if not root:
            QMessageBox.warning(self, "No folder", "Choose a folder first.")
            return
        self.settings["extensions"] = parse_extensions(self.ext_edit.text()) or DEFAULT_SETTINGS["extensions"]
        self.settings["recursive"] = self.recursive_chk.isChecked()
        self.settings["folder_image_ok"] = self.image_chk.isChecked()
        save_tool_settings(SETTINGS_KEY, self.settings)

        def finished(rows):
            self.scanned_root = root
            self.settings["recent_folders"] = remember_recent(self.settings["recent_folders"], root)
            save_tool_settings(SETTINGS_KEY, self.settings)
            self.folder.set_recent(self.settings["recent_folders"])
            self.folder.set_text(str(root))
            self.model.set_rows(rows)
            self.update_summary()
            self.write_log(f"Checked {len(rows)} song(s) in {root}: "
                           f"{sum(1 for r in rows if r['problems'])} with problems.")

        self.run_task(scan_music, (root, dict(self.settings)), finished, "Reading tags…")

    def export_csv(self):
        rows = self.proxy.visible_rows()
        path = rows and self.ask_save("Export CSV", "music_check.csv", "CSV files (*.csv)")
        if path:
            self.save_in_background(export_csv, (path, COLUMNS + [Column("path", "Full path")], rows,
                                                 [["Folder checked", str(self.scanned_root or "")]]), path)

    def export_txt(self):
        path = self.model.rows and self.ask_save("Save report", "music_check.txt", "Text files (*.txt)")
        if path:
            self.save_in_background(save_text, (path, text_report, list(self.model.rows),
                                                str(self.scanned_root or "")), path, "the report")

    def show_menu(self, pos):
        rows = selected_records(self.table, self.proxy)
        if not rows:
            return
        first = Path(rows[0]["path"])
        m = QMenu(self)
        m.addAction("Play / open", lambda: self.safe_open(first))
        m.addAction("Show in folder", lambda: self.safe_open(first, reveal=True))
        if rows[0]["key"]:
            m.addAction("Show all copies of this song",
                        lambda: (self.show_combo.setCurrentIndex(0), self.search.setText(rows[0]["title"])))
        m.addAction("Copy path" + ("s" if len(rows) > 1 else ""),
                    lambda: QApplication.clipboard().setText("\n".join(r["path"] for r in rows)))
        self.add_link_actions(m, first.parent)
        m.exec(self.table.viewport().mapToGlobal(pos))


if __name__ == "__main__":
    run_standalone(MusicCheckTab)
