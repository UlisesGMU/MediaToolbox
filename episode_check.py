#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Episode Check - looks inside each series folder and reports missing
episode numbers (and duplicates, like "05" and "05v2" side by side).

The original script's pattern ("- 01") stays first and is the default;
the other patterns only run when it doesn't find a number, and each one
can be turned off, reordered by dragging, or replaced with your own regex.

Part of Media Toolbox; can also be run on its own:  python episode_check.py
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QVBoxLayout,
)

from common import (
    DIM_COLOR, ERROR_COLOR, WARN_COLOR, Column, FolderPicker, RecordFilter, RecordModel, ToolTab,
    compact_ranges, export_csv, load_tool_settings, make_table_view, parse_extensions,
    remember_recent, run_standalone, save_tool_settings, selected_records,
)

SETTINGS_KEY = "episode_check"

# Each pattern finds the episode number (group 1) in a file name.
#   target: "filename" = full name, "stem" = name without extension.
EPISODE_PATTERNS = {
    # The original script's regex, unchanged: the first "- 01" in the name.
    "original": {
        "label": "Title - 01  (original)",
        "regex": r"-\s*(\d+)(?:v\d+)?",
        "target": "filename",
    },
    "sxxexx": {
        "label": "S01E02",
        "regex": r"(?i)S\d{1,2}[\s_.]?E(\d{1,4})",
        "target": "stem",
    },
    "nxnn": {
        "label": "1x02",
        "regex": r"(?<!\d)\d{1,2}x(\d{2,3})(?!\d)",
        "target": "stem",
    },
    # "Cap"/"Capitulo" cover Spanish releases.
    "episode": {
        "label": "Episode 02 / Ep 02 / Cap 02",
        "regex": r"(?i)(?:episode|ep|cap[ií]tulo|cap)[\s_.]*(\d{1,4})(?!\d)",
        "target": "stem",
    },
    # A number standing alone and followed by a bracket or the end of the name:
    # "[Rakuen]_Bounen_no_Xamdou_02_[64BEA878]" -> 2. "_1080p" never matches,
    # and numbers inside [CRC] tags are skipped because they touch letters.
    "underscore": {
        "label": "_02_[CRC]  /  Title 02",
        "regex": r"(?<![A-Za-z0-9])(\d{1,4})(?:v\d+)?(?=[\s_.]*(?:[\[(]|$))",
        "target": "stem",
    },
}
PATTERN_ORDER = ["original", "sxxexx", "nxnn", "episode", "underscore"]

DEFAULT_SETTINGS = {
    "recent_folders": [r"D:\Temporal\Seguidos"],
    "extensions": [".mkv"],             # The original only looked at .mkv files.
    "patterns": [{"id": i, "enabled": True} for i in PATTERN_ORDER],
    "custom_regex": "",                 # Tried before the built-ins when set.
    "start_at_one": True,               # The original counted from episode 1.
    "nested": False,                    # The original only checked direct subfolders.
    "show_complete": True,
}


# =============================================================== core logic (no Qt here)

def compiled_patterns(settings: dict) -> list[tuple[str, re.Pattern, str]]:
    """Enabled patterns in order as (label, regex, target). A custom regex goes first."""
    out = []
    custom = settings.get("custom_regex", "").strip()
    if custom:
        try:
            rx = re.compile(custom, re.IGNORECASE)
            if rx.groups:
                out.append(("Custom", rx, "stem"))
        except re.error:
            pass  # Validated in the tab; a broken one is simply ignored here.
    for p in settings.get("patterns", []):
        spec = EPISODE_PATTERNS.get(p.get("id"))
        if spec and p.get("enabled", True):
            out.append((spec["label"], re.compile(spec["regex"]), spec["target"]))
    return out


def episode_number(filename: str, patterns) -> int | None:
    stem = Path(filename).stem
    for _label, rx, target in patterns:
        m = rx.search(filename if target == "filename" else stem)
        if m and m.group(1) and m.group(1).isdigit():
            return int(m.group(1))
    return None


