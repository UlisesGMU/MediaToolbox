#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File List - lists every folder and file under a directory, with per-folder
and per-type counts, and saves the list as TXT and CSV.

The TXT export keeps the original script's layout (one block per folder,
subfolders first, then files sorted by extension and name, with a count at
the end of each folder and a grand total). On top of that:
    - Sizes for files AND folders (a folder's size counts everything inside
      it), shown friendly ("1.2 GB") and exact in bytes, in the table, the
      TXT and the CSV.
    - Optional checksums to identify files exactly. CRC32 (the default) is
      fast and is the code release groups put in file names ("[80186B58]"),
      so each file is also checked against its name to catch corrupted
      downloads. xxHash (fastest, pip install xxhash), MD5, SHA-1 and
      SHA-256 are also available.
    - Date columns, a search box, and a per-type summary you can click to filter.
    - Find duplicates: identical files anywhere in the list, even with
      different names. Only files that share a size are read, and checksums
      are remembered between runs, so repeat runs are fast.

Part of Media Toolbox; can also be run on its own:  python file_list.py
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMenu, QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
)

from common import (
    DIM_COLOR, Batcher, Column, FolderPicker, RecordFilter, RecordModel, ToolTab, export_csv,
    human_size, load_tool_settings, make_table_view, remember_recent, run_standalone,
    save_tool_settings, selected_records, size_with_bytes, timestamp, created_line,
    ERROR_COLOR, HASH_ALGORITHMS, HASH_NOTES, crc_in_name, file_hash, hash_available,
    save_text, hash_cache, require_folder,
)

SETTINGS_KEY = "file_list"

DEFAULT_SETTINGS = {
    "recent_folders": [r"D:\Temporal\Seguidos", r"E:\Videos\Anime\Incompleto"],
    "recursive": True,       # The original used os.walk, so it always went into subfolders.
    "show_folders": True,    # The original listed subfolders too.
    "hash": False,           # Checksums read every byte of every file: slow on big folders.
    "hash_algorithm": "CRC32",
}

FOLDER_TYPE = "Folder"
NO_EXTENSION = "(no extension)"


# =============================================================== core logic (no Qt here)

def folder_total(folder: Path, cancelled=None) -> tuple[int, int]:
    """(bytes, files) of everything inside a folder, at any depth."""
    size = count = 0
    for current, _dirs, names in os.walk(folder):
        if cancelled and cancelled():
            break
        for n in names:
            try:
                size += os.stat(os.path.join(current, n)).st_size
                count += 1
            except OSError:
                pass
    return size, count


def list_tree(root: Path, recursive: bool, hash_algorithm: str | None = None,
              progress=None, cancelled=None, emit=None) -> dict:
    """Walk root and stream one row per folder and file.

    Order matches the original: per folder, subfolders first (by name), then
    files sorted by extension and then name. Folder rows are sent before their
    contents are read, so their sizes come back separately in "folder_sizes"
    ({folder path: [bytes, files]}, counting everything inside at any depth)
    and are filled in with apply_folder_sizes().
    """
    require_folder(root)
    batch = Batcher(emit, size=200)
    files = folders = size_total = 0
    folder_sizes: dict[str, list[int]] = {str(root): [0, 0]}
    listed_dirs: list[Path] = []
    for current, subdirs, names in os.walk(root):
        if cancelled and cancelled():
            break
        subdirs.sort(key=str.lower)
        if not recursive:
            subdirs_to_list = list(subdirs)
            subdirs[:] = []  # Stop os.walk from going deeper.
        else:
            subdirs_to_list = subdirs
        cur = Path(current)
        rel = _rel(cur, root)
        if progress:
            progress(files, 0, f"{files} files…  {rel}")
        for d in subdirs_to_list:
            batch.add(_row(cur / d, rel, d, is_dir=True))
            listed_dirs.append(cur / d)
            folders += 1
        names.sort(key=lambda n: (os.path.splitext(n)[1].lower(), n.lower()))
        for n in names:
            if cancelled and cancelled():
                break
            path = cur / n
            row = _row(path, rel, n, is_dir=False)
            if hash_algorithm and row["size"] >= 0:
                if progress:
                    progress(files, 0, f"{files} files…  hashing {n}")
                row["hash"] = file_hash(path, hash_algorithm, cancelled)
                if hash_algorithm == "CRC32":
                    check_crc(row)
            size = max(row["size"], 0)
            size_total += size
            # Add the file to its folder and every folder above it, up to root.
            p = cur
            while True:
                entry = folder_sizes.setdefault(str(p), [0, 0])
                entry[0] += size
                entry[1] += 1
                if p == root or p.parent == p:
                    break
                p = p.parent
            batch.add(row)
            files += 1
    batch.flush()
    if hash_algorithm:
        hash_cache().save()

    # Without subfolders, the listed folders were never walked: measure them now.
    if not recursive:
        for n, d in enumerate(listed_dirs):
            if cancelled and cancelled():
                break
            if progress:
                progress(n, len(listed_dirs), f"Measuring {d.name}")
            b, c = folder_total(d, cancelled)
            folder_sizes[str(d)] = [b, c]
            folder_sizes[str(root)][0] += b
            folder_sizes[str(root)][1] += c

    return {"files": files, "folders": folders, "size": size_total, "folder_sizes": folder_sizes,
            "hash": hash_algorithm or "", "cancelled": bool(cancelled and cancelled())}


