#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Video Sorter - organizes episode and movie files into folders.
(Called "Folder Sorter" in earlier versions.)

Workflow:
    1. Pick a source folder and press Scan. This only builds a preview;
       nothing is moved yet.
    2. Review the table. Edit destinations, exclude files, or add rules.
    3. Press Organize. Files are moved into subfolders of the output folder
       (the source folder itself, unless you choose a different one).

Files with no episode number that run longer than a set time (60 minutes by
default) are treated as movies, which catches movies whose name doesn't say
"Movie". This needs pymediainfo (optional, same as the Video Check tab).

Every Organize run is saved to a history file, so it can be reverted later.
Part of Media Toolbox; can also be run on its own:  python video_sorter.py
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu,
    QMessageBox, QPushButton, QSpinBox, QTableView, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from common import (
    ERROR_COLOR, OK_COLOR, DIM_COLOR, FolderPicker, HistoryDialog, HistoryStore, ToolTab,
    _is_checked, config_dir, require_folder, human_duration, legacy_config_path, load_tool_settings, move_files, remember_recent,
    revert_batch, run_standalone, sanitize_component, save_tool_settings, unique_path,
)

# pymediainfo is optional: it's only used to find movies by their length.
try:
    from pymediainfo import MediaInfo
except ImportError:  # pragma: no cover - depends on the machine
    MediaInfo = None

# =============================================================== settings

SETTINGS_KEY = "video_sorter"

# Folders offered in the "recent" dropdown on first launch. Only the ones that
# exist on the current machine are shown. Edit or empty this list before
# sharing the script; after the first run the list lives in the settings file.
STARTER_FOLDERS = [
    "/run/media/ulisesgm/201F75C43B6E19C1/Temporal/Seguidos",
    r"E:\Videos\Anime",
    r"D:\Temporal\Seguidos",
    r"F:\Temporal\Videos\Torrent",
    r"E:\Videos\Anime\Incompleto",
    r"Z:\Videos\Series\Anime\Torrent",
]

MODES = [("word", "Whole word"), ("contains", "Contains"), ("regex", "Regex")]

# ---------------------------------------------------------------- name detectors
# Each detector is a regex that finds the series name in a file name. The name
# must be in a group called "name" (or the first group, for custom regexes).
#   target: "filename" matches the full name, "stem" the name without extension.
#   clean:  tidy the captured text (underscores/dots -> spaces, trim " - _ .").
# Anything in [brackets] at the very start is the release group; most
# detectors skip it with LEADING_GROUP so it doesn't end up in the folder name.
LEADING_GROUP = r"(?:\[[^\]]*\][\s_.]*)?"

BUILTIN_DETECTORS = {
    # The pattern from the original script, unchanged and not cleaned, so it
    # produces exactly the same folder names as before.
    "original": {
        "label": "Title - 01 [Group]  (original)",
        "example": "Frieren - 05 (1080p) [ABCD1234].mkv",
        "regex": r"^(?P<name>.*?)(?:\s*-\s*\d+(?:v\d+)?\s*(?:\(.*?\))?\s*\[.*?\].*\..+)$",
        "target": "filename",
        "clean": False,
    },
    # Same dash style, but the ending can be anything, e.g. no [CRC] at the end.
    "dash": {
        "label": "[Group] Title - 01  (any ending)",
        "example": "[SubsPlease] Frieren - 05 (1080p).mkv",
        "regex": rf"(?i)^{LEADING_GROUP}(?P<name>.+?)\s+-\s+(?:(?:ep|episode)\s*)?\d{{1,4}}(?:v\d+)?(?!\d)",
        "target": "stem",
        "clean": True,
    },
    # Underscore/dot separated, episode number followed by [CRC], (info) or the end.
    # The number must be followed by a bracket or the end, so "_1080p" is never
    # mistaken for an episode.
    "underscore": {
        "label": "[Group]_Title_01_[CRC]",
        "example": "[Rakuen]_Bounen_no_Xamdou_02_[64BEA878].mkv",
        "regex": rf"^{LEADING_GROUP}(?P<name>.+?)[\s_.]+(?:-[\s_.]+)?\d{{1,4}}(?:v\d+)?(?:[\s_.]*[\[(].*)?$",
        "target": "stem",
        "clean": True,
    },
    # Western TV style.
    "sxxexx": {
        "label": "Title S01E02",
        "example": "Breaking.Bad.S01E02.720p.mkv",
        "regex": rf"(?i)^{LEADING_GROUP}(?P<name>.+?)[\s_.\-]+S\d{{1,2}}[\s_.]?E\d{{1,4}}",
        "target": "stem",
        "clean": True,
    },
    "nxnn": {
        "label": "Title 1x02",
        "example": "Doctor Who 4x03.mkv",
        "regex": rf"(?i)^{LEADING_GROUP}(?P<name>.+?)[\s_.\-]+\d{{1,2}}x\d{{2,3}}(?!\d)",
        "target": "stem",
        "clean": True,
    },
    # Spelled-out episode markers. "Episodio", "Cap", "Capitulo" cover Spanish releases.
    "episode": {
        "label": "Title Episode 02 / Episodio 02 / Ep 02 / Cap 02",
        "example": "Mushishi Episode 03.mkv",
        "regex": rf"(?i)^{LEADING_GROUP}(?P<name>.+?)[\s_.\-]+(?:episode|episodio|ep|cap[ií]tulo|cap)[\s_.\-]*\d{{1,4}}(?!\d)",
        "target": "stem",
        "clean": True,
    },
    # One-off releases that belong with their series rather than in Movies:
    # "[Erai-raws] The Ribbon Hero - ONA [1080p]" -> "The Ribbon Hero".
    "special": {
        "label": "Title - OVA / ONA / OAD / Special",
        "example": "[Erai-raws] The Ribbon Hero - ONA [1080p][DF480D33].mkv",
        "regex": rf"(?i)^{LEADING_GROUP}(?P<name>.+?)\s+-\s+(?:OVA|ONA|OAD|OAV|Specials?|SP)(?:\s*\d{{1,3}})?(?:v\d+)?\s*(?:[\[(].*)?$",
        "target": "stem",
        "clean": True,
    },
}
# Default order: the original first, then specific markers (S01E02, 1x02,
# "Episode"), and the loose underscore pattern last, since it's the most
# general and would otherwise grab names like "Mushishi Episode 03".
DETECTOR_ORDER = ["original", "dash", "sxxexx", "nxnn", "episode", "special", "underscore"]

