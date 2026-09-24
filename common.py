#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared pieces for the Media Toolbox tabs.

    - Settings and history files (one JSON file per tool).
    - File moves that record everything they do, so any run can be reverted.
    - A background worker with progress, Cancel and streamed partial results.
    - A generic sortable/searchable table for rows stored as dicts.
    - ToolTab, the base class every tab builds on: it provides the log,
      the progress bar, the Cancel button and drag & drop of folders.

Requires:  pip install PySide6
"""
from __future__ import annotations

import copy
import csv
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QObject, QSortFilterProxyModel, Qt, QThread, Signal, Slot,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSizePolicy, QSplitter, QTableView, QVBoxLayout, QWidget,
)

APP_NAME = "Media Toolbox"

# Text colors used by every tab, chosen to stay readable on light and dark themes.
OK_COLOR = "#2e9b4f"
WARN_COLOR = "#c98a00"
ERROR_COLOR = "#d03b3b"
DIM_COLOR = "#8a8a8a"


# =============================================================== settings / storage

def _config_base() -> Path:
    """Per-user settings root: %APPDATA% on Windows, ~/.config on Linux."""
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", Path.home()))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def config_dir() -> Path:
    d = _config_base() / "MediaToolbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def legacy_config_path(app_folder: str, filename: str) -> Path:
    """Where an older standalone version kept a file (used once, to import it)."""
    return _config_base() / app_folder / filename


def load_json(path: Path, fallback):
    """Read a JSON file, returning `fallback` if it's missing or corrupt."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def save_json(path: Path, data) -> None:
    """Write to a temp file first, then swap it in, so a crash can't leave half a file."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_tool_settings(key: str, defaults: dict, legacy: Path | None = None) -> dict:
    """Defaults, overlaid with the saved file (or an older version's file, the first time)."""
    s = copy.deepcopy(defaults)
    saved = load_json(config_dir() / f"{key}.json", None)
    if saved is None and legacy is not None:
        saved = load_json(legacy, None)
    if isinstance(saved, dict):
        s.update(saved)
    return s


def save_tool_settings(key: str, settings: dict) -> None:
    save_json(config_dir() / f"{key}.json", settings)


def remember_recent(recent: list[str], path: Path, limit: int = 15) -> list[str]:
    """Move `path` to the front of a recent-folders list."""
    rest = [p for p in recent if Path(p) != path]
    return [str(path)] + rest[: limit - 1]


# =============================================================== file name helpers

# Characters Windows doesn't allow in file or folder names.
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_component(name: str) -> str:
    """Make one file or folder name safe. Windows also rejects trailing dots and spaces."""
    return INVALID_CHARS.sub("", name).strip().rstrip(".").strip()


def unique_path(p: Path) -> Path:
    """'file.mkv' -> 'file (1).mkv', 'file (2).mkv', ... until the name is free."""
    if not p.exists():
        return p
    i = 1
    while True:
        candidate = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def parse_extensions(text: str) -> list[str]:
    """'mkv, .MP4 avi' -> ['.mkv', '.mp4', '.avi']"""
    out = []
    for e in re.split(r"[,\s;]+", text):
        e = e.strip().lower()
        if e:
            out.append(e if e.startswith(".") else "." + e)
    return out


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return ""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def compact_ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 5, 7, 8] -> '1-3, 5, 7-8'"""
    nums = sorted(set(numbers))
    parts, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(parts)


# =============================================================== moves with undo

def _make_dirs(folder: Path, created: list[str]) -> None:
    """mkdir -p, remembering every folder that didn't exist so a revert can remove it."""
    missing, d = [], folder
    while not d.exists():
        missing.append(d)
        d = d.parent
    folder.mkdir(parents=True, exist_ok=True)
    created.extend(str(x) for x in reversed(missing))