def check_folder(folder: Path, root: Path, settings: dict, patterns) -> dict | None:
    """Analyze one folder. Returns None if it has no matching files at all."""
    exts = {e.lower() for e in settings["extensions"]}
    try:
        files = sorted(e.name for e in os.scandir(folder) if e.is_file() and Path(e.name).suffix.lower() in exts)
    except OSError as e:
        return _row(folder, root, [], [], error=str(e))
    if not files:
        return None
    numbers, unrecognized = [], []
    for f in files:
        n = episode_number(f, patterns)
        (numbers.append(n) if n is not None else unrecognized.append(f))
    return _row(folder, root, numbers, unrecognized, start_at_one=settings.get("start_at_one", True),
                files=len(files))


def _row(folder: Path, root: Path, numbers: list[int], unrecognized: list[str],
         start_at_one: bool = True, files: int = 0, error: str = "") -> dict:
    try:
        rel = str(folder.relative_to(root))
    except ValueError:
        rel = str(folder)
    uniq = sorted(set(numbers))
    dups = sorted({n for n in numbers if numbers.count(n) > 1})
    missing = []
    if uniq:
        start = 1 if start_at_one else uniq[0]
        have = set(uniq)
        # Episode 0 (specials, prologues) never counts as a gap.
        missing = [n for n in range(max(start, 1), uniq[-1] + 1) if n not in have]

    if error:
        status, color = "Error", ERROR_COLOR
    elif not uniq:
        status, color = "No episode numbers", DIM_COLOR
    elif missing:
        status, color = "Missing", ERROR_COLOR
    elif dups:
        status, color = "Duplicates", WARN_COLOR
    else:
        status, color = "Complete", None

    tip = [str(folder)]
    if unrecognized:
        tip += ["", "Files without a recognized episode number:"] + [f"  {f}" for f in unrecognized[:30]]
        if len(unrecognized) > 30:
            tip.append(f"  … and {len(unrecognized) - 30} more")
    return {
        "path": str(folder), "folder": rel, "status": status, "_color": color,
        "files": files, "episodes": len(uniq),
        "first": uniq[0] if uniq else -1, "last": uniq[-1] if uniq else -1,
        "range": f"{uniq[0]:02d}-{uniq[-1]:02d}" if uniq else "",
        "missing_list": missing, "missing": compact_ranges(missing), "missing_count": len(missing),
        "dups_list": dups, "duplicates": compact_ranges(dups),
        "unrecognized_list": unrecognized, "unrecognized": len(unrecognized),
        "error": error, "_tip": "\n".join(tip),
    }


def folders_to_check(root: Path, nested: bool) -> list[Path]:
    """Direct subfolders (the original behavior), or every folder below root."""
    if nested:
        out = [Path(d) for d, _subs, _files in os.walk(root)]
        return sorted((p for p in out if p != root), key=lambda p: str(p).lower())
    return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.lower())


def scan_episodes(root: Path, settings: dict, progress=None, cancelled=None) -> list[dict]:
    patterns = compiled_patterns(settings)
    folders = folders_to_check(root, settings.get("nested", False))
    rows = []
    for n, folder in enumerate(folders):
        if cancelled and cancelled():
            break
        if progress:
            progress(n, len(folders), folder.name)
        row = check_folder(folder, root, settings, patterns)
        if row:
            rows.append(row)
    return rows


def text_report(rows: list[dict], root: str) -> str:
    """Same kind of lines the original wrote to reporte_episodios.txt."""
    lines = [f"Episode report for: {root}", ""]
    for r in rows:
        if r["missing_list"]:
            lines.append(f"Warning: '{r['folder']}' is missing episodes: {r['missing_list']}")
        if r["dups_list"]:
            lines.append(f"Note: '{r['folder']}' has more than one file for episodes: {r['dups_list']}")
        if r["error"]:
            lines.append(f"Error: '{r['folder']}' could not be read: {r['error']}")
    if len(lines) == 2:
        lines.append("No missing episodes found.")
    return "\n".join(lines) + "\n"


# =============================================================== the tab

COLUMNS = [
    Column("status", "Status"),
    Column("folder", "Folder", stretch=True),
    Column("episodes", "Episodes", numeric=True),
    Column("range", "Range", sort_key="last", numeric=True),
    Column("missing", "Missing", sort_key="missing_count", numeric=True, width=220),
    Column("duplicates", "Duplicates", width=120),
    Column("unrecognized", "Unrecognized", numeric=True),
]


