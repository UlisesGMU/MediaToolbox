#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Media Toolbox - all the tools in one window, one tab each:

    Video Sorter    sort episodes and movies into folders
    Video Check     frame rate problems, via MediaInfo
    Episode Check   missing and duplicate episodes per series folder
    File List       list a folder tree and export it as TXT / CSV
    Music Sorter    move music into Artist/Album folders by tags

Each tab keeps its own settings and can run a task while you use the others.
Tabs can send a folder to each other ("Check episodes in this folder"), and
File > Export/Import settings moves every tab's settings to another PC.
Every tool file can also be run on its own (e.g. python video_check.py).

Requires:  pip install PySide6
Optional:  pip install pymediainfo mutagen   (pymediainfo: Video Check, and movie
           detection by length in Video Sorter; mutagen: Music Sorter)
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import platform
import sys
from pathlib import Path

# Let "python media_toolbox.py" work from any folder: the tool modules live next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QByteArray, QProcess, Qt, qVersion  # noqa: E402
from PySide6.QtGui import QColor, QKeySequence  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from common import (  # noqa: E402
    APP_NAME, APP_TITLE, APP_VERSION, DIM_COLOR, ERROR_COLOR, OK_COLOR, config_dir, export_all_settings, load_json, hash_cache, import_all_settings, load_tool_settings,
    open_path, save_tool_settings, stamped_name,
)
from episode_check import EpisodeCheckTab  # noqa: E402
from file_list import FileListTab  # noqa: E402
from music_check import MusicCheckTab  # noqa: E402
from music_sorter import MusicSorterTab  # noqa: E402
from video_check import VideoCheckTab  # noqa: E402
from video_sorter import VideoSorterTab  # noqa: E402

SETTINGS_KEY = "toolbox"

# Tab order. Add a new tool by writing a ToolTab subclass and listing it here.
TABS = [VideoSorterTab, VideoCheckTab, EpisodeCheckTab, FileListTab, MusicSorterTab, MusicCheckTab]


# Optional packages: (pip name, import name, what it enables).
OPTIONAL_PACKAGES = [
    ("pymediainfo", "pymediainfo", "Video Check, and movies by length in Video Sorter"),
    ("mutagen", "mutagen", "Music Sorter and Music Check"),
    ("xxhash", "xxhash", "the xxHash checksum"),
    ("send2trash", "send2trash", "deleting to the Recycle Bin instead of permanently"),
]

FROZEN = getattr(sys, "frozen", False)  # True inside the .exe built with build_exe.py


def installed(import_name: str) -> bool:
    importlib.invalidate_caches()  # Notice packages installed since the app started.
    return importlib.util.find_spec(import_name) is not None