def move_files(jobs: list[tuple[Path, Path]], on_exists=None, label: str = "",
               root: str = "", output: str = "", progress=None, cancelled=None):
    """Move files and record every move for the history.

    jobs:       [(source path, destination path)], both absolute.
    on_exists:  called when the destination is taken; returns (new path, status)
                to put the file somewhere else, or None to skip it.
    progress / cancelled: injected by TaskWorker.
    Returns (batch for the history or None, {source path as str: (status, new path or None)}).
    """
    moves, created, results = [], [], {}
    total = len(jobs)
    for n, (src, dst) in enumerate(jobs):
        if cancelled and cancelled():
            for s, _ in jobs[n:]:
                results[str(s)] = ("Cancelled", None)
            break
        if progress:
            progress(n, total, src.name)
        if not src.is_file():
            results[str(src)] = ("Missing", None)
            continue
        try:
            status = "Moved"
            if dst.exists():
                if dst.resolve() == src.resolve():
                    results[str(src)] = ("Already in place", None)
                    continue
                alt = on_exists(dst) if on_exists else None
                if alt is None:
                    results[str(src)] = ("Exists - skipped", None)
                    continue
                dst, status = alt
            _make_dirs(dst.parent, created)
            # Same drive: an instant rename. Different drive: copy + delete (slow for big files).
            shutil.move(str(src), str(dst))
            moves.append({"src": str(src), "dst": str(dst)})
            results[str(src)] = (status, dst)
        except OSError as e:
            results[str(src)] = (f"Error: {e}", None)
    if progress:
        progress(total, total, "")

    batch = None
    if moves:
        now = datetime.now()
        batch = {
            "id": now.strftime("%Y%m%d-%H%M%S-%f"),
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "label": label,
            "root": root,
            "output": output or root,
            "moves": moves,           # Absolute paths, so reverting works from anywhere.
            "created_dirs": created,
        }
    return batch, results


def revert_batch(batch: dict, progress=None, cancelled=None):
    """Move files back to where they came from and remove folders we created if empty.

    Returns (files reverted, list of error messages, moves that could not be reverted).
    """
    ok, errors, remaining = 0, [], []
    moves = list(reversed(batch.get("moves", [])))
    total = len(moves)
    for n, mv in enumerate(moves):
        if cancelled and cancelled():
            remaining.extend(moves[n:])  # Not attempted yet: keep them for later.
            errors.append("Cancelled before finishing.")
            break
        src, dst = Path(mv["src"]), Path(mv["dst"])
        if progress:
            progress(n, total, dst.name)
        if not dst.exists():
            # Moved or deleted by hand since then; nothing we can do, so drop it.
            errors.append(f"Not found anymore (skipped): {dst}")
        elif src.exists():
            # Something new took the original name; keep it in the history to retry later.
            errors.append(f"Original path is taken: {src}")
            remaining.append(mv)
        else:
            try:
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                ok += 1
            except OSError as e:
                errors.append(f"{dst}: {e}")
                remaining.append(mv)
    # Deepest folders first, and only if empty: never delete anything the user added.
    for d in sorted(batch.get("created_dirs", []), key=len, reverse=True):
        p = Path(d)
        try:
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass
    if progress:
        progress(total, total, "")
    remaining.reverse()  # Back to original order.
    return ok, errors, remaining


class HistoryStore:
    """The list of past runs for one tool, kept in <key>_history.json."""

    def __init__(self, key: str, limit: int = 50, legacy: Path | None = None):
        self.path = config_dir() / f"{key}_history.json"
        self.limit = limit
        data = load_json(self.path, None)
        if data is None and legacy is not None:
            data = load_json(legacy, None)
        self.batches: list[dict] = data if isinstance(data, list) else []

    def save(self) -> None:
        self.batches = self.batches[-self.limit:]  # Oldest runs are dropped first.
        save_json(self.path, self.batches)

    def add(self, batch: dict) -> None:
        self.batches.append(batch)
        self.save()

    def remove(self, batch_id: str) -> None:
        self.batches = [b for b in self.batches if b["id"] != batch_id]
        self.save()

    def last(self) -> dict | None:
        return self.batches[-1] if self.batches else None


# =============================================================== OS helpers

