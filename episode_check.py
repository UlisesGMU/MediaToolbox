#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Episode Check - looks inside each series folder and reports missing
episode numbers. It also points out older versions ("05" when "05v2", a
newer release, is in the same folder) and true duplicates (two files with
the same episode and version, e.g. from two release groups).

The original script's pattern ("- 01") stays first and is the default;
the other patterns only run when it doesn't find a number, and each one
can be turned off, reordered by dragging, or replaced with your own regex.

Part of Media Toolbox; can also be run on its own:  python episode_check.py
"""
from __future__ import annotations

import os
import re
from datetime import datetime
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
    HistoryDialog, HistoryStore, delete_files, delete_is_recoverable, move_files, revert_batch,
    sanitize_component, unique_path, require_folder,
    remember_recent, run_standalone, save_text, save_tool_settings, selected_records, timestamp, created_line,
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
    # Named groups: "ep" is the episode, "season" (optional) lets each season be
    # counted separately.
    "sxxexx": {
        "label": "S01E02",
        "regex": r"(?i)S(?P<season>\d{1,2})[\s_.]?E(?P<ep>\d{1,4})",
        "target": "stem",
    },
    "nxnn": {
        "label": "1x02",
        "regex": r"(?<!\d)(?P<season>\d{1,2})x(?P<ep>\d{2,3})(?!\d)",
        "target": "stem",
    },
    # "Episodio", "Cap", "Capitulo" cover Spanish releases.
    "episode": {
        "label": "Episode 02 / Episodio 02 / Ep 02 / Cap 02",
        "regex": r"(?i)(?:episode|episodio|ep|cap[ií]tulo|cap)[\s_.\-]*(\d{1,4})(?!\d)",
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
    "split_seasons": True,              # S01E05 and S02E05 are different episodes.
    # Cleaning up older versions ("05" when "05v2" exists):
    "old_versions_folder": "Old versions",  # Moved into this folder inside the series folder...
    "delete_old_versions": False,           # ...or deleted, only if turned on (off by default).
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


VERSION_RE = re.compile(r"[\s_.]*v(\d+)", re.IGNORECASE)


def episode_number(filename: str, patterns) -> tuple[int | None, int, int] | None:
    """Returns (season or None, episode, version).
    "05v2" -> (None, 5, 2); "S02E05" -> (2, 5, 1); no "v" means version 1."""
    stem = Path(filename).stem
    for _label, rx, target in patterns:
        text = filename if target == "filename" else stem
        m = rx.search(text)
        if not m:
            continue
        group = "ep" if "ep" in rx.groupindex else 1
        ep = m.group(group)
        if not ep or not ep.isdigit():
            continue
        season = None
        if "season" in rx.groupindex and m.group("season"):
            season = int(m.group("season"))
        # Release groups mark re-releases (fixed subs, better encode) as v2, v3...
        v = VERSION_RE.match(text, m.end(group))
        return season, int(ep), int(v.group(1)) if v else 1
    return None


def check_folder(folder: Path, root: Path, settings: dict, patterns) -> dict | None:
    """Analyze one folder. Returns None if it has no matching files at all."""
    exts = {e.lower() for e in settings["extensions"]}
    try:
        found = [e for e in os.scandir(folder) if e.is_file() and Path(e.name).suffix.lower() in exts]
        files = sorted(e.name for e in found)
        # Newest modification date: roughly when the last episode arrived.
        newest = max((e.stat().st_mtime for e in found), default=0)
    except OSError as e:
        return _row(folder, root, [], [], error=str(e))
    if not files:
        return None
    entries, unrecognized = [], []
    for f in files:
        found = episode_number(f, patterns)
        if found is None:
            unrecognized.append(f)
        else:
            entries.append((*found, f))
    return _row(folder, root, entries, unrecognized, start_at_one=settings.get("start_at_one", True),
                files=len(files), newest=newest, split_seasons=settings.get("split_seasons", True))


def _analyze(eps: list[tuple[int, int, str]], start_at_one: bool) -> dict:
    """One sequence of episodes: [(episode, version, file name)]."""
    by_ep: dict[int, list[tuple[int, str]]] = {}
    for ep, ver, name in eps:
        by_ep.setdefault(ep, []).append((ver, name))
    uniq = sorted(by_ep)

    # Older versions: a newer vN of the same episode exists, so these files are outdated.
    # Duplicates: two files with the same episode AND the same version (e.g. two release groups).
    old, old_files, dups = [], [], []
    for ep, versions in by_ep.items():
        top_version = max(v for v, _ in versions)
        outdated = sorted(name for v, name in versions if v < top_version)
        if outdated:
            old.append(ep)
            old_files.extend(outdated)
        counts = [v for v, _ in versions]
        if any(counts.count(v) > 1 for v in set(counts)):
            dups.append(ep)

    missing = []
    if uniq:
        start = 1 if start_at_one else uniq[0]
        # Episode 0 (specials, prologues) never counts as a gap.
        missing = [n for n in range(max(start, 1), uniq[-1] + 1) if n not in by_ep]
    return {"uniq": uniq, "missing": missing, "dups": sorted(dups), "old": sorted(old), "old_files": old_files}


def _season_label(season: int | None) -> str:
    return f"S{season:02d}" if season is not None else "No season"


def _row(folder: Path, root: Path, entries: list[tuple[int | None, int, int, str]], unrecognized: list[str],
         start_at_one: bool = True, files: int = 0, error: str = "", newest: float = 0,
         split_seasons: bool = True) -> dict:
    """entries: [(season or None, episode, version, file name)]."""
    try:
        rel = str(folder.relative_to(root))
    except ValueError:
        rel = str(folder)
    if rel == ".":
        rel = folder.name or str(folder)  # The chosen folder itself: show its name, not ".".

    # One group per season (or a single group when seasons aren't split or not present).
    groups: dict[int | None, list[tuple[int, int, str]]] = {}
    for season, ep, ver, name in entries:
        groups.setdefault(season if split_seasons else None, []).append((ep, ver, name))
    order = sorted(groups, key=lambda k: (k is None, k or 0))
    results = {k: _analyze(groups[k], start_at_one) for k in order}
    multi = len(results) > 1

    def joined(attr: str) -> str:
        """'3-4' for one season, 'S01: 3-4; S02: 7' for several."""
        if not multi:
            return compact_ranges(next(iter(results.values()))[attr]) if results else ""
        return "; ".join(f"{_season_label(k)}: {compact_ranges(r[attr])}" for k, r in results.items() if r[attr])

    def listed(attr: str) -> list:
        """Plain numbers for one season, 'S02E07' labels for several."""
        if not multi:
            return list(next(iter(results.values()))[attr]) if results else []
        return [f"{_season_label(k)}E{n:02d}" for k, r in results.items() for n in r[attr]]

    total_eps = sum(len(r["uniq"]) for r in results.values())
    last = max((r["uniq"][-1] for r in results.values() if r["uniq"]), default=-1)
    missing, dups, old = listed("missing"), listed("dups"), listed("old")
    old_files = sorted(f for r in results.values() for f in r["old_files"])
    if multi:
        rng = "  ".join(f"{_season_label(k)} {r['uniq'][0]:02d}-{r['uniq'][-1]:02d}"
                        for k, r in results.items() if r["uniq"])
    else:
        u = next(iter(results.values()))["uniq"] if results else []
        rng = f"{u[0]:02d}-{u[-1]:02d}" if u else ""

    if error:
        status, color = "Error", ERROR_COLOR
    elif not total_eps:
        status, color = "No episode numbers", DIM_COLOR
    elif missing:
        status, color = "Missing", ERROR_COLOR
    elif dups:
        status, color = "Duplicates", WARN_COLOR
    elif old:
        status, color = "Older versions", WARN_COLOR
    else:
        status, color = "Complete", None

    tip = [str(folder)]
    if multi:
        tip.append(f"Seasons: {', '.join(_season_label(k) for k in order)}")
    for title, names in (("Older versions (a newer v2, v3… is in the folder):", old_files),
                         ("Files without a recognized episode number:", unrecognized)):
        if names:
            tip += ["", title] + [f"  {f}" for f in names[:30]]
            if len(names) > 30:
                tip.append(f"  … and {len(names) - 30} more")
    return {
        "path": str(folder), "folder": rel, "status": status, "_color": color,
        "files": files, "episodes": total_eps, "seasons": len([k for k in order if k is not None]),
        "last": last, "range": rng,
        "missing_list": missing, "missing": joined("missing"), "missing_count": len(missing),
        "dups_list": dups, "duplicates": joined("dups"),
        "old_list": old, "old": joined("old"), "old_count": len(old_files), "old_files": old_files,
        "old_paths": [str(folder / f) for f in old_files],
        "unrecognized_list": unrecognized, "unrecognized": len(unrecognized),
        "error": error, "_tip": "\n".join(tip),
        "newest_ts": newest,
        "newest": datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M") if newest else "",
    }


def folders_to_check(root: Path, nested: bool, skip_name: str = "") -> list[Path]:
    """Direct subfolders (the original behavior), or every folder below root.
    Folders named skip_name ("Old versions") are left out: they hold the files
    moved aside by the clean-up, not a series."""
    skip = skip_name.casefold()
    if nested:
        out = []
        for d, subs, _files in os.walk(root):
            subs[:] = [x for x in subs if x.casefold() != skip]
            out.append(Path(d))
        return sorted((p for p in out if p != root), key=lambda p: str(p).lower())
    return sorted((p for p in root.iterdir() if p.is_dir() and p.name.casefold() != skip),
                  key=lambda p: p.name.lower())


def series_prefix(filename: str, patterns) -> str | None:
    """What comes before the episode number, normalized: which series a file belongs to.
    "[Rakuen]_Bounen_no_Xamdou_02_[64BEA878].mkv" -> "bounennoxamdou". Release
    group tags are ignored, so two groups' releases of one series match."""
    stem = Path(filename).stem
    for _label, rx, target in patterns:
        text = filename if target == "filename" else stem
        m = rx.search(text)
        if m:
            before = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", text[:m.start()])
            return re.sub(r"[\W_]+", "", before.casefold())
    return None


