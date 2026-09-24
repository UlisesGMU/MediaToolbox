#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Video Check - reads each video's frame rate information with MediaInfo and
flags files with problems.

Same checks as the original script:
    - Error:   the frame rate can't be read, or is 0 or less.
    - Warning: the frame rate mode is variable, or missing.
Plus optional extras: flag a minimum frame rate below a threshold, and show
resolution, codec and duration for every file.

Part of Media Toolbox; can also be run on its own:  python video_check.py
Requires:  pip install pymediainfo
           (on Linux also the MediaInfo library, e.g. "sudo pacman -S libmediainfo"
            or "sudo apt install libmediainfo0v5")
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QMessageBox, QPushButton,
)

from common import (
    DIM_COLOR, ERROR_COLOR, OK_COLOR, WARN_COLOR, Batcher, Column, FolderPicker, RecordFilter,
    RecordModel, ToolTab, export_csv, human_duration, human_size, load_tool_settings,
    make_table_view, missing_library_banner, parse_extensions, remember_recent, run_standalone,
    save_tool_settings, selected_records,
)

# pymediainfo is optional: without it this tab explains how to install it,
# and every other tab keeps working.
try:
    from pymediainfo import MediaInfo
except ImportError:  # pragma: no cover - depends on the machine
    MediaInfo = None

SETTINGS_KEY = "video_check"

DEFAULT_SETTINGS = {
    "recent_folders": [],
    "extensions": [".mp4", ".mkv", ".avi", ".mov", ".webm"],
    "recursive": True,          # The original script always looked in every subfolder.
    "min_fps_warning": 0.0,     # 0 = off. Otherwise warn when Minimum FPS is below this.
}


# =============================================================== core logic (no Qt here)

def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def find_files(root: Path, extensions: list[str], recursive: bool) -> list[Path]:
    exts = {e.lower() for e in extensions}
    if recursive:
        out = []
        for folder, _dirs, files in os.walk(root):
            out.extend(Path(folder) / f for f in files if Path(f).suffix.lower() in exts)
        return sorted(out, key=lambda p: str(p).lower())
    return sorted((p for p in root.iterdir() if p.is_file() and p.suffix.lower() in exts),
                  key=lambda p: p.name.lower())


def check_video(path: Path, root: Path, settings: dict) -> dict:
    """Read one file and return a table row with its values and problems."""
    try:
        size = path.stat().st_size
    except OSError:
        size = -1
    row = {
        "path": str(path), "file": path.name, "folder": _relative_folder(path, root),
        "fps": None, "mode": "", "min_fps": None, "max_fps": None,
        "resolution": "", "pixels": -1, "codec": "", "duration": None, "duration_text": "",
        "size": size, "size_text": human_size(size) if size >= 0 else "",
        "errors": [], "warnings": [],
    }
    try:
        info = MediaInfo.parse(str(path))
    except Exception as e:  # noqa: BLE001 - corrupt or unreadable files are a result, not a crash
        row["errors"].append(f"Can't read file: {e}")
        return _finish(row)

    general = next((t for t in info.tracks if t.track_type == "General"), None)
    video = next((t for t in info.tracks if t.track_type == "Video"), None)
    if general is not None:
        ms = _float(getattr(general, "duration", None))
        if ms:
            row["duration"] = ms / 1000
            row["duration_text"] = human_duration(ms / 1000)
    if video is None:
        row["errors"].append("No video track")
        return _finish(row)

    # --- frame rate (same checks as the original script)
    fps = _float(video.frame_rate)
    row["fps"] = fps
    if fps is None:
        row["errors"].append("Frame rate unreadable")
    elif fps <= 0:
        row["errors"].append("Frame rate is 0 or less")

    # --- frame rate mode: MediaInfo reports "CFR"/"VFR" (older versions "Constant")
    mode = (video.frame_rate_mode or "").strip()
    row["mode"] = mode
    if not mode:
        row["warnings"].append("No frame rate mode")
    elif mode.lower() not in ("constant", "cfr"):
        row["warnings"].append("Variable frame rate")

    # --- minimum / maximum frame rate (informative, like the original)
    row["min_fps"] = _float(getattr(video, "minimum_frame_rate", None))
    row["max_fps"] = _float(getattr(video, "maximum_frame_rate", None))
    threshold = settings.get("min_fps_warning", 0) or 0
    if threshold > 0 and row["min_fps"] is not None and row["min_fps"] < threshold:
        row["warnings"].append(f"Minimum frame rate {row['min_fps']:g} is below {threshold:g}")

    # --- extras
    w, h = video.width, video.height
    if w and h:
        row["resolution"] = f"{w}x{h}"
        row["pixels"] = int(w) * int(h)  # Sort key: 1920x1080 above 1280x720.
    row["codec"] = video.format or ""
    return _finish(row)