def open_path(p: Path) -> None:
    """Open a file or folder with the system's default app."""
    p = str(p)
    if sys.platform.startswith("win"):
        os.startfile(p)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def reveal_in_folder(p: Path) -> None:
    """Open the containing folder, selecting the file where the OS supports it."""
    if sys.platform.startswith("win"):
        # Passed as one string: Explorer mishandles /select when Python quotes the argument.
        subprocess.Popen(f'explorer /select,"{p}"')
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)])
    else:
        # Linux file managers don't share a common "select this file" option.
        open_path(p.parent if p.is_file() else p)


# =============================================================== background worker

class Batcher:
    """Groups streamed results so the GUI gets one signal per chunk instead of
    one per file (thousands of tiny signals would make the window stutter)."""

    def __init__(self, emit, size: int = 50, interval: float = 0.3):
        self.emit, self.size, self.interval = emit, size, interval
        self.buf: list = []
        self.last = time.monotonic()

    def add(self, item) -> None:
        self.buf.append(item)
        if len(self.buf) >= self.size or time.monotonic() - self.last > self.interval:
            self.flush()

    def flush(self) -> None:
        if self.buf and self.emit:
            self.emit(self.buf)
        self.buf = []
        self.last = time.monotonic()


class TaskWorker(QObject):
    """Runs a slow function off the GUI thread.

    The function receives these keyword arguments, but only if it declares them:
        progress(done, total, text)   total 0 = unknown (shows a busy bar)
        cancelled() -> bool           True once the user pressed Cancel
        emit(list_of_results)         stream partial results to the tab
    """
    progress = Signal(int, int, str)
    partial = Signal(object)
    done = Signal(object)

    def __init__(self, fn, args: tuple):
        super().__init__()
        self.fn, self.args = fn, args
        self._cancel = False

    def cancel(self) -> None:
        # Called from the GUI thread; a plain bool flag is safe to read from the worker.
        self._cancel = True

    @Slot()
    def run(self):
        params = inspect.signature(self.fn).parameters
        kwargs = {}
        if "progress" in params:
            kwargs["progress"] = self.progress.emit
        if "cancelled" in params:
            kwargs["cancelled"] = lambda: self._cancel
        if "emit" in params:
            kwargs["emit"] = self.partial.emit
        try:
            result = self.fn(*self.args, **kwargs)
        except Exception as e:  # noqa: BLE001 - reported to the user, never lost
            result = e
        self.done.emit(result)


# =============================================================== generic table

@dataclass
class Column:
    key: str
    header: str
    sort_key: str | None = None   # Sort by another field (e.g. bytes for a "1.2 GB" column).
    numeric: bool = False         # Missing values sort as -1 instead of text.
    check: bool = False           # Show a bool field as a checkbox.
    editable: bool = False
    stretch: bool = False
    width: int | None = None


class RecordModel(QAbstractTableModel):
    """A table whose rows are plain dicts.

    Special row keys:  _color (text color), _tip (tooltip).
    Hooks you can set: can_edit(row, key) -> bool, on_edit(row, key, value) -> bool.
    """

    def __init__(self, columns: list[Column]):
        super().__init__()
        self.columns = columns
        self.rows: list[dict] = []
        self.can_edit = lambda row, key: True
        self.on_edit = None

    # --- data management
    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def add_rows(self, rows: list[dict]) -> None:
        if not rows:
            return
        start = len(self.rows)
        self.beginInsertRows(QModelIndex(), start, start + len(rows) - 1)
        self.rows.extend(rows)
        self.endInsertRows()

    def refresh_all(self) -> None:
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.columns) - 1))

    # --- Qt interface
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section].header
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        col, row = self.columns[index.column()], self.rows[index.row()]
        if col.check and self.can_edit(row, col.key):
            f |= Qt.ItemFlag.ItemIsUserCheckable
        if col.editable and self.can_edit(row, col.key):
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = self.rows[index.row()], self.columns[index.column()]
        value = row.get(col.key)
        R = Qt.ItemDataRole
        if role == R.CheckStateRole and col.check:
            return Qt.CheckState.Checked if value else Qt.CheckState.Unchecked
        if role == R.DisplayRole:
            if col.check:
                return None
            return "" if value is None else str(value)
        if role == R.EditRole and col.editable:
            return "" if value is None else str(value)
        if role == R.UserRole:  # Sort key.
            v = row.get(col.sort_key or col.key)
            if col.check:
                return int(bool(v))
            if col.numeric or isinstance(v, (int, float)):
                return float(v) if isinstance(v, (int, float)) else -1.0
            return "" if v is None else str(v).lower()
        if role == R.ToolTipRole:
            return row.get("_tip") or (None if value is None else str(value))
        if role == R.ForegroundRole and row.get("_color"):
            return QColor(row["_color"])
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        row, col = self.rows[index.row()], self.columns[index.column()]
        if role == Qt.ItemDataRole.CheckStateRole and col.check:
            row[col.key] = _is_checked(value)
            if self.on_edit:
                self.on_edit(row, col.key, row[col.key])
        elif role == Qt.ItemDataRole.EditRole and col.editable and self.on_edit:
            if not self.on_edit(row, col.key, value):
                return False
        else:
            return False
        r = index.row()
        self.dataChanged.emit(self.index(r, 0), self.index(r, len(self.columns) - 1))
        return True