def loose_series_count(folder: Path, settings: dict, patterns) -> tuple[int, int]:
    """(episode files directly in folder, how many different series they belong to)."""
    exts = {e.lower() for e in settings["extensions"]}
    try:
        names = [e.name for e in os.scandir(folder) if e.is_file() and Path(e.name).suffix.lower() in exts]
    except OSError:
        return 0, 0
    prefixes = {series_prefix(n, patterns) for n in names} - {None}
    return len(names), len(prefixes)


def scan_episodes(root: Path, settings: dict, progress=None, cancelled=None) -> dict:
    """Check each series folder inside root (the original script's behavior).

    The chosen folder itself is checked too when it IS a series folder: no
    series subfolders, and all its files from one series. When it also holds
    loose files from several series (e.g. downloads not sorted yet), those are
    skipped, as the original script did, and "note" says so.
    Returns {"rows": [...], "note": "" or an explanation for the log}.
    """
    require_folder(root)
    patterns = compiled_patterns(settings)
    folders = folders_to_check(root, settings.get("nested", False), settings.get("old_versions_folder", ""))
    rows = []
    for n, folder in enumerate(folders):
        if cancelled and cancelled():
            break
        if progress:
            progress(n, len(folders), folder.name)
        row = check_folder(folder, root, settings, patterns)
        if row:
            rows.append(row)

    note = ""
    loose, series = loose_series_count(root, settings, patterns)
    if loose:
        if not rows and series <= 1:
            row = check_folder(root, root, settings, patterns)  # A single series folder.
            if row:
                rows.insert(0, row)
        elif rows:
            note = (f"{loose} file(s) directly in the chosen folder weren't checked: only the series "
                    f"folders inside it are. Sort them into folders first (Video Sorter).")
        else:
            note = (f"The {loose} file(s) in this folder belong to {series} different series, so they "
                    f"can't be checked as one. Sort them into folders first (Video Sorter).")
    return {"rows": rows, "note": note}