def console_python() -> str:
    """The python that runs pip. On Windows, pythonw.exe (no console) can't show
    pip's output well, so its python.exe sibling is used when there is one."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return sys.executable


def restart_command() -> tuple[str, list[str]]:
    """How to start this same app again, with the same arguments."""
    if FROZEN:
        return sys.executable, sys.argv[1:]
    exe = Path(sys.executable)
    # On Windows, restart without a console window when pythonw.exe is available.
    if sys.platform.startswith("win") and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        exe = exe.parent / "pythonw.exe"
    return str(exe), [str(Path(sys.argv[0]).resolve())] + sys.argv[1:]


class AboutDialog(QDialog):
    """Version, what's installed, and the two fixes for "a tab doesn't work":
    install the missing packages, then restart so they're loaded."""

    def __init__(self, window: "ToolboxWindow"):
        super().__init__(window)
        self.main = window  # Not "self.window": that would hide QWidget.window().
        self.proc: QProcess | None = None
        self.setWindowTitle(f"About {APP_NAME}")
        self.resize(620, 460)
        lay = QVBoxLayout(self)

        try:
            import PySide6
            pyside = PySide6.__version__
        except (ImportError, AttributeError):
            pyside = "?"
        head = QLabel(
            f"<h3>{APP_TITLE}</h3>"
            f"<p>Python {platform.python_version()} · PySide6 {pyside} · Qt {qVersion()} · "
            f"{platform.system()} {platform.release()}<br>"
            f"Settings folder: {config_dir()}</p>"
            f"<p>Built with the assistance of Claude (Anthropic). GPL-3.0.</p>")
        head.setWordWrap(True)
        head.setTextInteractionFlags(head.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(head)

        lay.addWidget(QLabel("<b>Optional packages</b>"))
        self.table = QTableWidget(len(OPTIONAL_PACKAGES), 3)
        self.table.setHorizontalHeaderLabels(["Package", "Status", "Needed for"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setMaximumHeight(150)
        lay.addWidget(self.table)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

        # pip's output, shown while installing.
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.hide()
        lay.addWidget(self.output, 1)

        row = QHBoxLayout()
        self.b_install = QPushButton("Install missing packages")
        self.b_install.clicked.connect(self.install)
        self.b_restart = QPushButton(f"Restart {APP_NAME}")
        self.b_restart.setToolTip("Close and start again, e.g. so newly installed packages are loaded.")
        self.b_restart.clicked.connect(self.restart)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.close)
        row.addWidget(self.b_install)
        row.addWidget(self.b_restart)
        row.addStretch()
        row.addWidget(b_close)
        lay.addLayout(row)
        self.refresh()

    def missing(self) -> list[str]:
        return [pip for pip, imp, _ in OPTIONAL_PACKAGES if not installed(imp)]

    def refresh(self, just_installed: bool = False):
        for r, (pip, imp, what) in enumerate(OPTIONAL_PACKAGES):
            ok = installed(imp)
            # A package installed in this session is on disk but not loaded until a restart.
            loaded = imp in sys.modules or not ok
            status = QTableWidgetItem("Installed" if ok and loaded else
                                      "Installed, restart to use" if ok else "Not installed")
            status.setForeground(QColor(OK_COLOR if ok and loaded else DIM_COLOR if ok else ERROR_COLOR))
            for c, item in enumerate((QTableWidgetItem(pip), status, QTableWidgetItem(what))):
                self.table.setItem(r, c, item)
        missing = self.missing()
        busy = self.proc is not None
        self.b_install.setEnabled(bool(missing) and not FROZEN and not busy)
        self.b_install.setText(f"Install missing packages ({len(missing)})" if missing else "Everything is installed")
        self.b_restart.setEnabled(not busy)

        notes = []
        if FROZEN:
            notes.append("This is the built program, which can't install packages. To add one, install it "
                         "with pip on the PC that builds it, then run build_exe.py again.")
        elif missing:
            notes.append(f"Install runs: {Path(console_python()).name} -m pip install {' '.join(missing)}")
        if sys.platform.startswith("linux") and installed("pymediainfo"):
            notes.append("On Linux, pymediainfo also needs the MediaInfo library from your package "
                         "manager (e.g. libmediainfo); pip can't install that part.")
        if just_installed:
            notes.insert(0, "<b>Installed. Restart to start using the new packages.</b>")
            self.b_restart.setDefault(True)
        self.note.setText("<br>".join(notes))

    # ---------------------------------------------------------- install
    def install(self):
        missing = self.missing()
        if not missing or self.proc is not None:
            return
        if QMessageBox.question(self, "Install packages",
                                f"Install {', '.join(missing)} with pip into this Python?\n{console_python()}") \
                != QMessageBox.StandardButton.Yes:
            return
        self.output.clear()
        self.output.show()
        # QProcess runs pip without freezing the window and streams its output here.
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read_output)
        self.proc.finished.connect(self._install_finished)
        self.proc.errorOccurred.connect(self._install_error)
        self.proc.start(console_python(), ["-m", "pip", "install", *missing])
        self.refresh()
        self.note.setText("Installing… this can take a minute.")

    def _read_output(self):
        if self.proc:
            text = bytes(self.proc.readAllStandardOutput()).decode(errors="replace")
            self.output.appendPlainText(text.rstrip())

    def _install_finished(self, code: int, _status=None):
        self._read_output()
        log = self.output.toPlainText()
        self.proc = None
        self.refresh(just_installed=(code == 0))
        if code != 0:
            hint = "pip didn't finish; the messages above say why."
            if "externally-managed-environment" in log:
                # Common on Linux: the system Python refuses pip installs.
                hint = ("This Python is managed by your system and refuses pip installs. Install the "
                        "packages with your package manager (e.g. python-mutagen, python-pymediainfo), "
                        "or run Media Toolbox from a virtual environment.")
            self.note.setText(f"<span style='color:{ERROR_COLOR}'>{hint}</span>")

    def _install_error(self, _error):
        if self.proc and self.proc.state() == QProcess.ProcessState.NotRunning:
            self.output.appendPlainText(f"Couldn't start pip: {self.proc.errorString()}")
            self.proc = None
            self.refresh()

    # ---------------------------------------------------------- restart / close
    def restart(self):
        if self.proc is not None:
            return
        self.accept()
        self.main.restart()

    def closeEvent(self, e):
        if self.proc is not None:
            QMessageBox.information(self, "Installing", "Wait for the installation to finish.")
            e.ignore()
        else:
            e.accept()

    def reject(self):  # Esc key: same rule as closing.
        if self.proc is None:
            super().reject()


class ToolboxWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_tool_settings(SETTINGS_KEY, {"last_tab": 0, "geometry": ""})
        self.setWindowTitle(APP_TITLE)
        self.resize(1180, 760)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.setCentralWidget(self.tabs)
        self.pages = []
        self.build_tabs()
        self.build_menu()

        # Reopen on the tab and at the size/position used last time.
        self.tabs.setCurrentIndex(min(self.settings.get("last_tab", 0), len(self.pages) - 1))
        if self.settings.get("geometry"):
            self.restoreGeometry(QByteArray.fromBase64(self.settings["geometry"].encode()))

    # ---------------------------------------------------------- tabs
    def build_tabs(self):
        """Create every tab. Also used after importing settings, so each tab
        starts again from the restored settings files."""
        current = self.tabs.currentIndex()
        for page in self.pages:
            page.wait_for_threads()  # Their threads must be fully stopped before deleting.
            page.deleteLater()
        self.tabs.clear()
        self.pages = []
        for i, cls in enumerate(TABS):
            page = cls()
            page.linked = True  # Show "Check episodes in this folder" and similar links.
            page.on_idle()      # Refresh buttons that depend on being linked.
            # A tab that's working shows "…" in its title, so you can see it from other tabs.
            page.busy_changed.connect(
                lambda busy, i=i, page=page: self.tabs.setTabText(i, f"{page.title} …" if busy else page.title))
            page.open_in_tab.connect(self.open_in_tab)
            self.pages.append(page)
            self.tabs.addTab(page, page.title)
        if current >= 0:
            self.tabs.setCurrentIndex(current)

    def open_in_tab(self, key: str, folder: str):
        """A tab asked to open a folder in another tab (e.g. "Check episodes in this folder")."""
        page = next((p for p in self.pages if p.key == key), None)
        if page is None:
            return
        self.tabs.setCurrentWidget(page)
        if page.busy:
            QMessageBox.information(self, page.title, f"{page.title} is still working. Try again when it finishes.")
            return
        page.open_folder(Path(folder))

    # ---------------------------------------------------------- menu
    def build_menu(self):
        m = self.menuBar().addMenu("&File")
        m.addAction("Export settings…", self.export_settings)
        m.addAction("Import settings…", self.import_settings)
        m.addAction("Open settings folder", lambda: open_path(config_dir()))
        m.addSeparator()
        m.addAction("Clear checksum cache", self.clear_cache)
        m.addSeparator()
        m.addAction("Quit", self.close, QKeySequence.StandardKey.Quit)
        h = self.menuBar().addMenu("&Help")
        h.addAction(f"About {APP_NAME}", self.about)
        h.addAction(f"Restart {APP_NAME}", self.restart)

    def about(self):
        AboutDialog(self).exec()

    def restart(self):
        """Close normally (saving settings; refused while a tab is working),
        then start the same app again."""
        program, args = restart_command()
        if not self.close():
            return  # A tab is still working: closeEvent said why.
        # Started after the settings were saved, so the new instance reads them.
        QProcess.startDetached(program, args, os.getcwd())
        QApplication.quit()

    def export_settings(self):
        """Every tab's settings (rules, patterns, options, recent folders) in one file,
        e.g. to copy them to another PC. Histories and the checksum cache stay here."""
        path, _ = QFileDialog.getSaveFileName(self, "Export settings",
                                              str(Path.home() / stamped_name("media_toolbox_settings.json")),
                                              "Settings backup (*.json)")
        if not path:
            return
        try:
            keys = export_all_settings(path)
        except OSError as e:
            QMessageBox.warning(self, "Export settings", f"Couldn't save the file:\n{e}")
            return
        QMessageBox.information(self, "Export settings", f"Saved the settings of {len(keys)} part(s) to:\n{path}")

    def import_settings(self):
        busy = [p.title for p in self.pages if p.busy]
        if busy:
            QMessageBox.information(self, "Import settings", "Wait for these to finish first:\n  " + "\n  ".join(busy))
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import settings", str(Path.home()), "Settings backup (*.json)")
        if not path:
            return
        # A backup made by a newer version may hold options this one doesn't know.
        made_with = (load_json(Path(path), {}) or {}).get("version", "")
        if made_with and _version_tuple(made_with) > _version_tuple(APP_VERSION):
            if QMessageBox.question(self, "Import settings",
                                    f"This backup was made with version {made_with}, newer than this one "
                                    f"({APP_VERSION}). Options this version doesn't know will be ignored.\n"
                                    "Import anyway?") != QMessageBox.StandardButton.Yes:
                return
        if QMessageBox.question(self, "Import settings",
                                "Replace the current settings of every tab with the ones in this file?\n"
                                "Tables are cleared; histories and undo are kept.") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            keys = import_all_settings(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Import settings", str(e))
            return
        # The window's own position is in the backup too; keep this PC's instead.
        save_tool_settings(SETTINGS_KEY, self.settings)
        self.build_tabs()  # Every tab reloads from the restored files.
        QMessageBox.information(self, "Import settings", f"Restored the settings of {len(keys)} part(s).")

    def clear_cache(self):
        n = hash_cache().clear()
        QMessageBox.information(self, "Checksum cache",
                                f"Forgot {n} remembered checksum(s). Files will be read again next time.")

    def closeEvent(self, e):
        busy = [p.title for p in self.pages if p.busy]
        if busy:
            # Closing mid-task would kill a thread halfway through a copy.
            QMessageBox.information(self, "Still working",
                                    "Wait for these to finish, or cancel them:\n  " + "\n  ".join(busy))
            e.ignore()
            return
        for p in self.pages:
            p.wait_for_threads()
        self.settings["last_tab"] = self.tabs.currentIndex()
        self.settings["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        try:
            save_tool_settings(SETTINGS_KEY, self.settings)
        except OSError:
            pass  # Losing the window position is not worth an error on exit.
        e.accept()


def _version_tuple(v: str) -> tuple[int, ...]:
    """'3.10.0' -> (3, 10, 0), so versions compare as numbers, not text."""
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def main():
    if "--version" in sys.argv:
        print(APP_TITLE)
        return
    app = QApplication(sys.argv)
    app.setApplicationVersion(APP_VERSION)
    app.setApplicationName(APP_NAME)
    w = ToolboxWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