def _is_checked(value) -> bool:
    # Depending on the PySide6 version, check states arrive as ints or as enums.
    try:
        return Qt.CheckState(value) == Qt.CheckState.Checked
    except (ValueError, TypeError):
        return value == Qt.CheckState.Checked


class RecordFilter(QSortFilterProxyModel):
    """Text search across every column, plus an optional extra test per row
    (e.g. "problems only"). Sorts by the model's UserRole sort keys."""

    def __init__(self, model: RecordModel, parent=None):
        super().__init__(parent)
        self.setSourceModel(model)
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(-1)
        self.extra = None  # callable(row dict) -> bool

    def filterAcceptsRow(self, source_row, parent):
        if self.extra is not None and not self.extra(self.sourceModel().rows[source_row]):
            return False
        return super().filterAcceptsRow(source_row, parent)

    def refilter(self) -> None:
        self.invalidateFilter()

    def visible_rows(self) -> list[dict]:
        """Rows as currently shown: filtered and in the current sort order."""
        m = self.sourceModel()
        return [m.rows[self.mapToSource(self.index(r, 0)).row()] for r in range(self.rowCount())]


def make_table_view(proxy: RecordFilter, columns: list[Column]) -> QTableView:
    view = QTableView()
    view.setModel(proxy)
    view.setSortingEnabled(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                         | QAbstractItemView.EditTrigger.EditKeyPressed)
    view.setAlternatingRowColors(True)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(22)
    view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    hh = view.horizontalHeader()
    for i, c in enumerate(columns):
        if c.stretch:
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
        elif c.width:
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            hh.resizeSection(i, c.width)
        else:
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
    return view


def selected_records(view: QTableView, proxy: RecordFilter) -> list[dict]:
    rows = sorted({proxy.mapToSource(i).row() for i in view.selectionModel().selectedRows()})
    return [proxy.sourceModel().rows[r] for r in rows]


def export_csv(path: str, columns: list[Column], rows: list[dict]) -> None:
    # utf-8-sig so Excel opens accented and Japanese names correctly.
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([c.header or c.key for c in columns])
        for r in rows:
            w.writerow([("yes" if r.get(c.key) else "no") if c.check else r.get(c.key, "") for c in columns])


# =============================================================== reusable widgets