DEFAULT_SETTINGS = {
    "recent_folders": STARTER_FOLDERS,
    "output_folder": "",            # Empty = organize inside the scanned folder.
    "extensions": [".mkv", ".mp4", ".avi"],
    # The original only sorted files directly in the folder; subfolders are optional.
    "recursive": False,
    # Rules are checked top to bottom; the first one that matches wins.
    "rules": [
        {"enabled": True, "pattern": "movie", "mode": "word", "destination": "Movies"},
    ],
    # Name detectors, tried in this order (see BUILTIN_DETECTORS below).
    # The original pattern stays first, so it wins whenever it matches.
    "detectors": [{"id": i, "enabled": True} for i in DETECTOR_ORDER],
    "strip_group_tag": False,       # "[SubsPlease] Frieren" -> "Frieren" for every detector.
    "merge_similar_folders": True,  # Reuse "Bounen no Xamdou" instead of creating "bounen no xamdou".
    # Files with no episode number that are at least this long are movies.
    "movie_by_duration": True,
    "movie_minutes": 60,
    "movie_folder": "Movies",
    "default_enabled": False,       # Where files go when nothing else matches.
    "default_folder": "Unsorted",
    "on_conflict": "skip",          # skip | rename
}


def _first_existing(*paths: Path) -> Path | None:
    return next((p for p in paths if p.exists()), None)


# Settings and history from before the rename ("Folder Sorter"), imported the first time.
LEGACY_SETTINGS = (config_dir() / "folder_sorter.json", legacy_config_path("FolderSorter", "settings.json"))
LEGACY_HISTORY = (config_dir() / "folder_sorter_history.json", legacy_config_path("FolderSorter", "history.json"))


def load_settings() -> dict:
    s = load_tool_settings(SETTINGS_KEY, DEFAULT_SETTINGS, legacy=_first_existing(*LEGACY_SETTINGS))
    # The very first version had a single on/off "series_detection".
    if s.get("series_detection") is False and s.get("detectors") == DEFAULT_SETTINGS["detectors"]:
        s["detectors"] = [{"id": i, "enabled": False} for i in DETECTOR_ORDER]
    s.pop("series_detection", None)
    # Built-in detectors added in a later version go at the end, turned on. Being
    # last, they only see files that no earlier pattern matched, so files that
    # were already being sorted keep going to the same folders.
    known = {d.get("id") for d in s["detectors"]}
    s["detectors"] = list(s["detectors"]) + [
        {"id": i, "enabled": True} for i in DETECTOR_ORDER if i not in known]
    return s


# =============================================================== core logic (no Qt here)

def tidy_name(name: str) -> str:
    """'Bounen_no_Xamdou' -> 'Bounen no Xamdou', 'Breaking.Bad' -> 'Breaking Bad'."""
    name = name.replace("_", " ")
    # Dots only count as spaces when there are no real spaces, so "Mr. Robot" survives.
    if " " not in name.strip():
        name = name.replace(".", " ")
    return re.sub(r"\s+", " ", name).strip(" -_.~")


def strip_group(name: str) -> str:
    """Remove leading [tags]: '[SubsPlease] Frieren' -> 'Frieren'."""
    return re.sub(r"^(?:\s*\[[^\]]*\]\s*)+", "", name).strip() or name


def folder_key(name: str) -> str:
    """Comparison key that ignores case, spaces, punctuation and a leading [Group]
    tag, used to merge 'Bounen no Xamdou', 'bounen_no_xamdou' and
    '[Erai-raws] Bounen no Xamdou' into one folder. The group tag matters because
    the original pattern keeps it, while the backup patterns drop it."""
    return re.sub(r"[\W_]+", "", strip_group(name).casefold())


def compiled_detectors(settings: dict) -> list[tuple[str, re.Pattern, str, bool]]:
    """Enabled detectors in order, as (label, regex, target, clean).
    Broken custom regexes are skipped instead of crashing the scan."""
    out = []
    for d in settings.get("detectors", []):
        if not d.get("enabled", True):
            continue
        if d.get("id") in BUILTIN_DETECTORS:
            spec = BUILTIN_DETECTORS[d["id"]]
            flags = 0
        elif d.get("id") == "custom":
            spec = {"label": d.get("label") or "Custom", "regex": d.get("regex", ""),
                    "target": "stem", "clean": True}
            flags = re.IGNORECASE
        else:
            continue
        try:
            rx = re.compile(spec["regex"], flags)  # re caches compiled patterns.
        except re.error:
            continue
        out.append((spec["label"], rx, spec["target"], spec["clean"]))
    return out


def detect_name(filename: str, settings: dict) -> tuple[str | None, str | None]:
    """Try each enabled detector in order. Returns (series name, detector label)."""
    stem = Path(filename).stem
    for label, rx, target, clean in compiled_detectors(settings):
        m = rx.search(filename if target == "filename" else stem)
        if not m:
            continue
        if "name" in rx.groupindex:
            raw = m.group("name")
        elif rx.groups:
            raw = m.group(1)
        else:
            continue  # A regex without a group can't tell us the name.
        if not raw:
            continue
        name = tidy_name(raw) if clean else raw
        if settings.get("strip_group_tag"):
            name = strip_group(name)
        name = sanitize_component(name)
        # A "name" with no letters ("01" from "01-Episodio-1.mkv") isn't a series title.
        if name and re.search(r"[^\W\d_]", name):
            return name, label
    return None, None


def clean_title(filename: str, settings: dict) -> str:
    """Readable title used for the {name} placeholder in destinations."""
    s, _ = detect_name(filename, settings)
    if s:
        return s
    stem = Path(filename).stem
    # Drop [release group], (resolution), {tags}.
    t = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", stem)
    # "Movie.Title.2019.1080p" style names use dots or underscores as spaces.
    # Only convert them when the name has no real spaces, so "Mr. Robot" survives.
    if " " not in stem.strip():
        t = re.sub(r"[._]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -_.")
    return sanitize_component(t) or sanitize_component(stem)


def rule_matches(rule: dict, text: str) -> bool:
    pattern = rule.get("pattern", "")
    if not pattern:
        return False
    mode = rule.get("mode", "word")
    if mode == "contains":
        return pattern.lower() in text.lower()
    if mode == "regex":
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error:
            return False  # A broken regex just never matches instead of crashing.
    # Whole word. Unlike \b, this treats "_" and "." as separators, so
    # "Title_Movie_1080p" matches "movie" but "Moviestar" does not.
    return re.search(rf"(?<![^\W_]){re.escape(pattern)}(?![^\W_])", text, re.IGNORECASE) is not None


def render_destination(template: str, filename: str, settings: dict) -> str:
    """Turn a destination template into a safe relative path.

    Supports {name} and subfolders, e.g. "Movies/{name}". Always returns
    forward slashes; "." and ".." are dropped so a rule can't escape the
    output folder.
    """
    text = template.replace("{name}", clean_title(filename, settings))
    parts = [sanitize_component(p) for p in re.split(r"[\\/]+", text)]
    return "/".join(p for p in parts if p and p not in (".", ".."))