def _relative_folder(path: Path, root: Path) -> str:
    try:
        rel = path.parent.relative_to(root)
        return "." if str(rel) == "." else str(rel)
    except ValueError:
        return str(path.parent)


def _finish(row: dict) -> dict:
    """Set the result column, color and tooltip from the collected problems."""
    if row["errors"]:
        row["result"], row["_color"] = "Error", ERROR_COLOR
    elif row["warnings"]:
        row["result"], row["_color"] = "Warning", WARN_COLOR
    else:
        row["result"], row["_color"] = "OK", None
    row["issues"] = "; ".join(row["errors"] + row["warnings"])
    for key in ("fps", "min_fps", "max_fps"):
        v = row[key]
        row[f"{key}_text"] = "" if v is None else f"{v:g}"
    row["_tip"] = row["path"]
    return row


def scan_videos(root: Path, settings: dict, progress=None, cancelled=None, emit=None) -> dict:
    """Check every video under root, streaming rows to the tab as they're ready."""
    if progress:
        progress(0, 0, "Looking for video files…")
    files = find_files(root, settings["extensions"], settings.get("recursive", True))
    batch = Batcher(emit)
    done = 0
    for n, p in enumerate(files):
        if cancelled and cancelled():
            break
        if progress:
            progress(n, len(files), p.name)
        batch.add(check_video(p, root, settings))
        done += 1
    batch.flush()
    return {"found": len(files), "checked": done}


def recheck(paths: list[Path], root: Path, settings: dict, progress=None, cancelled=None) -> list[dict]:
    rows = []
    for n, p in enumerate(paths):
        if cancelled and cancelled():
            break
        if progress:
            progress(n, len(paths), p.name)
        rows.append(check_video(p, root, settings))
    return rows


def text_report(rows: list[dict], root: str) -> str:
    """Plain-text summary in the spirit of the original script's console output."""
    ok = sum(1 for r in rows if r["result"] == "OK")
    fps_err = sum(1 for r in rows if r["errors"])
    mode_warn = sum(1 for r in rows if any("frame rate mode" in w.lower() or "variable" in w.lower()
                                           for w in r["warnings"]))
    lines = [
        f"Video check report for: {root}", "=" * 60,
        f"Files checked:                 {len(rows)}",
        f"No problems:                   {ok}",
        f"Frame rate errors:             {fps_err}",
        f"Variable or missing FR mode:   {mode_warn}", "",
    ]
    problems = [r for r in rows if r["result"] != "OK"]
    if problems:
        lines.append("Files with problems:")
        for r in problems:
            lines.append(f" - {r['path']}")
            for issue in r["errors"] + r["warnings"]:
                lines.append(f"    -> {issue}")
    else:
        lines.append("All files are OK.")
    mins = [r for r in rows if r["min_fps"] is not None]
    if mins:
        lines += ["", "Minimum frame rates found:"]
        lines += [f" - {r['path']}: {r['min_fps']:g} fps" for r in mins]
    return "\n".join(lines) + "\n"


# =============================================================== the tab

COLUMNS = [
    Column("result", "Result"),
    Column("file", "File", stretch=True),
    Column("fps_text", "FPS", sort_key="fps", numeric=True),
    Column("mode", "Mode"),
    Column("min_fps_text", "Min FPS", sort_key="min_fps", numeric=True),
    Column("max_fps_text", "Max FPS", sort_key="max_fps", numeric=True),
    Column("resolution", "Resolution", sort_key="pixels", numeric=True),
    Column("codec", "Codec"),
    Column("duration_text", "Duration", sort_key="duration", numeric=True),
    Column("size_text", "Size", sort_key="size", numeric=True),
    Column("issues", "Issues", width=260),
    Column("folder", "Folder", width=200),
]