class EpisodeCheckTab(ToolTab):
    title = "Episode Check"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = load_tool_settings(SETTINGS_KEY, DEFAULT_SETTINGS)
        # Patterns added in a later version show up at the end, turned off.
        known = {p["id"] for p in self.settings["patterns"]}
        self.settings["patterns"] += [{"id": i, "enabled": False} for i in PATTERN_ORDER if i not in known]
        self.scanned_root: Path | None = None
        lay = self.body_layout

        self.folder = FolderPicker("Folder:", self.settings["recent_folders"],
                                   "Folder that contains one folder per series (or drop it here)")
        self.folder.chosen.connect(lambda _: self.start())
        lay.addWidget(self.folder)

        # --- options: patterns list on the left, other options on the right
        opts = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("Episode number patterns (drag to reorder, first match wins):"))
        self.pattern_list = QListWidget()
        self.pattern_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.pattern_list.setMaximumHeight(118)
        for p in self.settings["patterns"]:
            spec = EPISODE_PATTERNS.get(p["id"])
            if not spec:
                continue
            it = QListWidgetItem(spec["label"])
            it.setData(Qt.ItemDataRole.UserRole, p["id"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if p.get("enabled", True) else Qt.CheckState.Unchecked)
            it.setToolTip(f"Regex: {spec['regex']}")
            self.pattern_list.addItem(it)
        left.addWidget(self.pattern_list)
        crow = QHBoxLayout()
        crow.addWidget(QLabel("Custom regex:"))
        self.custom_edit = QLineEdit(self.settings.get("custom_regex", ""))
        self.custom_edit.setPlaceholderText(r"Optional, tried first. Put the number in a group, e.g. #(\d+)")
        crow.addWidget(self.custom_edit)
        left.addLayout(crow)
        opts.addLayout(left, 3)

        right = QVBoxLayout()
        erow = QHBoxLayout()
        erow.addWidget(QLabel("Extensions:"))
        self.ext_edit = QLineEdit(", ".join(self.settings["extensions"]))
        erow.addWidget(self.ext_edit)
        right.addLayout(erow)
        self.start_chk = QCheckBox("Count from episode 1")
        self.start_chk.setToolTip("Off: count from the lowest episode found (for folders that start mid-season).")
        self.start_chk.setChecked(self.settings.get("start_at_one", True))
        self.nested_chk = QCheckBox("Also check folders inside folders")
        self.nested_chk.setChecked(self.settings.get("nested", False))
        self.complete_chk = QCheckBox("Show complete folders")
        self.complete_chk.setChecked(self.settings.get("show_complete", True))
        for w in (self.start_chk, self.nested_chk, self.complete_chk):
            right.addWidget(w)
        right.addStretch()
        self.b_start = QPushButton("Check episodes")
        self.b_start.setDefault(True)
        self.b_start.clicked.connect(self.start)
        right.addWidget(self.b_start)
        opts.addLayout(right, 2)
        lay.addLayout(opts)

        # --- filter row
        frow = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search folders…")
        self.search.setClearButtonEnabled(True)
        self.summary = QLabel("")
        frow.addWidget(self.search, 1)
        frow.addWidget(self.summary)
        lay.addLayout(frow)

        # --- table
        self.model = RecordModel(COLUMNS)
        self.proxy = RecordFilter(self.model, self)
        self.proxy.extra = lambda r: self.complete_chk.isChecked() or r["status"] != "Complete"
        self.table = make_table_view(self.proxy, COLUMNS)
        self.table.customContextMenuRequested.connect(self.show_menu)
        self.table.doubleClicked.connect(
            lambda i: self.safe_open(Path(self.model.rows[self.proxy.mapToSource(i).row()]["path"])))
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.search.textChanged.connect(lambda _: self.update_summary())
        self.complete_chk.toggled.connect(lambda _: (self.proxy.refilter(), self.update_summary()))
        lay.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        b_csv = QPushButton("Export CSV…")
        b_csv.clicked.connect(self.export_csv)
        b_txt = QPushButton("Save report…")
        b_txt.setToolTip("Missing episodes per folder, like the original reporte_episodios.txt")
        b_txt.clicked.connect(self.export_txt)
        bottom.addStretch()
        bottom.addWidget(b_csv)
        bottom.addWidget(b_txt)
        lay.addLayout(bottom)

        QShortcut(QKeySequence("F5"), self, activated=self.start,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

    # ---------------------------------------------------------- helpers
    def _read_options(self) -> bool:
        custom = self.custom_edit.text().strip()
        if custom:
            try:
                if re.compile(custom).groups == 0:
                    raise re.error("put the episode number in a group, e.g. (\\d+)")
            except re.error as e:
                QMessageBox.warning(self, "Invalid custom regex", str(e))
                return False
        self.settings["custom_regex"] = custom
        self.settings["patterns"] = [
            {"id": self.pattern_list.item(i).data(Qt.ItemDataRole.UserRole),
             "enabled": self.pattern_list.item(i).checkState() == Qt.CheckState.Checked}
            for i in range(self.pattern_list.count())]
        self.settings["extensions"] = parse_extensions(self.ext_edit.text()) or DEFAULT_SETTINGS["extensions"]
        self.ext_edit.setText(", ".join(self.settings["extensions"]))
        self.settings["start_at_one"] = self.start_chk.isChecked()
        self.settings["nested"] = self.nested_chk.isChecked()
        self.settings["show_complete"] = self.complete_chk.isChecked()
        return True

    def update_summary(self):
        rows = self.model.rows
        missing = sum(1 for r in rows if r["missing_list"])
        eps = sum(r["missing_count"] for r in rows)
        dups = sum(1 for r in rows if r["dups_list"])
        text = f"{len(rows)} folders,  {missing} with missing episodes ({eps} in total),  {dups} with duplicates"
        shown = self.proxy.rowCount()
        if shown != len(rows):
            text += f",  {shown} shown"
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
        if not self._read_options():
            return
        self.settings["recent_folders"] = remember_recent(self.settings["recent_folders"], root)
        save_tool_settings(SETTINGS_KEY, self.settings)
        self.folder.set_recent(self.settings["recent_folders"])
        self.folder.set_text(str(root))
        self.scanned_root = root

        def finished(rows):
            self.model.set_rows(rows)
            self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
            self.update_summary()
            missing = [r for r in rows if r["missing_list"]]
            self.write_log(f"Checked {len(rows)} folder(s) in {root}: {len(missing)} with missing episodes.")
            for r in missing:
                self.write_log(f"  {r['folder']}: missing {r['missing']}")

        self.run_task(scan_episodes, (root, dict(self.settings)), finished, "Checking folders…")

    def export_csv(self):
        rows = self.proxy.visible_rows()
        if not rows:
            return
        path = self.ask_save("Export CSV", "episode_check.csv", "CSV files (*.csv)")
        if path:
            export_csv(path, COLUMNS + [Column("path", "Full path")], rows)
            self.write_log(f"Exported {len(rows)} row(s) to {path}")

    def export_txt(self):
        if not self.model.rows:
            return
        path = self.ask_save("Save report", "episode_report.txt", "Text files (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text_report(self.model.rows, str(self.scanned_root or "")))
            self.write_log(f"Report saved to {path}")

    def show_menu(self, pos):
        rows = selected_records(self.table, self.proxy)
        if not rows:
            return
        first = rows[0]
        m = QMenu(self)
        m.addAction("Open folder", lambda: self.safe_open(Path(first["path"])))
        m.addSeparator()
        m.addAction("Copy missing episodes",
                    lambda: QApplication.clipboard().setText(
                        "\n".join(f"{r['folder']}: {r['missing']}" for r in rows if r["missing"])))
        a = m.addAction("Copy unrecognized file names",
                        lambda: QApplication.clipboard().setText(
                            "\n".join(f for r in rows for f in r["unrecognized_list"])))
        a.setEnabled(any(r["unrecognized_list"] for r in rows))
        m.addAction("Copy folder path" + ("s" if len(rows) > 1 else ""),
                    lambda: QApplication.clipboard().setText("\n".join(r["path"] for r in rows)))
        m.exec(self.table.viewport().mapToGlobal(pos))


if __name__ == "__main__":
    run_standalone(EpisodeCheckTab)