def decide(filename: str, settings: dict) -> tuple[str, str]:
    """Return (relative destination folder, reason shown in the table)."""
    stem = Path(filename).stem
    # 1. User rules, in order.
    for rule in settings.get("rules", []):
        if rule.get("enabled", True) and rule_matches(rule, stem):
            dest = render_destination(rule.get("destination", ""), filename, settings)
            if dest:
                return dest, f"Rule: {rule['pattern']}"
    # 2. Series name detectors, in order.
    name, label = detect_name(filename, settings)
    if name:
        return name, f"Series: {label}"
    # 3. Catch-all default folder, if enabled.
    if settings.get("default_enabled") and settings.get("default_folder", "").strip():
        dest = render_destination(settings["default_folder"], filename, settings)
        if dest:
            return dest, "Default folder"
    return "", "No match"


def merge_similar(dests: list[str], existing: list[str]) -> list[str]:
    """Point near-duplicate folder names at one spelling.

    dests: planned relative destinations. existing: folder names already in
    the output folder. The first spelling seen wins, and existing folders are
    seen first, so files join folders you already have.
    """
    known = {}
    for name in existing:
        known.setdefault(folder_key(name), name)
    out = []
    for dest in dests:
        if not dest:
            out.append(dest)
            continue
        first, sep, rest = dest.partition("/")
        key = folder_key(first)
        if key:
            first = known.setdefault(key, first)
        out.append(first + sep + rest)
    return out


def scan_folder(root: Path, output_root: Path, settings: dict, manual: dict,
                progress=None, cancelled=None) -> dict:
    """Build the preview for one folder. Runs in the background: listing a big
    or network folder can take a while, and the window must stay usable.

    manual: {relative path: PlanItem} edited by hand earlier; they're kept as they are.
    Returns {"items": [PlanItem], "existing": [folder names already in the output],
             "output_exists": bool, "already_sorted": files left out because they're
             already in the folder they'd go to (only possible with subfolders)}.
    """
    if progress:
        progress(0, 0, f"Reading {root}…")
    require_folder(root)

    exts = tuple(e.lower() for e in settings.get("extensions", [".mkv"]))
    files = list_source_files(root, exts, settings.get("recursive", False), output_root,
                              settings.get("skip_folders", SKIP_FOLDERS), cancelled)

    items = []
    for n, rel in enumerate(files):
        if cancelled and cancelled():
            break
        name = Path(rel).name
        if progress and n % 100 == 0:
            progress(n, len(files), name)
        if rel in manual:
            items.append(manual[rel])
            continue
        dest, reason = decide(name, settings)
        items.append(PlanItem(name, dest, reason, include=bool(dest), current_path=root / rel, rel=rel))

    existing = []
    output_exists = output_root.is_dir()
    if output_exists:
        try:
            existing = sorted(e.name for e in os.scandir(output_root) if e.is_dir())
        except OSError:
            pass

    # Different release groups spell the same series differently
    # ("Bounen no Xamdou" vs "bounen_no_xamdou"): send them all to one folder,
    # preferring a folder that already exists in the output.
    if settings.get("merge_similar_folders", True):
        auto = [i for i in items if not i.manual]
        for i, d in zip(auto, merge_similar([i.destination for i in auto], existing)):
            i.destination = d

    # With subfolders, the library's own series folders get scanned too. A file
    # that's already in the folder it would go to is left out: nothing to do.
    already = 0
    if settings.get("recursive", False):
        keep = []
        for i in items:
            if i.destination and not i.manual and _same_folder(i.current_path.parent, output_root / i.destination):
                already += 1
            else:
                keep.append(i)
        items = keep
    return {"items": items, "existing": existing, "output_exists": output_exists, "already_sorted": already}


# Subfolders never scanned: "Old versions" holds what Episode Check set aside on
# purpose; sorting those files would put outdated episodes back in the series folder.
SKIP_FOLDERS = ["Old versions"]


def _same_folder(a: Path, b: Path) -> bool:
    """Same folder? Case-insensitive on Windows, where "Frieren" and "frieren" are one folder."""
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def list_source_files(root: Path, exts: tuple, recursive: bool, output_root: Path,
                      skip_names: list[str], cancelled=None) -> list[str]:
    """Video files to sort, as paths relative to root ("file.mkv", "Sub/file.mkv").

    Without subfolders: only files directly in root (the original behavior).
    With subfolders: every folder below root, except folders named in skip_names
    and the output folder when it's a separate folder inside root (its files are
    already sorted).
    """
    if not recursive:
        return sorted(e.name for e in os.scandir(root) if e.is_file() and e.name.lower().endswith(exts))
    skip = {n.casefold() for n in skip_names}
    out = []
    for current, dirs, names in os.walk(root):
        if cancelled and cancelled():
            break
        here = Path(current)
        dirs[:] = sorted(d for d in dirs
                         if d.casefold() not in skip
                         and not (output_root != root and _same_folder(here / d, output_root)))
        rel_dir = here.relative_to(root)
        out.extend(str(rel_dir / n) if str(rel_dir) != "." else n
                   for n in names if n.lower().endswith(exts))
    return sorted(out, key=str.lower)


def probe_durations(paths: list[Path], progress=None, cancelled=None) -> dict[str, float | None]:
    """Length in seconds of each file, read with MediaInfo ({path: seconds or None})."""
    out: dict[str, float | None] = {}
    for n, p in enumerate(paths):
        if cancelled and cancelled():
            break
        if progress:
            progress(n, len(paths), f"Checking length: {p.name}")
        seconds = None
        try:
            general = next((t for t in MediaInfo.parse(str(p)).tracks if t.track_type == "General"), None)
            if general is not None and general.duration:
                seconds = float(general.duration) / 1000
        except Exception:  # noqa: BLE001 - unreadable files just stay unmatched
            seconds = None
        out[str(p)] = seconds
    return out


def execute_moves(source_root: Path, output_root: Path, jobs: list[tuple[str, str]],
                  on_conflict: str, progress=None, cancelled=None):
    """Move files from source_root into subfolders of output_root.

    jobs: [(path relative to source_root, relative destination folder)].
    Returns (batch for the history file or None, {relative path: (status, new path or None)}).
    """
    # Checked here, in the background, rather than before starting (slow on network drives).
    if output_root.exists() and not output_root.is_dir():
        raise NotADirectoryError(f"The output '{output_root}' is a file, not a folder.")
    abs_jobs = [(source_root / rel, output_root / dest / Path(rel).name) for rel, dest in jobs]
    # "rename" keeps both files as "name (1).mkv"; "skip" leaves the source untouched.
    on_exists = (lambda d: (unique_path(d), "Moved (renamed)")) if on_conflict == "rename" else None
    batch, results = move_files(abs_jobs, on_exists, label="Video Sorter",
                                root=str(source_root), output=str(output_root),
                                progress=progress, cancelled=cancelled)
    # Results are keyed by full source path; the tab works with paths relative to the source.
    by_rel = {str(source_root / rel): rel for rel, _dest in jobs}
    return batch, {by_rel.get(k, k): v for k, v in results.items()}


# =============================================================== table model