class FolderPicker(QWidget):
    """Editable dropdown of recent folders plus a Browse button.
    Emits `chosen(path)` on Enter, on Browse, and when a recent folder is picked."""
    chosen = Signal(str)

    def __init__(self, label: str, recent: list[str], placeholder: str = "",
                 browse_title: str = "Choose folder", parent=None):
        super().__init__(parent)
        self.browse_title = browse_title
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.combo.lineEdit().setPlaceholderText(placeholder)
        self.combo.lineEdit().returnPressed.connect(lambda: self.chosen.emit(self.text()))
        self.combo.activated.connect(lambda _: self.chosen.emit(self.text()))
        self.set_recent(recent)
        b = QPushButton("Browse…")
        b.clicked.connect(self.browse)
        lbl = QLabel(label)
        lbl.setMinimumWidth(70)
        lay.addWidget(lbl)
        lay.addWidget(self.combo, 1)
        lay.addWidget(b)

    def text(self) -> str:
        return self.combo.currentText().strip().strip('"')  # Pasted paths often have quotes.

    def set_text(self, text: str) -> None:
        self.combo.setEditText(text)

    def path(self) -> Path | None:
        t = self.text()
        return Path(t) if t else None

    def set_recent(self, recent: list[str]) -> None:
        current = self.combo.currentText()
        self.combo.blockSignals(True)
        self.combo.clear()
        # Only offer folders that exist here: the list can mix Linux and Windows paths.
        for p in recent:
            if Path(p).is_dir():
                self.combo.addItem(p)
        self.combo.setEditText(current if current else (self.combo.itemText(0) if self.combo.count() else ""))
        self.combo.blockSignals(False)

    def browse(self) -> None:
        start = self.text() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, self.browse_title, start)
        if d:
            self.set_text(d)
            self.chosen.emit(d)


class HistoryDialog(QDialog):
    """Lists past runs of one tab (newest first) and reverts any of them.
    The tab needs `.history` (HistoryStore) and `.revert(batch, after=callable)`."""

    def __init__(self, tab: "ToolTab"):
        super().__init__(tab)
        self.tab = tab
        self.setWindowTitle(f"{tab.title} history")
        self.resize(700, 420)
        lay = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self._revert())
        lay.addWidget(self.list)
        row = QHBoxLayout()
        b_rev = QPushButton("Revert selected")
        b_rev.clicked.connect(self._revert)
        b_open = QPushButton("Open output folder")
        b_open.clicked.connect(self._open)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.accept)
        row.addWidget(b_rev)
        row.addWidget(b_open)
        row.addStretch()
        row.addWidget(b_close)
        lay.addLayout(row)
        self.refresh()

    def refresh(self):
        self.list.clear()
        for batch in reversed(self.tab.history.batches):
            n = len(batch["moves"])
            out = batch.get("output") or batch.get("root", "")
            where = batch.get("root", "")
            if out and out != where:
                where = f"{where}  ->  {out}"
            it = QListWidgetItem(f"{batch['time']}   {n} file{'s' * (n != 1)}   {where}")
            it.setData(Qt.ItemDataRole.UserRole, batch["id"])
            # Hover to see what was moved (first 40 files).
            it.setToolTip("\n".join(f"{Path(m['src']).name}  ->  {m['dst']}" for m in batch["moves"][:40]))
            self.list.addItem(it)

    def _selected(self):
        it = self.list.currentItem()
        if not it:
            return None
        bid = it.data(Qt.ItemDataRole.UserRole)
        return next((b for b in self.tab.history.batches if b["id"] == bid), None)

    def _revert(self):
        batch = self._selected()
        if batch:
            self.tab.revert(batch, after=self.refresh)

    def _open(self):
        batch = self._selected()
        if batch:
            self.tab.safe_open(Path(batch.get("output") or batch.get("root", "")))


# =============================================================== base tab