def old_version_jobs(rows: list[dict], folder_name: str) -> list[tuple[Path, Path]]:
    """Where each older version goes: an "Old versions" folder inside its own series folder."""
    name = sanitize_component(folder_name) or "Old versions"
    return [(Path(p), Path(p).parent / name / Path(p).name) for r in rows for p in r["old_paths"]]


def move_old_versions(jobs: list[tuple[Path, Path]], root: str, progress=None, cancelled=None):
    # A file already in "Old versions" with the same name is kept: the new one gets "(1)".
    return move_files(jobs, lambda d: (unique_path(d), "Moved (renamed)"), label="Episode Check clean-up",
                      root=root, output=root, progress=progress, cancelled=cancelled)


def text_report(rows: list[dict], root: str) -> str:
    """Same kind of lines the original wrote to reporte_episodios.txt."""
    lines = [f"Episode report for: {root}", created_line(), ""]
    for r in rows:
        if r["missing_list"]:
            lines.append(f"Warning: '{r['folder']}' is missing episodes: {r['missing_list']}")
        if r["dups_list"]:
            lines.append(f"Note: '{r['folder']}' has two files with the same episode and version: {r['dups_list']}")
        if r["old_files"]:
            lines.append(f"Note: '{r['folder']}' still has older versions of episodes {r['old_list']}:")
            lines += [f"    {f}" for f in r["old_files"]]
        if r["error"]:
            lines.append(f"Error: '{r['folder']}' could not be read: {r['error']}")
    if len(lines) == 3:
        lines.append("No missing episodes found.")
    return "\n".join(lines) + "\n"