@dataclass
class PlanItem:
    """One row of the preview table."""
    filename: str
    destination: str          # Relative to the output folder; "" = nowhere to go.
    reason: str               # Rule / Series / Default folder / Manual / No match
    include: bool
    current_path: Path        # Where the file is right now (changes after moving).
    status: str = "Pending"
    manual: bool = False      # Edited by hand; kept when rescanning the same folder.
    rel: str = ""             # Path relative to the scanned folder ("Sub/file.mkv"); the file's key.

    def __post_init__(self):
        self.rel = self.rel or self.filename  # Files directly in the folder: just the name.

    @property
    def from_folder(self) -> str:
        """The subfolder the file is in, "" for files directly in the scanned folder."""
        parent = str(Path(self.rel).parent)
        return "" if parent == "." else parent


COLS = ["", "File", "Destination", "Reason", "Status", "In subfolder"]
C_INC, C_FILE, C_DEST, C_REASON, C_STATUS, C_FROM = range(6)


class PlanModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.items: list[PlanItem] = []
        self.output_root: Path | None = None  # Only used for the Destination tooltip.
        self.settings: dict = {}              # Needed to expand {name} when editing inline.

    def set_items(self, items: list[PlanItem]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def refresh_row(self, row: int) -> None:
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLS) - 1))

    def refresh_all(self) -> None:
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, len(COLS) - 1))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLS[section]
        return None

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        it = self.items[index.row()]
        # Rows that were already moved are read-only.
        if index.column() == C_INC and it.status == "Pending":
            f |= Qt.ItemFlag.ItemIsUserCheckable
        if index.column() == C_DEST and it.status == "Pending":
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        it, c = self.items[index.row()], index.column()
        R = Qt.ItemDataRole
        if role == R.CheckStateRole and c == C_INC:
            return Qt.CheckState.Checked if it.include else Qt.CheckState.Unchecked
        if role == R.DisplayRole:
            return [None, it.filename, it.destination or "-", it.reason, it.status, it.from_folder][c]
        if role == R.EditRole and c == C_DEST:
            return it.destination
        if role == R.UserRole:
            # Sort keys: the proxy sorts by this role, so the checkbox column sorts too
            # and text sorts case-insensitively.
            return [int(it.include), it.filename.lower(), it.destination.lower(),
                    it.reason.lower(), it.status.lower()][c]
        if role == R.ToolTipRole:
            if c == C_DEST and it.destination and it.status == "Pending" and self.output_root:
                return str(self.output_root / it.destination)
            return str(it.current_path)
        if role == R.ForegroundRole:
            if it.status.startswith("Moved"):
                return QColor(OK_COLOR)
            if it.status.startswith("Error") or it.status == "Missing":
                return QColor(ERROR_COLOR)
            if it.status != "Pending" or it.reason == "No match" or not it.include:
                return QColor(DIM_COLOR)
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        it = self.items[index.row()]
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == C_INC:
            # A file with no destination can't be included.
            it.include = _is_checked(value) and bool(it.destination)
        elif role == Qt.ItemDataRole.EditRole and index.column() == C_DEST:
            it.destination = render_destination(str(value), it.filename, self.settings)
            it.reason, it.manual = "Manual", True
            it.include = bool(it.destination)
        else:
            return False
        self.refresh_row(index.row())
        return True


# =============================================================== settings dialog