def check_crc(row: dict) -> None:
    """Compare a file's CRC32 with the one in its name, e.g. "[80186B58]".
    A mismatch usually means a broken or incomplete download."""
    expected = crc_in_name(row["name"])
    if not expected or not row["hash"]:
        return  # No code in the name (or the file couldn't be read): nothing to compare.
    if row["hash"] == expected:
        row["crc_check"] = "OK"
    else:
        row["crc_check"] = f"Mismatch (name says {expected})"
        row["_color"] = ERROR_COLOR


def find_duplicates(files: list[tuple[str, int, str]], algorithm: str,
                    progress=None, cancelled=None) -> dict:
    """Identical files, even with different names.

    files: [(path, size in bytes, checksum already known or "")].
    Only files that share their size with another file can be identical, so
    only those are read (and unchanged files come from the checksum cache).
    Returns {"groups": {path: (group number, copies)}, "hashes": {path: checksum},
             "count": number of groups, "wasted": bytes used by the extra copies}.
    """
    by_size: dict[int, list[tuple[str, str]]] = {}
    for path, size, known in files:
        if size > 0:  # Empty files are all "identical"; not useful.
            by_size.setdefault(size, []).append((path, known))
    candidates = [g for g in by_size.values() if len(g) > 1]
    total = sum(len(g) for g in candidates)

    hashes: dict[str, str] = {}
    done = 0
    for group in candidates:
        for path, known in group:
            if cancelled and cancelled():
                break
            if progress:
                progress(done, total, Path(path).name)
            hashes[path] = known or file_hash(Path(path), algorithm, cancelled)
            done += 1
    hash_cache().save()

    by_key: dict[tuple[int, str], list[str]] = {}
    for size, group in by_size.items():
        for path, _known in group:
            if hashes.get(path):
                by_key.setdefault((size, hashes[path]), []).append(path)
    groups: dict[str, tuple[int, int]] = {}
    wasted = count = 0
    # Biggest files first: group 1 is where the most space can be recovered.
    for (size, _h), paths in sorted(by_key.items(), key=lambda kv: -kv[0][0]):
        if len(paths) > 1:
            count += 1
            wasted += size * (len(paths) - 1)
            for p in paths:
                groups[p] = (count, len(paths))
    return {"groups": groups, "hashes": hashes, "count": count, "wasted": wasted,
            "cancelled": bool(cancelled and cancelled())}


def apply_folder_sizes(rows: list[dict], folder_sizes: dict[str, list[int]]) -> None:
    """Fill in size columns of folder rows once the walk has measured them."""
    for r in rows:
        if r["is_dir"] and r["path"] in folder_sizes:
            size, count = folder_sizes[r["path"]]
            r["size"], r["items"] = size, count
            _size_fields(r)


def _rel(p: Path, root: Path) -> str:
    try:
        r = str(p.relative_to(root))
        return "." if r == "." else r
    except ValueError:
        return str(p)


