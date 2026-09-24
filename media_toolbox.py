#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Media Toolbox - all the tools in one window, one tab each:

    Folder Sorter   sort episodes and movies into folders
    Video Check     frame rate problems, via MediaInfo
    Episode Check   missing and duplicate episodes per series folder
    File List       list a folder tree and export it as TXT / CSV
    Music Sorter    move music into Artist/Album folders by tags

Each tab keeps its own settings and can run a task while you use the others.
Every tool file can also be run on its own (e.g. python video_check.py).

Requires:  pip install PySide6
Optional:  pip install pymediainfo mutagen   (for Video Check and Music Sorter)
"""
from __future__ import annotations

import sys
from pathlib import Path

# Let "python media_toolbox.py" work from any folder: the tool modules live next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QByteArray  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QTabWidget  # noqa: E402

from common import APP_NAME, load_tool_settings, save_tool_settings  # noqa: E402
from episode_check import EpisodeCheckTab  # noqa: E402
from file_list import FileListTab  # noqa: E402
from folder_sorter import FolderSorterTab  # noqa: E402
from music_sorter import MusicSorterTab  # noqa: E402
from video_check import VideoCheckTab  # noqa: E402

SETTINGS_KEY = "toolbox"

# Tab order. Add a new tool by writing a ToolTab subclass and listing it here.
TABS = [FolderSorterTab, VideoCheckTab, EpisodeCheckTab, FileListTab, MusicSorterTab]


class ToolboxWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_tool_settings(SETTINGS_KEY, {"last_tab": 0, "geometry": ""})
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 760)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.pages = []
        for cls in TABS:
            page = cls()
            self.pages.append(page)
            self.tabs.addTab(page, page.title)
        self.setCentralWidget(self.tabs)

        # Reopen on the tab and at the size/position used last time.
        self.tabs.setCurrentIndex(min(self.settings.get("last_tab", 0), len(self.pages) - 1))
        if self.settings.get("geometry"):
            self.restoreGeometry(QByteArray.fromBase64(self.settings["geometry"].encode()))

        # A tab that's working shows "…" in its title, so you can see it from other tabs.
        for i, page in enumerate(self.pages):
            page.busy_changed.connect(
                lambda busy, i=i, page=page: self.tabs.setTabText(i, f"{page.title} …" if busy else page.title))

    def closeEvent(self, e):
        busy = [p.title for p in self.pages if p.busy]
        if busy:
            # Closing mid-task would kill a thread halfway through a copy.
            QMessageBox.information(self, "Still working",
                                    "Wait for these to finish, or cancel them:\n  " + "\n  ".join(busy))
            e.ignore()
            return
        self.settings["last_tab"] = self.tabs.currentIndex()
        self.settings["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        try:
            save_tool_settings(SETTINGS_KEY, self.settings)
        except OSError:
            pass  # Losing the window position is not worth an error on exit.
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    w = ToolboxWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