class RulesDialog(QDialog):
    """Three tabs (folder rules, name detection, general) plus a live test box
    that shows where a file name would go with the settings as currently edited."""

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.base = settings
        self.setWindowTitle("Rules & settings")
        self.resize(820, 620)
        lay = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_rules_tab(settings), "Folder rules")
        tabs.addTab(self._build_detect_tab(settings), "Name detection")
        tabs.addTab(self._build_general_tab(settings), "General")
        lay.addWidget(tabs, 1)

        # --- live test
        test = QFormLayout()
        self.test_edit = QLineEdit()
        self.test_edit.setPlaceholderText("Paste a file name (with extension) to see where it would go")
        self.test_edit.setClearButtonEnabled(True)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        self.test_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        test.addRow("Try a file name:", self.test_edit)
        test.addRow("", self.test_result)
        lay.addLayout(test)
        self.test_edit.textChanged.connect(self._update_test)
        self.rules_table.itemChanged.connect(self._update_test)
        self.det_table.itemChanged.connect(self._update_test)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # ------------------------------------------------------------ tab: folder rules
    def _build_rules_tab(self, settings: dict) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Rules are checked first, top to bottom; the first match wins. Matching uses the file name "
            "without extension.\nDestination can use {name} (the detected title) and subfolders, "
            "e.g. Movies/{name}."))

        self.rules_table = t = QTableWidget(0, 4)
        t.setHorizontalHeaderLabels(["On", "Match text", "Mode", "Destination folder"])
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        lay.addWidget(t)
        for r in settings.get("rules", []):
            self._add_rule_row(r)

        btns = QHBoxLayout()
        for text, slot in [
            ("Add rule", lambda: self._add_rule_row({}, select=True)),
            ("Remove", lambda: self._remove_row(self.rules_table)),
            ("Move up", lambda: self._move_row(self.rules_table, self._all_rules, self._add_rule_row, -1)),
            ("Move down", lambda: self._move_row(self.rules_table, self._all_rules, self._add_rule_row, 1)),
        ]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch()
        lay.addLayout(btns)
        return w

    def _add_rule_row(self, r: dict, select=False):
        t = self.rules_table
        t.blockSignals(True)  # Half-built rows would confuse the live test.
        row = t.rowCount()
        t.insertRow(row)
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        chk.setCheckState(Qt.CheckState.Checked if r.get("enabled", True) else Qt.CheckState.Unchecked)
        t.setItem(row, 0, chk)
        t.setItem(row, 1, QTableWidgetItem(r.get("pattern", "")))
        combo = QComboBox()
        for key, label in MODES:
            combo.addItem(label, key)
        combo.setCurrentIndex(max(0, combo.findData(r.get("mode", "word"))))
        combo.currentIndexChanged.connect(self._update_test)
        t.setCellWidget(row, 2, combo)
        t.setItem(row, 3, QTableWidgetItem(r.get("destination", "")))
        t.blockSignals(False)
        if select:
            t.selectRow(row)
            t.editItem(t.item(row, 1))

    def _all_rules(self) -> list[dict]:
        t, out = self.rules_table, []
        for row in range(t.rowCount()):
            if not (t.item(row, 0) and t.item(row, 1) and t.item(row, 3) and t.cellWidget(row, 2)):
                continue
            out.append({
                "enabled": t.item(row, 0).checkState() == Qt.CheckState.Checked,
                "pattern": t.item(row, 1).text().strip(),
                "mode": t.cellWidget(row, 2).currentData(),
                "destination": t.item(row, 3).text().strip(),
            })
        return out

    # ------------------------------------------------------------ tab: name detection
    def _build_detect_tab(self, settings: dict) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "When no folder rule matches, these patterns look for the series name, top to bottom; "
            "the first one that finds a name wins.\nCustom patterns are regexes matched against the "
            "file name without extension. Put the name in a group called (?P<name>...), or in the first group."))

        self.det_table = t = QTableWidget(0, 3)
        t.setHorizontalHeaderLabels(["On", "Pattern", "Example  /  regex for custom patterns"])
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        lay.addWidget(t)
        for d in settings.get("detectors", []):
            self._add_det_row(d)

        btns = QHBoxLayout()
        for text, slot in [
            ("Add custom pattern", lambda: self._add_det_row(
                {"id": "custom", "enabled": True, "label": "My pattern",
                 "regex": r"^(?P<name>.+?)[\s_.]+\d{1,4}$"}, select=True)),
            ("Remove", self._remove_det),
            ("Move up", lambda: self._move_row(self.det_table, self._all_detectors, self._add_det_row, -1)),
            ("Move down", lambda: self._move_row(self.det_table, self._all_detectors, self._add_det_row, 1)),
        ]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch()
        lay.addLayout(btns)

        self.strip_chk = QCheckBox("Remove a leading [Group] tag from folder names (also for the original pattern)")
        self.strip_chk.setChecked(settings.get("strip_group_tag", False))
        self.strip_chk.toggled.connect(self._update_test)
        self.merge_chk = QCheckBox("Use an existing folder when a name only differs in case, spaces or punctuation")
        self.merge_chk.setChecked(settings.get("merge_similar_folders", True))
        lay.addWidget(self.strip_chk)
        lay.addWidget(self.merge_chk)
        return w

    def _add_det_row(self, d: dict, select=False):
        did = d.get("id")
        builtin = BUILTIN_DETECTORS.get(did)
        if not builtin and did != "custom":
            return  # Unknown id from a newer/older version: ignore it.
        t = self.det_table
        t.blockSignals(True)
        row = t.rowCount()
        t.insertRow(row)
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        chk.setCheckState(Qt.CheckState.Checked if d.get("enabled", True) else Qt.CheckState.Unchecked)
        t.setItem(row, 0, chk)

        label = QTableWidgetItem(builtin["label"] if builtin else d.get("label") or "Custom")
        label.setData(Qt.ItemDataRole.UserRole, did)  # Remember which detector this row is.
        third = QTableWidgetItem(builtin["example"] if builtin else d.get("regex", ""))
        if builtin:
            # Built-ins are read-only: only their on/off state and order can change.
            for item in (label, third):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            third.setForeground(QColor("#8a8a8a"))
            third.setToolTip(f"Regex: {builtin['regex']}")
        else:
            label.setToolTip("Double-click to rename")
            third.setToolTip("Double-click to edit the regex")
        t.setItem(row, 1, label)
        t.setItem(row, 2, third)
        t.blockSignals(False)
        if select:
            t.selectRow(row)
            t.editItem(third)

    def _all_detectors(self) -> list[dict]:
        t, out = self.det_table, []
        for row in range(t.rowCount()):
            if not (t.item(row, 0) and t.item(row, 1) and t.item(row, 2)):
                continue
            did = t.item(row, 1).data(Qt.ItemDataRole.UserRole)
            d = {"id": did, "enabled": t.item(row, 0).checkState() == Qt.CheckState.Checked}
            if did == "custom":
                d["label"] = t.item(row, 1).text().strip() or "Custom"
                d["regex"] = t.item(row, 2).text().strip()
            out.append(d)
        return out

    def _remove_det(self):
        row = self.det_table.currentRow()
        if row < 0:
            return
        if self.det_table.item(row, 1).data(Qt.ItemDataRole.UserRole) != "custom":
            QMessageBox.information(self, "Built-in pattern",
                                    "Built-in patterns can't be removed. Uncheck it to turn it off.")
            return
        self.det_table.removeRow(row)
        self._update_test()

    # ------------------------------------------------------------ tab: general
    def _build_general_tab(self, settings: dict) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        row = QHBoxLayout()
        self.default_chk = QCheckBox("Move unmatched files to")
        self.default_chk.setChecked(settings.get("default_enabled", False))
        self.default_edit = QLineEdit(settings.get("default_folder", "Unsorted"))
        self.default_edit.setEnabled(self.default_chk.isChecked())
        self.default_chk.toggled.connect(self.default_edit.setEnabled)
        self.default_chk.toggled.connect(self._update_test)
        self.default_edit.textChanged.connect(self._update_test)
        row.addWidget(self.default_chk)
        row.addWidget(self.default_edit)
        form.addRow(row)

        self.ext_edit = QLineEdit(", ".join(settings.get("extensions", [])))
        form.addRow("File extensions:", self.ext_edit)

        self.conflict_combo = QComboBox()
        self.conflict_combo.addItem("Skip the file", "skip")
        self.conflict_combo.addItem("Rename as \"file (1).mkv\"", "rename")
        self.conflict_combo.setCurrentIndex(
            max(0, self.conflict_combo.findData(settings.get("on_conflict", "skip"))))
        form.addRow("If the file already exists:", self.conflict_combo)

        # Movies whose name doesn't say "Movie": no episode number + long running time.
        mrow = QHBoxLayout()
        self.movie_chk = QCheckBox("Files with no episode number that run at least")
        self.movie_chk.setChecked(settings.get("movie_by_duration", True))
        self.movie_min = QSpinBox()
        self.movie_min.setRange(1, 600)
        self.movie_min.setSuffix(" min")
        self.movie_min.setValue(int(settings.get("movie_minutes", 60)))
        self.movie_edit = QLineEdit(settings.get("movie_folder", "Movies"))
        mrow.addWidget(self.movie_chk)
        mrow.addWidget(self.movie_min)
        mrow.addWidget(QLabel("go to"))
        mrow.addWidget(self.movie_edit)
        for wdg in (self.movie_min, self.movie_edit):
            wdg.setEnabled(self.movie_chk.isChecked())
            self.movie_chk.toggled.connect(wdg.setEnabled)
        form.addRow(mrow)
        if MediaInfo is None:
            self.movie_chk.setEnabled(False)
            note = QLabel("Needs pymediainfo:  pip install pymediainfo")
            note.setStyleSheet(f"color: {DIM_COLOR};")
            form.addRow(note)
        return w

    # ------------------------------------------------------------ shared helpers
    def _remove_row(self, table: QTableWidget):
        row = table.currentRow()
        if row >= 0:
            table.removeRow(row)
            self._update_test()

    def _move_row(self, table: QTableWidget, getter, adder, delta: int):
        row = table.currentRow()
        target = row + delta
        if row < 0 or not (0 <= target < table.rowCount()):
            return
        # Rebuilding is simpler than swapping cell widgets (the mode combos) in place.
        data = getter()
        data[row], data[target] = data[target], data[row]
        table.setRowCount(0)
        for d in data:
            adder(d)
        table.selectRow(target)
        self._update_test()

    def _update_test(self, *_):
        name = self.test_edit.text().strip().strip('"')
        if not name:
            self.test_result.setText("")
            return
        dest, reason = decide(name, self.result_settings(self.base))
        if dest:
            self.test_result.setText(f"Goes to:  {dest}      ({reason})")
        else:
            self.test_result.setText("No match: the file would stay where it is.")

    def accept(self):
        # Catch broken custom regexes here rather than silently ignoring them later.
        for d in self._all_detectors():
            if d["id"] != "custom" or not d["regex"]:
                continue
            try:
                rx = re.compile(d["regex"])
            except re.error as e:
                QMessageBox.warning(self, "Invalid pattern", f"'{d['label']}' isn't a valid regex:\n{e}")
                return
            if "name" not in rx.groupindex and rx.groups == 0:
                QMessageBox.warning(self, "Invalid pattern",
                                    f"'{d['label']}' needs a group around the name, e.g. (?P<name>.+?)")
                return
        super().accept()

    def result_settings(self, base: dict) -> dict:
        s = dict(base)
        s["rules"] = [r for r in self._all_rules() if r["pattern"]]  # Drop empty rows.
        s["detectors"] = [d for d in self._all_detectors() if d["id"] != "custom" or d["regex"]]
        s["strip_group_tag"] = self.strip_chk.isChecked()
        s["merge_similar_folders"] = self.merge_chk.isChecked()
        s["default_enabled"] = self.default_chk.isChecked()
        s["default_folder"] = self.default_edit.text().strip() or "Unsorted"
        exts = []
        for e in re.split(r"[,\s;]+", self.ext_edit.text()):
            e = e.strip().lower()
            if e:
                exts.append(e if e.startswith(".") else "." + e)  # "mkv" -> ".mkv"
        s["extensions"] = exts or [".mkv"]
        s["on_conflict"] = self.conflict_combo.currentData()
        s["movie_by_duration"] = self.movie_chk.isChecked()
        s["movie_minutes"] = self.movie_min.value()
        s["movie_folder"] = self.movie_edit.text().strip() or "Movies"
        return s