# =============================================================== the tab

COLUMNS = [
    Column("status", "Status"),
    Column("folder", "Folder", stretch=True),
    Column("episodes", "Episodes", numeric=True),
    Column("range", "Range", sort_key="last", numeric=True),
    Column("missing", "Missing", sort_key="missing_count", numeric=True, width=220),
    Column("old", "Older versions", sort_key="old_count", numeric=True, width=120),
    Column("duplicates", "Duplicates", width=120),
    Column("unrecognized", "Unrecognized", numeric=True),
    Column("newest", "Newest file", sort_key="newest_ts", numeric=True),
]


class EpisodeCheckTab(ToolTab):
    title = "Episode Check"
    key = "episode_check"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = load_tool_settings(SETTINGS_KEY, DEFAULT_SETTINGS)
        # Patterns added in a later version show up at the end, turned off.
        known = {p["id"] for p in self.settings["patterns"]}
        self.settings["patterns"] += [{"id": i, "enabled": False} for i in PATTERN_ORDER if i not in known]
        self.scanned_root: Path | None = None
        self.history = HistoryStore(SETTINGS_KEY)  # Undo for "Clean up older versions".
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
        self.custom_edit.setPlaceholderText(r"Optional, tried first. Number in a group: #(\d+) or (?P<ep>\d+), season: (?P<season>\d+)")
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
        self.seasons_chk = QCheckBox("Count each season separately")
        self.seasons_chk.setToolTip("S01E05 and S02E05 are different episodes, not duplicates.")
        self.seasons_chk.setChecked(self.settings.get("split_seasons", True))
        # Off by default: the clean-up moves older versions aside (undoable) unless this is on.
        where = "to the Recycle Bin" if delete_is_recoverable() else "permanently"
        self.delete_chk = QCheckBox(f"Clean-up deletes older versions ({where})")
        self.delete_chk.setToolTip(
            "Off (default): older versions are moved to an \"Old versions\" folder inside the\n"
            "series folder, and Undo puts them back.\n"
            "On: they're deleted instead" + (
                ", to the Recycle Bin." if delete_is_recoverable() else
                " permanently. Install send2trash (pip install send2trash)\nto send them to the Recycle Bin instead."))
        self.delete_chk.setChecked(self.settings.get("delete_old_versions", False))
        for w in (self.start_chk, self.seasons_chk, self.nested_chk, self.complete_chk, self.delete_chk):
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
        self.b_clean = QPushButton("Clean up older versions…")
        self.b_clean.setToolTip("For every folder shown: move (or delete, if turned on) files that have a newer vN")
        self.b_clean.clicked.connect(lambda: self.clean_up(self.proxy.visible_rows()))
        self.b_undo = QPushButton("Undo last")
        self.b_undo.clicked.connect(self.undo_last)
        b_hist = QPushButton("History…")
        b_hist.clicked.connect(lambda: HistoryDialog(self).exec())
        b_csv = QPushButton("Export CSV…")
        b_csv.clicked.connect(self.export_csv)
        b_txt = QPushButton("Save report…")
        b_txt.setToolTip("Missing episodes per folder, like the original reporte_episodios.txt")
        b_txt.clicked.connect(self.export_txt)
        for b in (self.b_clean, self.b_undo, b_hist):
            bottom.addWidget(b)
        bottom.addStretch()
        bottom.addWidget(b_csv)
        bottom.addWidget(b_txt)
        self.add_open_last_buttons(bottom)
        lay.addLayout(bottom)

        QShortcut(QKeySequence("F5"), self, activated=self.start,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.on_idle()

    # ---------------------------------------------------------- helpers
    def on_idle(self):
        self.b_undo.setEnabled(bool(self.history.batches) and not self.busy)
        self.b_clean.setEnabled(any(r["old_files"] for r in self.model.rows) and not self.busy)

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
        self.settings["split_seasons"] = self.seasons_chk.isChecked()
        self.settings["delete_old_versions"] = self.delete_chk.isChecked()
        return True

    def update_summary(self):
        rows = self.model.rows
        missing = sum(1 for r in rows if r["missing_list"])
        eps = sum(r["missing_count"] for r in rows)
        dups = sum(1 for r in rows if r["dups_list"])
        old = sum(r["old_count"] for r in rows)
        text = (f"{len(rows)} folders,  {missing} with missing episodes ({eps} in total),  "
                f"{old} older version files,  {dups} with duplicates")
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
        if not root:
            QMessageBox.warning(self, "No folder", "Choose a folder first.")
            return
        if not self._read_options():
            return
        save_tool_settings(SETTINGS_KEY, self.settings)
        # Whether the folder exists is checked in the background task.

        def finished(result):
            self.scanned_root = root
            self.settings["recent_folders"] = remember_recent(self.settings["recent_folders"], root)
            save_tool_settings(SETTINGS_KEY, self.settings)
            self.folder.set_recent(self.settings["recent_folders"])
            self.folder.set_text(str(root))
            rows = result["rows"]
            self.model.set_rows(rows)
            self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
            self.update_summary()
            missing = [r for r in rows if r["missing_list"]]
            self.write_log(f"Checked {len(rows)} folder(s) in {root}: {len(missing)} with missing episodes.")
            if result["note"]:
                self.write_log(f"  {result['note']}")
            elif not rows:
                # Say why the table is empty instead of leaving it blank.
                exts = ", ".join(self.settings["extensions"])
                self.write_log(f"  No {exts} files were found in this folder or its subfolders. "
                               "Check the Extensions box, or turn on \"Also check folders inside folders\".")
            for r in missing:
                self.write_log(f"  {r['folder']}: missing {r['missing']}")
            old = sum(r["old_count"] for r in rows)
            if old:
                self.write_log(f"{old} older version file(s) can be cleaned up (Clean up older versions).")

        self.run_task(scan_episodes, (root, dict(self.settings)), finished, "Checking folders…")

    # ---------------------------------------------------------- clean-up of older versions
    def clean_up(self, rows: list[dict]):
        """Move older versions ("05" when "05v2" exists) into an "Old versions" folder,
        or delete them if that option is on. Moving is the default and can be undone."""
        if self.busy:
            return
        self._read_options()
        save_tool_settings(SETTINGS_KEY, self.settings)
        rows = [r for r in rows if r["old_paths"]]
        paths = [p for r in rows for p in r["old_paths"]]
        if not paths:
            return
        sample = "\n".join(f"  {Path(p).name}" for p in paths[:10])
        if len(paths) > 10:
            sample += f"\n  … and {len(paths) - 10} more"
        folder = self.settings.get("old_versions_folder", "Old versions")

        if self.settings.get("delete_old_versions"):
            where = ("They go to the Recycle Bin." if delete_is_recoverable()
                     else "This is PERMANENT: they can't be recovered, and Undo can't bring them back.")
            box = QMessageBox(QMessageBox.Icon.Warning, "Delete older versions",
                              f"Delete {len(paths)} older version file(s) from {len(rows)} folder(s)?\n"
                              f"{where}\n\n{sample}", parent=self)
            box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)  # Enter doesn't delete by accident.
            if box.exec() != QMessageBox.StandardButton.Yes:
                return

            def deleted(result):
                done, errors = result
                self.write_log(f"Deleted {done} older version file(s).")
                for e in errors:
                    self.write_log(f"  {e}")
                self.start()  # Show the folders as they are now.

            self.run_task(delete_files, ([Path(p) for p in paths],), deleted, f"Deleting {len(paths)} file(s)…")
            return

        if QMessageBox.question(self, "Clean up older versions",
                                f"Move {len(paths)} older version file(s) into an \"{folder}\" folder inside "
                                f"each of {len(rows)} series folder(s)?\nUndo puts them back.\n\n{sample}") \
                != QMessageBox.StandardButton.Yes:
            return
        jobs = old_version_jobs(rows, folder)

        def moved(result):
            batch, results = result
            ok = sum(1 for st, _ in results.values() if st.startswith("Moved"))
            self.write_log(f"Moved {ok} older version file(s) into \"{folder}\" folders.")
            for src, (st, _) in results.items():
                if not st.startswith("Moved"):
                    self.write_log(f"  {st}: {src}")
            if batch:
                self.history.add(batch)
            self.start()

        self.run_task(move_old_versions, (jobs, str(self.scanned_root or "")), moved,
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
            self.write_log(f"Put back {ok} file(s).")
            for e in errors:
                self.write_log(f"  {e}")
            if after:
                after()
            if self.scanned_root:
                self.start()

        self.run_task(revert_batch, (batch,), finished, f"Reverting {n} file(s)…")

    def export_csv(self):
        rows = self.proxy.visible_rows()
        if not rows:
            return
        path = self.ask_save("Export CSV", "episode_check.csv", "CSV files (*.csv)")
        if path:
            self.save_in_background(export_csv, (path, COLUMNS + [Column("path", "Full path")], rows,
                                                 [["Folder checked", str(self.scanned_root or "")]]),
                                    path, f"{len(rows)} row(s)")

    def export_txt(self):
        if not self.model.rows:
            return
        path = self.ask_save("Save report", "episode_report.txt", "Text files (*.txt)")
        if path:
            self.save_in_background(save_text, (path, text_report, list(self.model.rows),
                                                str(self.scanned_root or "")), path, "the report")

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
        a = m.addAction("Copy older version file names",
                        lambda: QApplication.clipboard().setText(
                            "\n".join(f for r in rows for f in r["old_files"])))
        a.setEnabled(any(r["old_files"] for r in rows))
        if len(rows) == 1 and first["old_files"]:
            old_path = Path(first["path"]) / first["old_files"][0]
            m.addAction("Show an older version file", lambda: self.safe_open(old_path, reveal=True))
        a = m.addAction("Copy unrecognized file names",
                        lambda: QApplication.clipboard().setText(
                            "\n".join(f for r in rows for f in r["unrecognized_list"])))
        a.setEnabled(any(r["unrecognized_list"] for r in rows))
        m.addAction("Copy folder path" + ("s" if len(rows) > 1 else ""),
                    lambda: QApplication.clipboard().setText("\n".join(r["path"] for r in rows)))
        m.addSeparator()
        n_old = sum(r["old_count"] for r in rows)
        verb = "Delete" if self.delete_chk.isChecked() else "Move aside"
        a = m.addAction(f"{verb} older versions in selection ({n_old})…", lambda: self.clean_up(rows))
        a.setEnabled(n_old > 0 and not self.busy)
        self.add_link_actions(m, Path(first["path"]))
        m.exec(self.table.viewport().mapToGlobal(pos))


if __name__ == "__main__":
    run_standalone(EpisodeCheckTab)