class ToolTab(QWidget):
    """Base for every tab.

    Subclasses put their widgets in `self.body_layout`. The base adds a log
    below the body and a progress row (status, bar, Cancel) at the bottom,
    shown only while a background task runs. While busy, only the body is
    disabled, so other tabs keep working.
    """
    title = "Tool"
    busy_changed = Signal(bool)  # Lets the main window mark a working tab.

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._thread: QThread | None = None
        self._worker: TaskWorker | None = None
        self._on_done = None
        self._on_partial = None
        self.setAcceptDrops(True)

        outer = QVBoxLayout(self)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setPlaceholderText("Activity log")

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.body)
        split.addWidget(self.log)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)
        outer.addWidget(split, 1)

        self.progress_row = QWidget()
        pr = QHBoxLayout(self.progress_row)
        pr.setContentsMargins(0, 0, 0, 0)
        self.status_label = QLabel("")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(320)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_task)
        pr.addWidget(self.status_label, 1)
        pr.addWidget(self.progress)
        pr.addWidget(self.cancel_btn)
        self.progress_row.hide()
        outer.addWidget(self.progress_row)

    # ---------------------------------------------------------- helpers for subclasses
    @property
    def busy(self) -> bool:
        return self._busy

    def write_log(self, text: str) -> None:
        self.log.appendPlainText(f"[{datetime.now():%H:%M:%S}] {text}")

    def safe_open(self, p: Path, reveal: bool = False) -> None:
        try:
            if not p.exists():
                raise FileNotFoundError(f"{p} does not exist")
            reveal_in_folder(p) if reveal else open_path(p)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Can't open", str(e))

    def ask_save(self, title: str, default_name: str, filters: str) -> str | None:
        path, _ = QFileDialog.getSaveFileName(self, title, str(Path.home() / default_name), filters)
        return path or None

    def on_idle(self) -> None:
        """Called after a task finishes; override to refresh buttons."""

    def folder_dropped(self, path: Path) -> None:
        """Called when a folder is dropped on the tab; override to use it."""

    # ---------------------------------------------------------- drag & drop
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls and not self._busy:
            p = Path(urls[0].toLocalFile())
            self.folder_dropped(p if p.is_dir() else p.parent)

    # ---------------------------------------------------------- background tasks
    def run_task(self, fn, args: tuple, on_done, label: str, on_partial=None) -> bool:
        """Run fn(*args) in a thread. on_done(result) and on_partial(list) run on the
        GUI thread. If fn raises, the error is shown and on_done is not called."""
        if self._busy:
            return False
        self._busy = True
        self._on_done, self._on_partial = on_done, on_partial
        self.body.setEnabled(False)
        self.progress.setRange(0, 0)  # Busy animation until the first progress report.
        self.status_label.setText(label)
        self.cancel_btn.setEnabled(True)
        self.progress_row.show()

        self._thread = QThread(self)
        self._worker = TaskWorker(fn, args)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        # Bound methods of this widget are queued back to the GUI thread automatically.
        self._worker.progress.connect(self._task_progress)
        self._worker.partial.connect(self._task_partial)
        self._worker.done.connect(self._task_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()
        self.busy_changed.emit(True)
        return True

    def cancel_task(self) -> None:
        if self._worker:
            self._worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.status_label.setText("Cancelling after the current file…")

    def _task_progress(self, done: int, total: int, text: str):
        if total <= 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        if text and self.cancel_btn.isEnabled():
            self.status_label.setText(f"{done + 1}/{total}  {text}" if total > 0 else text)

    def _task_partial(self, items):
        if self._on_partial:
            self._on_partial(items)

    def _task_done(self, result):
        self._busy = False
        self._thread = self._worker = None
        self.body.setEnabled(True)
        self.progress_row.hide()
        callback, self._on_done, self._on_partial = self._on_done, None, None
        if isinstance(result, Exception):
            self.write_log(f"Error: {result}")
            QMessageBox.critical(self, "Something went wrong", str(result))
        elif callback:
            callback(result)
        self.on_idle()
        self.busy_changed.emit(False)


def missing_library_banner(package: str, extra: str = "") -> QLabel:
    """Shown at the top of a tab whose optional library isn't installed."""
    lbl = QLabel(f"This tab needs the <b>{package}</b> package. Install it with "
                 f"<code>pip install {package}</code> and restart.{(' ' + extra) if extra else ''}")
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color: {ERROR_COLOR}; padding: 6px;")
    return lbl


def run_standalone(tab_cls) -> None:
    """Run one tab as its own window (each tool file can still be started alone)."""
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    win = QMainWindow()
    tab = tab_cls()
    win.setCentralWidget(tab)
    win.setWindowTitle(tab.title)
    win.resize(1100, 720)

    def close_event(e, _orig=win.closeEvent):
        # Closing mid-task would kill the thread halfway through a copy.
        if tab.busy:
            QMessageBox.information(win, "Still working", "Wait for the current task to finish, or cancel it.")
            e.ignore()
        else:
            e.accept()

    win.closeEvent = close_event
    win.show()
    sys.exit(app.exec())
