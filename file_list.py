#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File List - lists every folder and file under a directory, with per-folder
and per-type counts, and saves the list as TXT and CSV.

The TXT export keeps the original script's layout (one block per folder,
subfolders first, then files sorted by extension and name, with a count at
the end of each folder and a grand total). On top of that the tab adds size
and date columns, a search box, and a per-type summary you can click to filter.

Part of Media Toolbox; can also be run on its own:  python file_list.py
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMenu, QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
)

from common import (
    DIM_COLOR, Batcher, Column, FolderPicker, RecordFilter, RecordModel, ToolTab, export_csv,
    human_size, load_tool_settings, make_table_view, remember_recent, run_standalone,
    save_tool_settings, selected_records,
)

SETTINGS_KEY = "file_list"

DEFAULT_SETTINGS = {
    "recent_folders": [r"D:\Temporal\Seguidos", r"E:\Videos\Anime\Incompleto"],
    "recursive": True,       # The original used os.walk, so it always went into subfolders.
    "show_folders": True,    # The original listed subfolders too.
}

FOLDER_TYPE = "Folder"
NO_EXTENSION = "(no extension)"


# =============================================================== core logic (no Qt here)

def list_tree(root: Path, recursive: bool, progress=None, cancelled=None, emit=None) -> dict:
    """Walk root and stream one row per folder and file.

    Order matches the original: per folder, subfolders first (by name), then
    files sorted by extension and then name.
    """
    batch = Batcher(emit, size=200)
    files = folders = size_total = 0
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
            folders += 1
        names.sort(key=lambda n: (os.path.splitext(n)[1].lower(), n.lower()))
        for n in names:
            row = _row(cur / n, rel, n, is_dir=False)
            size_total += max(row["size"], 0)
            batch.add(row)
            files += 1
    batch.flush()
    return {"files": files, "folders": folders, "size": size_total,
            "cancelled": bool(cancelled and cancelled())}


def _rel(p: Path, root: Path) -> str:
    try:
        r = str(p.relative_to(root))
        return "." if r == "." else r
    except ValueError:
        return str(p)


def _row(path: Path, rel_folder: str, name: str, is_dir: bool) -> dict:
    try:
        st = path.stat()
        size = -1 if is_dir else st.st_size
        mtime = st.st_mtime
    except OSError:
        size, mtime = -1, 0
    ext = os.path.splitext(name)[1]
    return {
        "path": str(path), "parent": str(path.parent), "folder": rel_folder, "name": name,
        "type": FOLDER_TYPE if is_dir else (ext.lower() if ext else NO_EXTENSION),
        "is_dir": is_dir,
        "size": size, "size_text": human_size(size) if size >= 0 else "",
        "mtime": mtime,
        "modified": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
        "_color": DIM_COLOR if is_dir else None, "_tip": str(path),
    }


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


def text_export(rows: list[dict], root: str) -> str:
    """The original TXT layout: a block per folder, a count per folder, a grand total."""
    out = [f"Listing of: {root}"]
    current, count, total = None, 0, 0
    for r in rows:
        if r["parent"] != current:
            if current is not None:
                out.append(f"  📊 Files in this folder: {count}")
            out.append("")
            out.append(f"📂 Folder: {r['parent']}")
            current, count = r["parent"], 0
        if r["is_dir"]:
            out.append(f"  📁 {r['name']}/")
        else:
            out.append(f"  📄 {r['name']} ({r['type'] if r['type'] != NO_EXTENSION else 'No extension'})")
            count += 1
            total += 1
    if current is not None:
        out.append(f"  📊 Files in this folder: {count}")
    out += ["", f"Total files in all folders: {total}"]
    return "\n".join(out) + "\n"


# =============================================================== the tab

COLUMNS = [
    Column("folder", "Folder", width=260),
    Column("name", "Name", stretch=True),
    Column("type", "Type"),
    Column("size_text", "Size", sort_key="size", numeric=True),
    Column("modified", "Modified", sort_key="mtime", numeric=True),
]
# CSV keeps the original's three columns first (Path, Name, Type), then the extras.
CSV_COLUMNS = [Column("parent", "Path"), Column("name", "Name"), Column("type", "Type"),
               Column("size", "Size (bytes)"), Column("modified", "Modified")]


class FileListTab(ToolTab):
    title = "File List"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = load_tool_settings(SETTINGS_KEY, DEFAULT_SETTINGS)
        self.scanned_root: Path | None = None
        self.type_filter: str | None = None
        lay = self.body_layout

        self.folder = FolderPicker("Folder:", self.settings["recent_folders"], "Folder to list (or drop it here)")
        self.folder.chosen.connect(lambda _: self.start())
        lay.addWidget(self.folder)

        opts = QHBoxLayout()
        self.recursive_chk = QCheckBox("Include subfolders")
        self.recursive_chk.setChecked(self.settings.get("recursive", True))
        self.folders_chk = QCheckBox("Show folders in the list")
        self.folders_chk.setChecked(self.settings.get("show_folders", True))
        opts.addWidget(self.recursive_chk)
        opts.addWidget(self.folders_chk)
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
        frow.addWidget(self.search, 1)
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
        lay.addLayout(bottom)

        QShortcut(QKeySequence("F5"), self, activated=self.start,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

    # ---------------------------------------------------------- filters / summary
    def _row_filter(self, row: dict) -> bool:
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
        size = sum(max(r["size"], 0) for r in rows)
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
        if not root or not root.is_dir():
            QMessageBox.warning(self, "Folder not found", f"'{self.folder.text()}' doesn't exist or isn't a folder.")
            return
        self.settings["recursive"] = self.recursive_chk.isChecked()
        self.settings["show_folders"] = self.folders_chk.isChecked()
        self.settings["recent_folders"] = remember_recent(self.settings["recent_folders"], root)
        save_tool_settings(SETTINGS_KEY, self.settings)
        self.folder.set_recent(self.settings["recent_folders"])
        self.folder.set_text(str(root))
        self.scanned_root = root
        self.type_filter = None
        self.model.set_rows([])
        # Sorting while thousands of rows stream in is slow; turn it back on at the end.
        self.table.setSortingEnabled(False)

        def partial(rows):
            self.model.add_rows(rows)

        def finished(result):
            self.table.setSortingEnabled(True)
            self.table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)  # Keep the walk order.
            self._fill_types()
            self.update_summary()
            note = " (cancelled)" if result["cancelled"] else ""
            self.write_log(f"Listed {result['files']} file(s) in {result['folders']} folder(s), "
                           f"{human_size(result['size'])}, in {root}{note}.")

        self.run_task(list_tree, (root, self.settings["recursive"]), finished, "Listing files…", partial)

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
            with open(path, "w", encoding="utf-8") as f:
                f.write(text_export(rows, str(self.scanned_root or "")))
            self.write_log(f"Saved {len(rows)} row(s) to {path}")

    def export_csv(self):
        rows = self.proxy.visible_rows()
        if not rows:
            return
        path = self.ask_save("Export CSV", "file_list.csv", "CSV files (*.csv)")
        if path:
            export_csv(path, CSV_COLUMNS, rows)
            self.write_log(f"Exported {len(rows)} row(s) to {path}")

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
        m.exec(self.table.viewport().mapToGlobal(pos))


if __name__ == "__main__":
    run_standalone(FileListTab)