# =============================================================== the tab

class VideoSorterTab(ToolTab):
    title = "Video Sorter"
    key = "video_sorter"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = load_settings()
        self.history = HistoryStore(SETTINGS_KEY, legacy=_first_existing(*LEGACY_HISTORY))
        self.scanned_root: Path | None = None
        self.existing_folders: list[str] = []
        self.output_existed = True
        lay = self.body_layout

        # --- source and output folders
        self.source = FolderPicker("Source:", self.settings.get("recent_folders", []),
                                   "Folder to organize (or drop a folder here)", "Choose folder to organize")
        self.source.chosen.connect(lambda _: self.scan())
        lay.addWidget(self.source)

        out = QHBoxLayout()
        lbl = QLabel("Output:")
        lbl.setMinimumWidth(70)
        self.output_edit = QLineEdit(self.settings.get("output_folder", ""))
        self.output_edit.setPlaceholderText("Same as the source folder")
        self.output_edit.setClearButtonEnabled(True)
        self.output_edit.setToolTip("Subfolders are created here. Leave empty to organize in place.")
        self.output_edit.editingFinished.connect(self._output_changed)
        self.output_edit.textChanged.connect(lambda t: self._output_changed() if not t else None)
        b_out = QPushButton("Browse…")
        b_out.clicked.connect(self.browse_output)
        b_scan = QPushButton("Scan")
        b_scan.setToolTip("Preview what would happen. Nothing is moved yet. (F5)")
        b_scan.clicked.connect(self.scan)
        self.recursive_chk = QCheckBox("Include subfolders")
        self.recursive_chk.setToolTip(
            "Also sort video files inside subfolders.\n"
            "Files already in the folder they'd go to are left out, and so are\n"
            "\"Old versions\" folders and the output folder.")
        self.recursive_chk.setChecked(self.settings.get("recursive", False))
        self.recursive_chk.toggled.connect(self._recursive_changed)
        out.addWidget(lbl)
        out.addWidget(self.output_edit, 1)
        out.addWidget(b_out)
        out.addWidget(self.recursive_chk)
        out.addWidget(b_scan)
        lay.addLayout(out)

        # --- search
        srow = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search file, destination, reason or status (try \"No match\" or \"Movies\")")
        self.search.setClearButtonEnabled(True)
        self.count_label = QLabel("")
        srow.addWidget(self.search, 1)
        srow.addWidget(self.count_label)
        lay.addLayout(srow)

        # --- preview table (model -> sort/filter proxy -> view)
        self.model = PlanModel()
        self.model.settings = self.settings
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)  # Search every column.
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.search.textChanged.connect(lambda _: self.update_counts())

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(C_FILE, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(C_INC, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(C_FILE, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_DEST, QHeaderView.ResizeMode.Interactive)
        hh.resizeSection(C_DEST, 260)
        hh.setSectionResizeMode(C_REASON, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(C_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(C_FROM, QHeaderView.ResizeMode.Interactive)
        hh.resizeSection(C_FROM, 180)
        # Only useful when subfolders are scanned.
        self.table.setColumnHidden(C_FROM, not self.settings.get("recursive", False))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_menu)
        self.table.doubleClicked.connect(self._double_clicked)
        self.model.dataChanged.connect(lambda *_: self.update_counts())
        lay.addWidget(self.table, 1)

        # --- bottom buttons
        bottom = QHBoxLayout()
        b_all = QPushButton("Select all")
        b_all.clicked.connect(lambda: self.set_include_visible(True))
        b_none = QPushButton("Select none")
        b_none.clicked.connect(lambda: self.set_include_visible(False))
        b_rules = QPushButton("Rules & settings…")
        b_rules.clicked.connect(self.edit_rules)
        b_hist = QPushButton("History…")
        b_hist.clicked.connect(lambda: HistoryDialog(self).exec())
        self.b_undo = QPushButton("Undo last")
        self.b_undo.setToolTip("Put back the files from the last Organize (Ctrl+Z)")
        self.b_undo.clicked.connect(self.undo_last)
        self.b_go = QPushButton("Organize")
        self.b_go.setDefault(True)
        self.b_go.clicked.connect(self.organize)
        # Opens Episode Check on the output folder: the natural next step after sorting.
        self.b_check = QPushButton("Check episodes…")
        self.b_check.setToolTip("Open the output folder in the Episode Check tab")
        self.b_check.clicked.connect(
            lambda: self.output_root() and self.open_in_tab.emit("episode_check", str(self.output_root())))
        for b in (b_all, b_none, b_rules, b_hist, self.b_check):
            bottom.addWidget(b)
        bottom.addStretch()
        bottom.addWidget(self.b_undo)
        bottom.addWidget(self.b_go)
        lay.addLayout(bottom)

        # Shortcuts only work while this tab is the one showing.
        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        QShortcut(QKeySequence("F5"), self, activated=self.scan, context=ctx)
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.undo_last, context=ctx)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.search.setFocus, context=ctx)

        self.update_counts()
        self.on_idle()

    # ---------------------------------------------------------- helpers
    def output_root(self) -> Path | None:
        """The folder where subfolders get created: the chosen output, or the source."""
        text = self.output_edit.text().strip().strip('"')
        return Path(text) if text else self.scanned_root

    def _output_changed(self):
        new = self.output_edit.text().strip().strip('"')
        if new == self.settings.get("output_folder", ""):
            return  # editingFinished also fires on focus loss; ignore if nothing changed.
        self.settings["output_folder"] = new
        self.save_settings()
        self.model.output_root = self.output_root()
        # Merging similar names depends on which folders exist in the output.
        if self.scanned_root and not self.busy:
            self.scan()

    def _recursive_changed(self, on: bool):
        self.settings["recursive"] = on
        self.save_settings()
        self.table.setColumnHidden(C_FROM, not on)
        if self.scanned_root and not self.busy:
            self.scan()  # The list of files changes, so preview it again.

    def save_settings(self):
        try:
            save_tool_settings(SETTINGS_KEY, self.settings)
        except OSError as e:
            self.write_log(f"Could not save settings: {e}")

    def on_idle(self):
        self.b_undo.setEnabled(bool(self.history.batches) and not self.busy)
        # Only inside Media Toolbox (the button needs the other tab) and after a scan.
        self.b_check.setVisible(self.linked)
        self.b_check.setEnabled(self.scanned_root is not None and not self.busy)
        self.update_counts()

    def selected_rows(self) -> list[int]:
        # Selection is in proxy (sorted/filtered) rows; map back to model rows.
        rows = {self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()}
        return sorted(rows)

    def update_counts(self):
        items = self.model.items
        will = sum(1 for i in items if i.include and i.destination and i.status == "Pending")
        unmatched = sum(1 for i in items if i.reason == "No match")
        moved = sum(1 for i in items if i.status.startswith("Moved"))
        shown = self.proxy.rowCount()
        parts = [f"{len(items)} files", f"{will} to move", f"{unmatched} unmatched"]
        if moved:
            parts.append(f"{moved} moved")
        if shown != len(items):
            parts.append(f"{shown} shown")
        self.count_label.setText(",  ".join(parts))
        self.b_go.setText(f"Organize {will} file{'s' * (will != 1)}" if will else "Organize")
        self.b_go.setEnabled(will > 0 and not self.busy)

    def folder_dropped(self, path: Path):
        self.source.set_text(str(path))
        self.scan()

    # ---------------------------------------------------------- actions
    def browse_output(self):
        from PySide6.QtWidgets import QFileDialog
        start = self.output_edit.text() or self.source.text() or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Choose output folder", start)
        if d:
            self.output_edit.setText(d)
            self._output_changed()

    def scan(self):
        if self.busy:
            return
        root = self.source.path()
        if not root:
            QMessageBox.warning(self, "No folder", "Choose a folder to organize first.")
            return

        # Keep destinations edited by hand when rescanning the same folder.
        manual = {}
        if self.scanned_root == root:
            manual = {i.rel: i for i in self.model.items if i.manual and i.status == "Pending"}
        out_text = self.output_edit.text().strip().strip('"')
        output_root = Path(out_text) if out_text else root

        def finished(result):
            items = result["items"]
            self.scanned_root = root
            self.existing_folders = result["existing"]  # Reused by "Change destination".
            self.output_existed = result["output_exists"]
            self.model.output_root = self.output_root()
            self.model.set_items(items)
            self.settings["recent_folders"] = remember_recent(self.settings.get("recent_folders", []), root)
            self.save_settings()
            self.source.set_recent(self.settings["recent_folders"])
            self.source.set_text(str(root))
            self.update_counts()
            self.write_log(f"Scanned {root}: {len(items)} file(s)"
                           + (" including subfolders." if self.settings.get("recursive") else "."))
            if result["already_sorted"]:
                self.write_log(f"  {result['already_sorted']} file(s) already in their folder were left out.")
            self.find_movies_by_length(items)  # Starts the next background step.

        self.run_task(scan_folder, (root, output_root, dict(self.settings), manual), finished,
                      "Reading folder…")

    def find_movies_by_length(self, items: list[PlanItem]):
        """Check the running time of files with no episode number, in the background.
        Long ones become movies; the table updates when the check finishes."""
        if MediaInfo is None or not self.settings.get("movie_by_duration", True):
            return
        candidates = [i for i in items if not i.manual and i.status == "Pending"
                      and i.reason in ("No match", "Default folder")]
        if not candidates:
            return
        limit = self.settings.get("movie_minutes", 60) * 60
        template = self.settings.get("movie_folder", "Movies")

        def finished(durations):
            found = 0
            for i in candidates:
                seconds = durations.get(str(i.current_path))
                # Skip rows the user edited while the check was running.
                if seconds and seconds >= limit and not i.manual and i.status == "Pending":
                    dest = render_destination(template, i.filename, self.settings)
                    if dest:
                        i.destination, i.include = dest, True
                        i.reason = f"Movie by length ({human_duration(seconds)})"
                        found += 1
            self.model.refresh_all()
            self.update_counts()
            if found:
                self.write_log(f"{found} file(s) with no episode number are {limit // 60}+ minutes: treated as movies.")

        self.run_task(probe_durations, ([i.current_path for i in candidates],), finished,
                      f"Checking the length of {len(candidates)} file(s)…")

    def organize(self):
        if self.busy or not self.scanned_root:
            return
        todo = [i for i in self.model.items if i.include and i.destination and i.status == "Pending"]
        if not todo:
            return

        out = self.output_root()
        if not out.is_absolute():
            QMessageBox.warning(self, "Output folder", "Use a full path for the output folder.")
            return

        # Confirmation shows which folders will receive files.
        dests: dict[str, int] = {}
        for i in todo:
            dests[i.destination] = dests.get(i.destination, 0) + 1
        summary = "\n".join(f"  {d}  ({n})" for d, n in sorted(dests.items())[:15])
        if len(dests) > 15:
            summary += f"\n  … and {len(dests) - 15} more folders"
        note = "" if self.output_existed else "\n(The output folder will be created.)"
        if QMessageBox.question(self, "Organize files",
                                f"Move {len(todo)} file(s) into {len(dests)} folder(s) in:\n{out}{note}\n\n{summary}") \
                != QMessageBox.StandardButton.Yes:
            return

        jobs = [(i.rel, i.destination) for i in todo]
        on_conflict = self.settings.get("on_conflict", "skip")

        def finished(result):
            batch, results = result
            for i in todo:
                status, new_path = results.get(i.rel, ("Error", None))
                i.status = status
                if new_path:
                    i.current_path = Path(new_path)
                    i.include = False
                self.write_log(f"{status}: {i.filename}" + (f"  ->  {new_path}" if new_path else ""))
            self.model.refresh_all()
            if batch:
                self.history.add(batch)
                self.write_log(f"Moved {len(batch['moves'])} file(s). Use Undo last to put them back.")
                # The folders exist now: keep the cached list current without re-reading the disk.
                new = {i.destination.split("/")[0] for i in todo if i.status.startswith("Moved")}
                self.existing_folders = sorted(set(self.existing_folders) | new, key=str.lower)
                self.output_existed = True

        self.run_task(execute_moves, (self.scanned_root, out, jobs, on_conflict), finished,
                      f"Moving {len(jobs)} file(s)…")

    def undo_last(self):
        if self.history.batches and not self.busy:
            self.revert(self.history.last())

    def revert(self, batch: dict, after=None):
        if self.busy:
            return
        n = len(batch["moves"])
        if QMessageBox.question(self, "Revert",
                                f"Put back {n} file(s) moved on {batch['time']}?\n{batch.get('root', '')}") \
                != QMessageBox.StandardButton.Yes:
            return

        def finished(result):
            ok, errors, remaining = result
            if remaining:
                batch["moves"] = remaining   # Keep what couldn't be reverted for a later try.
                self.history.save()
            else:
                self.history.remove(batch["id"])
            self.write_log(f"Reverted {ok} file(s).")
            for e in errors:
                self.write_log(f"  {e}")
            if errors:
                QMessageBox.warning(self, "Revert finished with problems",
                                    f"Reverted {ok} of {n}.\n\n" + "\n".join(errors[:15]))
            if self.scanned_root and Path(batch.get("root", "")) == self.scanned_root:
                self.scan()  # The files are back, so show them again.
            if after:
                after()

        self.run_task(revert_batch, (batch,), finished, f"Reverting {n} file(s)…")

    def edit_rules(self):
        dlg = RulesDialog(self.settings, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.settings = dlg.result_settings(self.settings)
            self.model.settings = self.settings
            self.save_settings()
            self.write_log("Rules saved.")
            if self.scanned_root:
                self.scan()  # Re-run the rules on the current folder.

    def set_include_visible(self, value: bool):
        """Select all / none, only for rows visible under the current search."""
        for r in range(self.proxy.rowCount()):
            it = self.model.items[self.proxy.mapToSource(self.proxy.index(r, 0)).row()]
            if it.status == "Pending":
                it.include = value and bool(it.destination)
        self.model.refresh_all()

    def _double_clicked(self, proxy_index):
        it = self.model.items[self.proxy.mapToSource(proxy_index).row()]
        if proxy_index.column() == C_DEST and it.status == "Pending":
            return  # Double-click on Destination edits it instead.
        self.safe_open(it.current_path)

    # ---------------------------------------------------------- context menu
    def show_menu(self, pos):
        # Right-clicking an unselected row selects it first, like a file manager.
        idx = self.table.indexAt(pos)
        if idx.isValid() and not self.table.selectionModel().isRowSelected(idx.row(), QModelIndex()):
            self.table.selectRow(idx.row())
        rows = self.selected_rows()
        if not rows:
            return
        items = [self.model.items[r] for r in rows]
        first = items[0]
        pending = [i for i in items if i.status == "Pending"]
        out = self.output_root()

        m = QMenu(self)
        m.addAction("Open file", lambda: self.safe_open(first.current_path))
        m.addAction("Show in folder", lambda: self.safe_open(first.current_path, reveal=True))
        a = m.addAction("Open destination folder", lambda: self.safe_open(out / first.destination))
        # Uses the folder list from the last scan instead of checking the disk from the menu.
        a.setEnabled(bool(first.destination) and out is not None
                     and first.destination.split("/")[0] in self.existing_folders)
        m.addSeparator()

        a = m.addAction(f"Change destination… ({len(pending)})", lambda: self.change_destination(pending))
        a.setEnabled(bool(pending))
        a = m.addAction("Reset to automatic", lambda: self.reset_auto(pending))
        a.setEnabled(any(i.manual for i in pending))
        a = m.addAction("Include", lambda: self._set_include(pending, True))
        a.setEnabled(bool(pending))
        a = m.addAction("Exclude", lambda: self._set_include(pending, False))
        a.setEnabled(bool(pending))
        m.addSeparator()
        m.addAction("Make a rule from this file…", lambda: self.rule_from_item(first))
        m.addAction("Copy file name" + ("s" if len(items) > 1 else ""),
                    lambda: QApplication.clipboard().setText("\n".join(i.filename for i in items)))
        # Links go to the file's destination folder once it exists, else to where it is now.
        known = bool(first.destination) and first.destination.split("/")[0] in self.existing_folders
        self.add_link_actions(m, out / first.destination if known and out else first.current_path.parent)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def change_destination(self, items: list[PlanItem]):
        # Suggest folders that already exist in the output, rule targets, and current destinations.
        # Folder names come from the last scan, so the menu doesn't touch the disk.
        choices = set(self.existing_folders)
        choices |= {r["destination"] for r in self.settings.get("rules", []) if r.get("destination")}
        choices |= {i.destination for i in self.model.items if i.destination}
        ordered = sorted(choices, key=str.lower)
        current = items[0].destination
        if current in ordered:
            ordered.remove(current)
        ordered.insert(0, current)
        text, ok = QInputDialog.getItem(self, "Change destination",
                                        f"Folder for {len(items)} file(s)  ({{name}} = title):",
                                        ordered, 0, True)
        if not ok:
            return
        for i in items:
            i.destination = render_destination(text, i.filename, self.settings)
            i.reason, i.manual = "Manual", True
            i.include = bool(i.destination)
        self.model.refresh_all()

    def reset_auto(self, items: list[PlanItem]):
        for i in items:
            i.destination, i.reason = decide(i.filename, self.settings)
            i.manual = False
            i.include = bool(i.destination)
        self.model.refresh_all()

    def _set_include(self, items: list[PlanItem], value: bool):
        for i in items:
            i.include = value and bool(i.destination)
        self.model.refresh_all()

    def rule_from_item(self, item: PlanItem):
        """Two quick prompts that add a 'contains' rule at the top of the list."""
        suggestion = clean_title(item.filename, self.settings)
        pattern, ok = QInputDialog.getText(self, "New rule", "Files whose name contains:", text=suggestion)
        if not ok or not pattern.strip():
            return
        dest, ok = QInputDialog.getText(self, "New rule", "Move them to folder:",
                                        text=item.destination or pattern.strip())
        if not ok or not dest.strip():
            return
        rule = {"enabled": True, "pattern": pattern.strip(), "mode": "contains", "destination": dest.strip()}
        self.settings.setdefault("rules", []).insert(0, rule)  # Top = highest priority.
        self.save_settings()
        self.write_log(f"Rule added: '{rule['pattern']}' -> {rule['destination']}")
        self.scan()


if __name__ == "__main__":
    run_standalone(VideoSorterTab)