SHOW_OPTIONS = ["All files", "Problems only", "Errors only", "Warnings only", "OK only"]


class VideoCheckTab(ToolTab):
    title = "Video Check"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = load_tool_settings(SETTINGS_KEY, DEFAULT_SETTINGS)
        self.scanned_root: Path | None = None
        lay = self.body_layout

        if MediaInfo is None:
            lay.addWidget(missing_library_banner(
                "pymediainfo", "On Linux, also install the MediaInfo library (libmediainfo)."))
        elif not MediaInfo.can_parse():
            lay.addWidget(missing_library_banner(
                "MediaInfo library",
                "pymediainfo is installed but can't find libmediainfo. "
                "On Linux install it with your package manager (libmediainfo)."))

        self.folder = FolderPicker("Folder:", self.settings["recent_folders"],
                                   "Folder with videos (or drop a folder here)")
        self.folder.chosen.connect(lambda _: self.start())
        lay.addWidget(self.folder)

        # --- options
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Extensions:"))
        self.ext_edit = QLineEdit(", ".join(self.settings["extensions"]))
        self.ext_edit.setMaximumWidth(260)
        opts.addWidget(self.ext_edit)
        self.recursive_chk = QCheckBox("Include subfolders")
        self.recursive_chk.setChecked(self.settings.get("recursive", True))
        opts.addWidget(self.recursive_chk)
        opts.addWidget(QLabel("Warn if min FPS below:"))
        self.min_spin = QDoubleSpinBox()
        self.min_spin.setRange(0, 240)
        self.min_spin.setDecimals(3)
        self.min_spin.setSpecialValueText("off")  # 0 shows as "off"
        self.min_spin.setValue(self.settings.get("min_fps_warning", 0) or 0)
        self.min_spin.setToolTip("Adds a warning when a file's minimum frame rate is lower than this. 0 = off.")
        opts.addWidget(self.min_spin)
        opts.addStretch()
        self.b_start = QPushButton("Check videos")
        self.b_start.setDefault(True)
        self.b_start.clicked.connect(self.start)
        self.b_start.setEnabled(MediaInfo is not None)
        opts.addWidget(self.b_start)
        lay.addLayout(opts)

        # --- filter row
        frow = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search file, codec, issue…")
        self.search.setClearButtonEnabled(True)
        self.show_combo = QComboBox()
        self.show_combo.addItems(SHOW_OPTIONS)
        self.summary = QLabel("")
        frow.addWidget(self.search, 1)
        frow.addWidget(self.show_combo)
        frow.addWidget(self.summary)
        lay.addLayout(frow)

        # --- table
        self.model = RecordModel(COLUMNS)
        self.proxy = RecordFilter(self.model, self)
        self.proxy.extra = self._show_filter
        self.table = make_table_view(self.proxy, COLUMNS)
        self.table.customContextMenuRequested.connect(self.show_menu)
        self.table.doubleClicked.connect(lambda i: self.safe_open(Path(self._row_at(i)["path"])))
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.search.textChanged.connect(lambda _: self.update_summary())
        self.show_combo.currentIndexChanged.connect(lambda _: (self.proxy.refilter(), self.update_summary()))
        lay.addWidget(self.table, 1)

        # --- export
        bottom = QHBoxLayout()
        b_csv = QPushButton("Export CSV…")
        b_csv.clicked.connect(self.export_csv)
        b_txt = QPushButton("Save report…")
        b_txt.setToolTip("Summary and list of problem files, like the original script printed")
        b_txt.clicked.connect(self.export_txt)
        bottom.addStretch()
        bottom.addWidget(b_csv)
        bottom.addWidget(b_txt)
        lay.addLayout(bottom)

        QShortcut(QKeySequence("F5"), self, activated=self.start,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

    # ---------------------------------------------------------- helpers
    def _row_at(self, proxy_index) -> dict:
        return self.model.rows[self.proxy.mapToSource(proxy_index).row()]

    def _show_filter(self, row: dict) -> bool:
        choice = self.show_combo.currentIndex()
        result = row["result"]
        return {0: True, 1: result != "OK", 2: result == "Error",
                3: result == "Warning", 4: result == "OK"}[choice]

    def _read_options(self):
        self.settings["extensions"] = parse_extensions(self.ext_edit.text()) or DEFAULT_SETTINGS["extensions"]
        self.settings["recursive"] = self.recursive_chk.isChecked()
        self.settings["min_fps_warning"] = self.min_spin.value()
        self.ext_edit.setText(", ".join(self.settings["extensions"]))

    def update_summary(self):
        rows = self.model.rows
        ok = sum(1 for r in rows if r["result"] == "OK")
        err = sum(1 for r in rows if r["result"] == "Error")
        warn = sum(1 for r in rows if r["result"] == "Warning")
        text = f"{len(rows)} checked,  {ok} OK,  {err} errors,  {warn} warnings"
        shown = self.proxy.rowCount()
        if shown != len(rows):
            text += f",  {shown} shown"
        self.summary.setText(text)

    def folder_dropped(self, path: Path):
        self.folder.set_text(str(path))
        self.start()

    # ---------------------------------------------------------- actions
    def start(self):
        if self.busy or MediaInfo is None:
            return
        root = self.folder.path()
        if not root or not root.is_dir():
            QMessageBox.warning(self, "Folder not found", f"'{self.folder.text()}' doesn't exist or isn't a folder.")
            return
        self._read_options()
        self.settings["recent_folders"] = remember_recent(self.settings["recent_folders"], root)
        save_tool_settings(SETTINGS_KEY, self.settings)
        self.folder.set_recent(self.settings["recent_folders"])
        self.folder.set_text(str(root))

        self.scanned_root = root
        self.model.set_rows([])
        self.update_summary()

        def partial(rows):
            self.model.add_rows(rows)
            self.update_summary()

        def finished(result):
            note = "" if result["checked"] == result["found"] else f" (cancelled after {result['checked']})"
            self.write_log(f"Checked {result['checked']} of {result['found']} video(s) in {root}{note}.")
            self.update_summary()

        self.run_task(scan_videos, (root, dict(self.settings)), finished, "Checking videos…", partial)

    def recheck_selected(self, rows: list[dict]):
        if self.busy or not rows:
            return
        self._read_options()
        by_path = {r["path"]: r for r in rows}

        def finished(new_rows):
            # Replace the old rows in place so the table keeps its order.
            for new in new_rows:
                old = by_path[new["path"]]
                old.clear()
                old.update(new)
            self.model.refresh_all()
            self.proxy.refilter()
            self.update_summary()
            self.write_log(f"Checked {len(new_rows)} file(s) again.")

        self.run_task(recheck, ([Path(r["path"]) for r in rows], self.scanned_root or Path("."),
                                dict(self.settings)), finished, "Checking again…")

    def export_csv(self):
        rows = self.proxy.visible_rows()
        if not rows:
            return
        path = self.ask_save("Export CSV", "video_check.csv", "CSV files (*.csv)")
        if path:
            cols = COLUMNS + [Column("path", "Full path")]
            export_csv(path, cols, rows)
            self.write_log(f"Exported {len(rows)} row(s) to {path}")

    def export_txt(self):
        if not self.model.rows:
            return
        path = self.ask_save("Save report", "video_check_report.txt", "Text files (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text_report(self.model.rows, str(self.scanned_root or "")))
            self.write_log(f"Report saved to {path}")

    # ---------------------------------------------------------- context menu
    def show_menu(self, pos):
        rows = selected_records(self.table, self.proxy)
        if not rows:
            return
        first = Path(rows[0]["path"])
        m = QMenu(self)
        m.addAction("Open file", lambda: self.safe_open(first))
        m.addAction("Show in folder", lambda: self.safe_open(first, reveal=True))
        m.addSeparator()
        m.addAction(f"Check again ({len(rows)})", lambda: self.recheck_selected(rows))
        m.addSeparator()
        m.addAction("Copy path" + ("s" if len(rows) > 1 else ""),
                    lambda: QApplication.clipboard().setText("\n".join(r["path"] for r in rows)))
        m.addAction("Copy issues",
                    lambda: QApplication.clipboard().setText(
                        "\n".join(f"{r['file']}: {r['issues'] or 'OK'}" for r in rows)))
        m.exec(self.table.viewport().mapToGlobal(pos))


if __name__ == "__main__":
    run_standalone(VideoCheckTab)