def _size_fields(row: dict) -> None:
    size = row["size"]
    row["size_text"] = human_size(size) if size >= 0 else ""       # Friendly: 1.2 GB
    row["bytes_text"] = f"{size:,}" if size >= 0 else ""            # Exact: 1,288,490,188
    row["size_full"] = size_with_bytes(size)                        # Both, for exports


def _row(path: Path, rel_folder: str, name: str, is_dir: bool) -> dict:
    try:
        st = path.stat()
        size = -1 if is_dir else st.st_size  # Folders get their size after the walk.
        mtime = st.st_mtime
    except OSError:
        size, mtime = -1, 0
    ext = os.path.splitext(name)[1]
    row = {
        "path": str(path), "parent": str(path.parent), "folder": rel_folder, "name": name,
        "type": FOLDER_TYPE if is_dir else (ext.lower() if ext else NO_EXTENSION),
        "is_dir": is_dir, "size": size, "items": None, "hash": "", "crc_check": "",
        "mtime": mtime,
        "modified": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
        "_color": DIM_COLOR if is_dir else None, "_tip": str(path),
    }
    _size_fields(row)
    return row


def type_summary(rows: list[dict]) -> list[tuple[str, int, int]]:
    """[(type, count, total bytes)], most common first. Folders are not counted."""
    counts: dict[str, list[int]] = {}
    for r in rows:
        if r["is_dir"]:
            continue
        c = counts.setdefault(r["type"], [0, 0])
        c[0] += 1
        c[1] += max(r["size"], 0)
    return sorted(((t, c[0], c[1]) for t, c in counts.items()), key=lambda x: (-x[1], x[0]))


def _files(n: int) -> str:
    return f"{n} file" if n == 1 else f"{n} files"


def text_export(rows: list[dict], root: str, folder_sizes: dict | None = None, hash_name: str = "") -> str:
    """The original TXT layout: a block per folder, a count per folder, a grand total.
    Every folder and file also shows its size (friendly and in exact bytes)
    and when it was last modified."""
    folder_sizes = folder_sizes or {}
    shown_files = [r for r in rows if not r["is_dir"]]
    shown_size = sum(max(r["size"], 0) for r in shown_files)
    out = [f"Listing of: {root}", created_line()]
    if root in folder_sizes:
        b, c = folder_sizes[root]
        out.append(f"Folder total: {_files(c)}, {size_with_bytes(b)}")

    current, count, block_size, total = None, 0, 0, 0

    def close_block():
        out.append(f"  📊 Files in this folder: {count}  —  {size_with_bytes(block_size)}")

    for r in rows:
        if r["parent"] != current:
            if current is not None:
                close_block()
            out.append("")
            header = f"📂 Folder: {r['parent']}"
            if r["parent"] in folder_sizes:
                b, c = folder_sizes[r["parent"]]
                header += f"  —  {size_with_bytes(b)} in {_files(c)}, including subfolders"
            out.append(header)
            current, count, block_size = r["parent"], 0, 0
        modified = f"  —  modified {r['modified']}" if r["modified"] else ""
        if r["is_dir"]:
            extra = f"  —  {r['size_full']}, {_files(r['items'])}" if r["size"] >= 0 else ""
            out.append(f"  📁 {r['name']}/{extra}{modified}")
        else:
            kind = r["type"] if r["type"] != NO_EXTENSION else "No extension"
            line = f"  📄 {r['name']} ({kind})  —  {r['size_full']}{modified}"
            if r.get("hash"):
                line += f"  [{hash_name or 'hash'}: {r['hash']}]"
                if r.get("crc_check") == "OK":
                    line += " ✓ matches name"
                elif r.get("crc_check"):
                    line += f" ✗ {r['crc_check']}"
            out.append(line)
            count += 1
            block_size += max(r["size"], 0)
            total += 1
    if current is not None:
        close_block()
    out += ["", f"Total files in all folders: {total}  —  {size_with_bytes(shown_size)}"]
    return "\n".join(out) + "\n"


# =============================================================== the tab

COLUMNS = [
    Column("folder", "Folder", width=260),
    Column("name", "Name", stretch=True),
    Column("type", "Type"),
    Column("size_text", "Size", sort_key="size", numeric=True),
    Column("bytes_text", "Bytes", sort_key="size", numeric=True),
    Column("items", "Files inside", numeric=True),
    Column("modified", "Modified", sort_key="mtime", numeric=True),
    Column("hash", "Checksum", width=280),
]
HASH_COL = len(COLUMNS) - 1
COLUMNS.append(Column("crc_check", "Name CRC", width=200))
CRC_COL = len(COLUMNS) - 1
COLUMNS.append(Column("duplicate", "Duplicate", sort_key="dup_group", numeric=True, width=150))
DUP_COL = len(COLUMNS) - 1
# CSV keeps the original's three columns first (Path, Name, Type), then the extras.
# "Size (bytes)" is a plain number, so Excel can sum and sort it.
CSV_COLUMNS = [Column("parent", "Path"), Column("name", "Name"), Column("type", "Type"),
               Column("size", "Size (bytes)"), Column("size_text", "Size"),
               Column("items", "Files inside"), Column("modified", "Modified")]


class FileListTab(ToolTab):
    title = "File List"
    key = "file_list"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = load_tool_settings(SETTINGS_KEY, DEFAULT_SETTINGS)
        self.scanned_root: Path | None = None
        self.type_filter: str | None = None
        self.folder_sizes: dict = {}
        self.hash_used = ""
        lay = self.body_layout

        self.folder = FolderPicker("Folder:", self.settings["recent_folders"], "Folder to list (or drop it here)")
        self.folder.chosen.connect(lambda _: self.start())
        lay.addWidget(self.folder)

        opts = QHBoxLayout()
        self.recursive_chk = QCheckBox("Include subfolders")
        self.recursive_chk.setChecked(self.settings.get("recursive", True))
        self.folders_chk = QCheckBox("Show folders in the list")
        self.folders_chk.setChecked(self.settings.get("show_folders", True))
        self.hash_chk = QCheckBox("Checksums:")
        self.hash_chk.setToolTip("Reads every byte of every file. CRC32 and xxHash are the fast ones;\n"
                                 "on a regular hard drive the drive's speed is the limit for all of them.")
        self.hash_chk.setChecked(self.settings.get("hash", False))
        self.hash_combo = QComboBox()
        for i, name in enumerate(HASH_ALGORITHMS):
            self.hash_combo.addItem(name)
            self.hash_combo.setItemData(i, HASH_NOTES[name], Qt.ItemDataRole.ToolTipRole)
            if not hash_available(name):
                # Listed but greyed out, so it's clear it exists and what it needs.
                self.hash_combo.model().item(i).setEnabled(False)
                self.hash_combo.setItemText(i, f"{name} (pip install xxhash)")
        chosen = self.settings.get("hash_algorithm", "CRC32")
        self.hash_combo.setCurrentText(chosen if hash_available(chosen) else "CRC32")
        self.hash_combo.setEnabled(self.hash_chk.isChecked())
        self.hash_chk.toggled.connect(self.hash_combo.setEnabled)
        opts.addWidget(self.recursive_chk)
        opts.addWidget(self.folders_chk)
        opts.addWidget(self.hash_chk)
        opts.addWidget(self.hash_combo)
        opts.addStretch()
        self.b_start = QPushButton("List files")
        self.b_start.setDefault(True)
        self.b_start.clicked.connect(self.start)
        opts.addWidget(self.b_start)
        lay.addLayout(opts)

        frow = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search names, folders or types…")
        self.search.setClearButtonEnabled(True)
        self.summary = QLabel("")
        self.show_combo = QComboBox()
        self.show_combo.addItems(["All files", "Duplicates only"])
        self.show_combo.setEnabled(False)  # Until "Find duplicates" has run.
        self.b_dups = QPushButton("Find duplicates")
        self.b_dups.setToolTip("Identical files, even with different names. Only files with the same\n"
                               "size are read, using the checksum type chosen above.")
        self.b_dups.clicked.connect(self.find_dups)
        frow.addWidget(self.search, 1)
        frow.addWidget(self.show_combo)
        frow.addWidget(self.b_dups)
        frow.addWidget(self.summary)
        lay.addLayout(frow)

        # --- type summary (left) and file table (right)
        self.types = QTableWidget(0, 3)
        self.types.setHorizontalHeaderLabels(["Type", "Files", "Size"])
        self.types.verticalHeader().setVisible(False)
        self.types.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.types.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.types.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        th = self.types.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        th.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        th.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.types.setToolTip("Click a type to show only those files")
        self.types.itemSelectionChanged.connect(self._type_clicked)

        self.model = RecordModel(COLUMNS)
        self.proxy = RecordFilter(self.model, self)
        self.proxy.extra = self._row_filter
        self.table = make_table_view(self.proxy, COLUMNS)
        self.table.setColumnHidden(HASH_COL, True)  # Shown only after a run with checksums.
        self.table.setColumnHidden(CRC_COL, True)   # Shown only after a CRC32 run.
        self.table.setColumnHidden(DUP_COL, True)   # Shown after "Find duplicates".
        self.show_combo.currentIndexChanged.connect(lambda _: (self.proxy.refilter(), self.update_summary()))
        self.table.customContextMenuRequested.connect(self.show_menu)
        self.table.doubleClicked.connect(
            lambda i: self.safe_open(Path(self.model.rows[self.proxy.mapToSource(i).row()]["path"])))
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.search.textChanged.connect(lambda _: self.update_summary())
        self.folders_chk.toggled.connect(lambda _: (self.proxy.refilter(), self.update_summary()))

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.types)
        split.addWidget(self.table)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 4)
        split.setSizes([220, 880])
        lay.addWidget(split, 1)

        bottom = QHBoxLayout()
        b_txt = QPushButton("Save TXT…")
        b_txt.setToolTip("The original layout: one block per folder with counts")
        b_txt.clicked.connect(self.export_txt)
        b_csv = QPushButton("Export CSV…")
        b_csv.clicked.connect(self.export_csv)
        note = QLabel("Exports include only what's shown (search and type filter apply).")
        note.setStyleSheet(f"color: {DIM_COLOR};")
        bottom.addWidget(note)
        bottom.addStretch()
        bottom.addWidget(b_txt)
        bottom.addWidget(b_csv)
        self.add_open_last_buttons(bottom)
        lay.addLayout(bottom)

        QShortcut(QKeySequence("F5"), self, activated=self.start,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

    # ---------------------------------------------------------- filters / summary
    def on_idle(self):
        # Sorting is paused while rows stream in; make sure it's back on even
        # when a listing fails halfway (then "finished" never runs).
        if not self.table.isSortingEnabled():
            self.table.setSortingEnabled(True)

    def _row_filter(self, row: dict) -> bool:
        if self.show_combo.currentIndex() == 1:
            return bool(row.get("duplicate"))  # Duplicates only (never folders).
        if row["is_dir"]:
            # Folders show only with the checkbox on and no type selected.
            return self.folders_chk.isChecked() and self.type_filter is None
        return self.type_filter is None or row["type"] == self.type_filter

    def _type_clicked(self):
        items = self.types.selectedItems()
        t = items[0].data(Qt.ItemDataRole.UserRole) if items else None
        self.type_filter = t  # None = the "All types" row.
        self.proxy.refilter()
        self.update_summary()

    def _fill_types(self):
        rows = self.model.rows
        summary = type_summary(rows)
        total_files = sum(c for _t, c, _s in summary)
        total_size = sum(s for _t, _c, s in summary)
        self.types.blockSignals(True)
        self.types.setSortingEnabled(False)
        self.types.setRowCount(0)
        for t, count, size in [("All types", total_files, total_size)] + summary:
            r = self.types.rowCount()
            self.types.insertRow(r)
            name = QTableWidgetItem(t)
            name.setData(Qt.ItemDataRole.UserRole, None if r == 0 else t)
            c = QTableWidgetItem(str(count))
            c.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            s = QTableWidgetItem(human_size(size))
            s.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            for col, item in enumerate((name, c, s)):
                self.types.setItem(r, col, item)
        self.types.blockSignals(False)
        # Keep the current type selected if it still exists.
        for r in range(self.types.rowCount()):
            if self.types.item(r, 0).data(Qt.ItemDataRole.UserRole) == self.type_filter:
                self.types.selectRow(r)
                break

    def update_summary(self):
        rows = self.model.rows
        files = sum(1 for r in rows if not r["is_dir"])
        folders = len(rows) - files
        # Files only: folder sizes already include their files and would count them twice.
        size = sum(max(r["size"], 0) for r in rows if not r["is_dir"])
        text = f"{files} files,  {folders} folders,  {human_size(size)}"
        shown = sum(1 for r in self.proxy.visible_rows() if not r["is_dir"]) if self.proxy.rowCount() != len(rows) else None
        if shown is not None:
            text += f",  {shown} files shown"
        self.summary.setText(text)

    def folder_dropped(self, path: Path):
        self.folder.set_text(str(path))
        self.start()

    # ---------------------------------------------------------- actions
    def start(self):
        if self.busy:
            return
        root = self.folder.path()
        if not root:
            QMessageBox.warning(self, "No folder", "Choose a folder first.")
            return
        self.settings["recursive"] = self.recursive_chk.isChecked()
        self.settings["show_folders"] = self.folders_chk.isChecked()
        self.settings["hash"] = self.hash_chk.isChecked()
        self.settings["hash_algorithm"] = self.hash_combo.currentText()
        save_tool_settings(SETTINGS_KEY, self.settings)
        self.scanned_root = root  # Whether it exists is checked in the background task.
        self.type_filter = None
        self.show_combo.setCurrentIndex(0)
        self.show_combo.setEnabled(False)
        self.table.setColumnHidden(DUP_COL, True)
        self.model.set_rows([])
        # Sorting while thousands of rows stream in is slow; turn it back on at the end.
        self.table.setSortingEnabled(False)

        def partial(rows):
            self.model.add_rows(rows)

        def finished(result):
            self.settings["recent_folders"] = remember_recent(self.settings["recent_folders"], root)
            save_tool_settings(SETTINGS_KEY, self.settings)
            self.folder.set_recent(self.settings["recent_folders"])
            self.folder.set_text(str(root))
            # Folder sizes are only known once everything inside them was read.
            self.folder_sizes = result["folder_sizes"]
            apply_folder_sizes(self.model.rows, self.folder_sizes)
            self.model.refresh_all()
            self.hash_used = result["hash"]
            self.table.setColumnHidden(HASH_COL, not self.hash_used)
            self.table.setColumnHidden(CRC_COL, self.hash_used != "CRC32")
            if self.hash_used:
                self.model.columns[HASH_COL].header = self.hash_used
                self.model.headerDataChanged.emit(Qt.Orientation.Horizontal, HASH_COL, HASH_COL)
            self.table.setSortingEnabled(True)
            self.table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)  # Keep the walk order.
            self._fill_types()
            self.update_summary()
            note = " (cancelled)" if result["cancelled"] else ""
            self.write_log(f"Listed {result['files']} file(s) in {result['folders']} folder(s), "
                           f"{size_with_bytes(result['size'])}, in {root}{note}.")
            if self.hash_used == "CRC32":
                checked = [r for r in self.model.rows if r.get("crc_check")]
                bad = [r for r in checked if r["crc_check"] != "OK"]
                self.write_log(f"CRC32 checked against the file name for {len(checked)} file(s): "
                               f"{len(checked) - len(bad)} OK, {len(bad)} mismatched.")
                for r in bad:
                    self.write_log(f"  MISMATCH: {r['path']}  ({r['crc_check']})")

        hash_alg = self.settings["hash_algorithm"] if self.settings["hash"] else None
        self.table.setColumnHidden(HASH_COL, True)
        self.table.setColumnHidden(CRC_COL, True)
        self.run_task(list_tree, (root, self.settings["recursive"], hash_alg), finished,
                      "Listing files…", partial)

    def find_dups(self):
        if self.busy:
            return
        files = [r for r in self.model.rows if not r["is_dir"]]
        if not files:
            QMessageBox.information(self, "Find duplicates", "List a folder first.")
            return
        algorithm = self.hash_combo.currentText().split(" (")[0]
        # Reuse checksums from the listing when they're of the same type.
        same = self.hash_used == algorithm
        snapshot = [(r["path"], max(r["size"], 0), r["hash"] if same else "") for r in files]

        def finished(result):
            groups = result["groups"]
            for r in files:
                g = groups.get(r["path"])
                r["dup_group"] = g[0] if g else -1
                r["duplicate"] = f"Group {g[0]} ({g[1]} copies)" if g else ""
                if not r["hash"] and (same or not self.hash_used) and r["path"] in result["hashes"]:
                    r["hash"] = result["hashes"][r["path"]]
            if not self.hash_used and result["hashes"]:
                self.hash_used = algorithm
                self.model.columns[HASH_COL].header = algorithm
                self.model.headerDataChanged.emit(Qt.Orientation.Horizontal, HASH_COL, HASH_COL)
                self.table.setColumnHidden(HASH_COL, False)
            self.model.refresh_all()
            self.table.setColumnHidden(DUP_COL, False)
            self.show_combo.setEnabled(True)
            if result["count"]:
                self.show_combo.setCurrentIndex(1)
            self.proxy.refilter()
            self.update_summary()
            note = " (cancelled, so some may be missing)" if result["cancelled"] else ""
            self.write_log(f"Found {result['count']} group(s) of identical files{note}; the extra copies use "
                           f"{size_with_bytes(result['wasted'])}.")

        self.run_task(find_duplicates, (snapshot, algorithm), finished, "Looking for duplicates…")

    def _rows_in_walk_order(self) -> list[dict]:
        """Visible rows in the original order (folder, then extension, then name),
        whatever column the table is currently sorted by."""
        order = {id(r): i for i, r in enumerate(self.model.rows)}
        return sorted(self.proxy.visible_rows(), key=lambda r: order[id(r)])

    def export_txt(self):
        rows = self._rows_in_walk_order()
        if not rows:
            return
        path = self.ask_save("Save TXT", "file_list.txt", "Text files (*.txt)")
        if path:
            self.save_in_background(save_text, (path, text_export, rows, str(self.scanned_root or ""),
                                                self.folder_sizes, self.hash_used), path, f"{len(rows)} row(s)")

    def export_csv(self):
        rows = self.proxy.visible_rows()
        if not rows:
            return
        path = self.ask_save("Export CSV", "file_list.csv", "CSV files (*.csv)")
        if path:
            cols = CSV_COLUMNS + ([Column("hash", self.hash_used)] if self.hash_used else [])
            if self.hash_used == "CRC32":
                cols.append(Column("crc_check", "CRC32 vs name"))
            if not self.table.isColumnHidden(DUP_COL):
                cols.append(Column("duplicate", "Duplicate"))
            files = [r for r in rows if not r["is_dir"]]
            size = sum(max(r["size"], 0) for r in files)
            # Totals at the end, like the original's "Total de archivos" row.
            footer = [
                ["Listing of", str(self.scanned_root or "")],
                ["Total files", len(files)],
                ["Total size (bytes)", size],
                ["Total size", human_size(size)],
            ]
            self.save_in_background(export_csv, (path, cols, rows, footer), path, f"{len(rows)} row(s)")

    def show_menu(self, pos):
        rows = selected_records(self.table, self.proxy)
        if not rows:
            return
        first = Path(rows[0]["path"])
        m = QMenu(self)
        m.addAction("Open", lambda: self.safe_open(first))
        m.addAction("Show in folder", lambda: self.safe_open(first, reveal=True))
        m.addSeparator()
        m.addAction("Copy name" + ("s" if len(rows) > 1 else ""),
                    lambda: QApplication.clipboard().setText("\n".join(r["name"] for r in rows)))
        m.addAction("Copy full path" + ("s" if len(rows) > 1 else ""),
                    lambda: QApplication.clipboard().setText("\n".join(r["path"] for r in rows)))
        a = m.addAction("Copy checksum" + ("s" if len(rows) > 1 else ""),
                        lambda: QApplication.clipboard().setText(
                            "\n".join(f"{r['hash']}  {r['name']}" for r in rows if r.get("hash"))))
        a.setEnabled(any(r.get("hash") for r in rows))
        if rows[0].get("duplicate"):
            group = rows[0]["duplicate"].split(" (")[0] + " ("  # "Group 3 (" won't match "Group 30".
            m.addAction("Show only this duplicate group", lambda: self.search.setText(group))
        folder = first if rows[0]["is_dir"] else first.parent
        self.add_link_actions(m, folder)
        m.exec(self.table.viewport().mapToGlobal(pos))


if __name__ == "__main__":
    run_standalone(FileListTab)
