"""PyQt6 gallery that scans an image library and shows images sorted by
duplicate groups.

Usage: ``python main.py [folder]`` or pick a folder from the toolbar.
"""

from __future__ import annotations

import os
import sys
from io import BytesIO

from PIL import Image, ImageOps
from PyQt6.QtCore import (
    QAbstractListModel,
    QDir,
    QModelIndex,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QFileSystemModel, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSplitter,
    QToolBar,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .hashing import hamming
from .scanner import ImageRecord, ScanResult, scan_folder

_DEFAULT_GROUP_ROLE = int(Qt.ItemDataRole.UserRole)

_THUMB_PIXEL = 200
_THUMB_CACHE: dict[str, QPixmap] = {}
_THUMB_DISK_DIR: str | None = None


def _thumb_disk_dir() -> str:
    """Persistent thumbnails dir under the same root as the scan index."""
    global _THUMB_DISK_DIR
    if _THUMB_DISK_DIR is None:
        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
        _THUMB_DISK_DIR = os.path.join(base, "thumbs") if base else ""
    return _THUMB_DISK_DIR


def _thumb_disk_path(path: str) -> str:
    root = _thumb_disk_dir()
    if not root:
        return ""
    stat_key = ""
    try:
        st = os.stat(path)
        stat_key = f"-{st.st_size}-{st.st_mtime_ns}"
    except OSError:
        pass
    digest = hashlib.sha256(
        os.path.normcase(os.path.abspath(path)).encode("utf-8")
    ).hexdigest()[:24]
    return os.path.join(root, f"thumb-{digest}{stat_key}.png")


def _thumb_pixmap(path: str) -> QPixmap:
    pixmap = _THUMB_CACHE.get(path)
    if pixmap is not None:
        return pixmap

    disk_path = _thumb_disk_path(path)
    if disk_path and os.path.isfile(disk_path):
        candidate = QPixmap(disk_path)
        if not candidate.isNull():
            _THUMB_CACHE[path] = candidate
            return candidate

    pixmap = _build_pixmap(path, _THUMB_PIXEL)
    if disk_path and not pixmap.isNull():
        try:
            os.makedirs(os.path.dirname(disk_path), exist_ok=True)
            pixmap.save(disk_path, "PNG")
        except OSError:
            pass
    _THUMB_CACHE[path] = pixmap
    return pixmap


def _build_pixmap(path: str, max_size: int) -> QPixmap:
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode in ("P", "CMYK", "YCbCr", "I;16", "F"):
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            image.thumbnail((max_size, max_size))
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            pixmap = QPixmap()
            if not pixmap.loadFromData(buffer.getvalue()):
                return QPixmap()
            return pixmap
    except Exception:
        return QPixmap()


def _load_full_pixmap(path: str) -> QPixmap:
    return _build_pixmap(path, 1600)


def _hsize(value: int) -> str:
    units = ("B", "KB", "MB", "GB")
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} GB"


class ThumbnailModel(QAbstractListModel):
    """Model exposing lazy, cached thumbnails and captions."""

    def __init__(self, records: list[ImageRecord], parent: QWidget | None = None):
        super().__init__(parent)
        self.records = list(records)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.records)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.records)):
            return None
        record = self.records[index.row()]
        if role == Qt.ItemDataRole.DecorationRole:
            return _thumb_pixmap(record.path)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{record.path}\n{record.width} x {record.height}  {_hsize(record.size)}"
        if role == Qt.ItemDataRole.DisplayRole:
            return os.path.basename(record.path)
        return None


class GalleryView(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.model: ThumbnailModel | None = None

        self.title = QLabel("Open a folder to start.")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-weight: 700; padding: 4px;")

        self.list = QListView()
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setUniformItemSizes(True)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setGridSize(QSize(_THUMB_PIXEL + 12, _THUMB_PIXEL + 42))
        self.list.setIconSize(QSize(_THUMB_PIXEL, _THUMB_PIXEL))
        self.list.doubleClicked.connect(self._open_viewer)

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(4, 4, 4, 4)
        self.layout.addWidget(self.title)
        self.layout.addWidget(self.list, 1)

    def show_raw_folder(self):
        self.set_records([], "Open a folder, or choose one in the toolbar.")

    def set_records(self, records: list[ImageRecord], heading: str) -> None:
        self.model = ThumbnailModel(records, self)
        self.list.setModel(self.model)
        self.title.setText(heading)

    def _open_viewer(self, index: QModelIndex) -> None:
        if self.model is None or not index.isValid():
            return
        records = list(self.model.records)
        viewer = ImageViewer(records, index.row(), self)
        viewer.show()


class ImageViewer(QDialog):
    def __init__(
        self,
        records: list[ImageRecord],
        start: int,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Image viewer")
        self.resize(960, 720)
        self._records = records
        self._index = max(0, min(start, len(records) - 1))
        self._records_on_show = False

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(200, 200)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(self.image_label)

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setAlignment(Qt.AlignmentFlag.AlignCenter)

        prev_button = QPushButton("< Prev")
        next_button = QPushButton("Next >")
        prev_button.clicked.connect(self._previous)
        next_button.clicked.connect(self._next)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(prev_button)
        buttons.addWidget(next_button)
        buttons.addStretch(1)

        self.layout = QVBoxLayout(self)
        self.layout.addWidget(scroll, 1)
        self.layout.addWidget(self.info)
        self.layout.addLayout(buttons)

        QShortcut(QKeySequence("Left"), self, activated=self._previous)
        QShortcut(QKeySequence("Right"), self, activated=self._next)

        self._records_on_show = True
        self._show_current()

    def _show_current(self) -> None:
        self._records_on_show = False
        record = self._records[self._index]
        self.setWindowTitle(
            f"{os.path.basename(record.path)} ({self._index + 1}/{len(self._records)})"
        )
        self.image_label.setPixmap(_load_full_pixmap(record.path))
        anchor = self._records[0]
        similarity = hamming(anchor.phash, record.phash)
        self.info.setText(
            f"{self._index + 1} / {len(self._records)}\n"
            f"{record.path}\n"
            f"{record.width} x {record.height}   {_hsize(record.size)}   "
            f"hash diff to first: {similarity} bits"
        )
        self._records_on_show = True

    def _previous(self) -> None:
        self._index = (self._index - 1) % len(self._records)
        self._show_current()

    def _next(self) -> None:
        self._index = (self._index + 1) % len(self._records)
        self._show_current()


class FolderPickerDialog(QDialog):
    """Deterministic directory chooser: pick a folder in the tree, then press
    "Scan This Folder". The emitted path is exactly what the user sees."""

    folder_chosen = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Choose image library folder")
        self.resize(560, 640)
        self.setMinimumSize(360, 420)

        self.model = QFileSystemModel(self)
        self.model.setReadOnly(True)
        self.model.setFilter(
            QDir.Filter.Dirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.AllDirs
        )
        self.model.setRootPath("")

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Path:"))
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Type or paste a folder path and press Enter")
        self.path_edit.returnPressed.connect(self._jump_to_path)
        path_row.addWidget(self.path_edit, 1)
        browse_again_button = QPushButton("Go")
        browse_again_button.clicked.connect(self._jump_to_path)
        path_row.addWidget(browse_again_button)

        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setRootIndex(self.model.index(""))
        for column in range(1, self.model.columnCount()):
            self.tree.setColumnHidden(column, True)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tree.clicked.connect(lambda _index: self._update_label())
        self.tree.doubleClicked.connect(self._on_double_click)
        self.tree.selectionModel().currentChanged.connect(
            lambda _old, _new: self._update_label()
        )
        self.model.directoryLoaded.connect(lambda _path: self._reveal(0))
        self._pending_reveal: str | None = None
        self._queued = False

        self.path_label = QLabel("Select a folder in the tree, or type a path above.")
        self.path_label.setWordWrap(True)
        self.path_label.setStyleSheet("font-weight: 600; padding: 2px;")

        scan_button = QPushButton("Scan This Folder")
        scan_button.clicked.connect(self._scan_current)
        scan_button.setDefault(True)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(scan_button)
        buttons.addWidget(cancel_button)

        layout = QVBoxLayout(self)
        layout.addLayout(path_row)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.path_label)
        layout.addLayout(buttons)

    def show_at(self, directory: str) -> None:
        self._pending_reveal = os.path.normpath(os.path.abspath(directory))
        self._reveal(0)

    def _reveal(self, attempt: int) -> None:
        """Expand the tree down to the pending folder and select it once its
        ancestors have been fetched by the file-system model (which loads
        lazily/asynchronously)."""
        target = self._pending_reveal
        if not target or attempt > 60:
            self._pending_reveal = None
            return

        index = self.model.index(target)
        if index.isValid():
            chain = []
            node = index
            while node.isValid():
                chain.append(node)
                node = node.parent()
            for node in reversed(chain):
                if node != index:
                    self.tree.expand(node)
            self.tree.setCurrentIndex(index)
            self.tree.scrollTo(index)
            self._update_label()
            self._pending_reveal = None
            return

        # Not loaded yet - expand the deepest available ancestor to pull the
        # next level in, then retry shortly after (or on directoryLoaded).
        ancestor = target
        while True:
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                break
            ancestor = parent
            parent_index = self.model.index(ancestor)
            if parent_index.isValid():
                self.tree.expand(parent_index)
        QTimer.singleShot(60, lambda: self._reveal(attempt + 1))

    def _current_path(self) -> str:
        index = self.tree.currentIndex()
        if not index.isValid():
            return ""
        return self.model.filePath(index)

    def _jump_to_path(self) -> None:
        raw = self.path_edit.text().strip().strip('"')
        if not raw:
            return
        path = os.path.normpath(os.path.abspath(os.path.expanduser(os.path.expandvars(raw))))
        if os.path.isdir(path):
            self.path_label.setText(path)
            self.path_edit.setText(path)
            self.show_at(path)
        else:
            self.path_label.setText(f"Not an existing folder: {path}")

    def _update_label(self) -> None:
        path = self._current_path()
        if path and os.path.isdir(path):
            self.path_label.setText(path)
            self.path_edit.setText(path)
        else:
            self.path_label.setText("Select a folder in the tree, or type a path above.")

    def _scan_current(self) -> None:
        typed = self.path_edit.text().strip().strip('"')
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(typed)))
        if not path or not os.path.isdir(path):
            path = self._current_path()
        if not path or not os.path.isdir(path):
            self.path_label.setText("Select or type a valid folder first.")
            return
        self._emit_scan(os.path.normpath(path))

    def _on_double_click(self, index: QModelIndex) -> None:
        path = self.model.filePath(index)
        if path and os.path.isdir(path):
            self._emit_scan(os.path.normpath(path))

    def _emit_scan(self, path: str) -> None:
        self._queued = False
        self.folder_chosen.emit(path)
        if not self._queued:
            self.accept()

    def notify_queued(self, folder: str) -> None:
        self._queued = True
        self.path_label.setText(
            f"A scan is already running.\nYour folder is queued and will start "
            f"when it finishes:\n{folder}"
        )


class ScanThread(QThread):
    progress = pyqtSignal(int, int, str)
    scan_done = pyqtSignal(object)
    scan_failed = pyqtSignal(str)

    def __init__(self, folder: str, cache_dir: str | None = None):
        super().__init__()
        self.folder = folder
        self.cache_dir = cache_dir

    def run(self) -> None:
        try:
            result = scan_folder(
                self.folder,
                on_progress=self.progress.emit,
                cache_dir=self.cache_dir,
            )
            self.scan_done.emit(result)
        except Exception as exc:  # noqa: BLE001 - surfaced to the GUI
            self.scan_failed.emit(str(exc))


class MainWindow(QMainWindow):
    scan_queued = pyqtSignal(str)

    def __init__(self, initial_folder: str | None = None):
        super().__init__()
        self.result: ScanResult | None = None
        self.thread: ScanThread | None = None
        self.current_folder: str | None = None
        self.pending_folder: str | None = None
        self.settings = QSettings("ImageDuplicates", "ImageDuplicateGallery")
        self.last_folder_key = "last_folder"

        self.setWindowTitle("Image Duplicate Gallery")
        self.resize(1280, 820)

        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)
        open_action = toolbar.addAction("Open Folder...")
        open_action.setShortcut(QKeySequence("Ctrl+O"))
        open_action.triggered.connect(self.choose_folder)
        toolbar.addSeparator()
        self.threshold_label = QLabel(" Similarity threshold: ")
        toolbar.addWidget(self.threshold_label)
        toolbar.addAction("10 bits").setEnabled(False)
        self.progress_bar = _ToolbarProgress(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(self._on_tree_change)
        splitter.addWidget(self.tree)
        splitter.addWidget(GalleryView(self))
        splitter.setSizes([380, 900])
        self.setCentralWidget(splitter)

        self.gallery: GalleryView = splitter.widget(1)

        self.folder_picker = FolderPickerDialog(self)
        self.folder_picker.folder_chosen.connect(self.start_scan)
        self.scan_queued.connect(self.folder_picker.notify_queued)

        self.status = self.statusBar()
        self._add_status_widgets()

        self.status.showMessage("Open a folder to scan.")

        if initial_folder and os.path.isdir(initial_folder):
            self.start_scan(initial_folder)
            return

        saved_folder = self.settings.value(self.last_folder_key, "", str)
        if saved_folder and os.path.isdir(saved_folder):
            self.status.showMessage(f"Last folder: {saved_folder}")
            self.start_scan(saved_folder)
        else:
            self.status.showMessage("Open a folder to scan.")

    def _add_status_widgets(self):
        self.counts_label = QLabel("No images scanned")
        self.counts_label.setStyleSheet("padding-right: 12px;")
        self.status.addPermanentWidget(self.counts_label)

    def choose_folder(self) -> None:
        if self.current_folder:
            self.folder_picker.show_at(self.current_folder)
        self.folder_picker.show()
        self.folder_picker.raise_()
        self.folder_picker.activateWindow()

    def start_scan(self, folder: str) -> None:
        if self.thread is not None and self.thread.isRunning():
            if folder != self.current_folder:
                self.pending_folder = folder
                self.status.showMessage(
                    f"Scan still running - will scan {folder} when it finishes."
                )
            else:
                self.status.showMessage(f"Already scanning {self.current_folder}.")
            self.scan_queued.emit(folder)
            return
        self.pending_folder = None
        self.settings.setValue(self.last_folder_key, folder)
        self.current_folder = folder
        self.tree.clear()
        self.tree.setEnabled(False)
        self.gallery.show_raw_folder()
        self.counts_label.setText("Scanning...")
        self.status.showMessage(f"Scanning {folder}...")
        self.progress_bar.set_busy(folder)

        cache_dir: str | None = None
        cache_root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
        if cache_root:
            cache_dir = os.path.join(cache_root, "index")
        self.thread = ScanThread(folder, cache_dir)
        self.thread.progress.connect(self._on_progress)
        self.thread.scan_done.connect(self._on_scan_done)
        self.thread.scan_failed.connect(self._on_scan_failed)
        self.thread.start()

    def _on_progress(self, done: int, total: int, path: str) -> None:
        self.progress_bar.update(done, total, path)

    def _on_scan_failed(self, message: str) -> None:
        self.tree.setEnabled(True)
        self.status.showMessage("Scan failed.")
        QMessageBox.critical(self, "Scan failed", message)

    def _on_scan_done(self, result: ScanResult) -> None:
        self.result = result
        self.tree.setEnabled(True)
        self.progress_bar.finish()
        self._populate_tree(result)
        self.status.showMessage(self.current_folder or "")
        self._update_counts(result)
        next_folder, self.pending_folder = self.pending_folder, None
        if next_folder:
            self.start_scan(next_folder)

    def _update_counts(self, result: ScanResult) -> None:
        wasted = _hsize(result.wasted_bytes)
        self.counts_label.setText(
            f"{result.total} images  |  {len(result.groups)} duplicate groups "
            f"({result.duplicated_count} images)  |  {len(result.uniques)} unique  "
            f"|  {wasted} wasted"
        )

    def _populate_tree(self, result: ScanResult) -> None:
        self.tree.blockSignals(True)
        try:
            if result.groups:
                groups_root = QTreeWidgetItem(
                    self.tree, [f"Duplicate Groups ({len(result.groups)})"]
                )
                groups_root.setExpanded(True)
                for index, group in enumerate(result.groups, start=1):
                    first = group[0]
                    wasted = _hsize((len(group) - 1) * max(r.size for r in group))
                    item = QTreeWidgetItem(
                        groups_root,
                        [
                            f"Group {index}  ({len(group)} images, {wasted} wasted) "
                            f"- {os.path.basename(first.path)}"
                        ],
                    )
                    item.setData(0, _DEFAULT_GROUP_ROLE, list(group))
                    pixmap = _thumb_pixmap(first.path)
                    if not pixmap.isNull():
                        item.setData(0, Qt.ItemDataRole.DecorationRole, pixmap)
            else:
                QTreeWidgetItem(self.tree, ["No duplicate groups found"])

            uniques_root = QTreeWidgetItem(
                self.tree, [f"Unique Images ({len(result.uniques)})"]
            )
            self._insert_unique_summary(uniques_root, result.uniques)
        finally:
            self.tree.blockSignals(False)

        if result.groups:
            self.tree.setCurrentItem(self.tree.topLevelItem(0).child(0))
        elif result.uniques:
            summary = self.tree.topLevelItem(self.tree.topLevelItemCount() - 1)
            self.tree.setCurrentItem(summary.child(0))

    def _insert_unique_summary(
        self, root: QTreeWidgetItem, uniques: list[ImageRecord]
    ) -> None:
        summary = QTreeWidgetItem(root, ["Click to browse all unique images"])
        summary.setData(0, _DEFAULT_GROUP_ROLE, list(uniques))

    def _on_tree_change(self, current: QTreeWidgetItem | None, _previous) -> None:
        if current is None:
            return
        records = current.data(0, _DEFAULT_GROUP_ROLE)
        if not records:
            return
        heading = f"{current.text(0)}  ({len(records)} images)"
        self.gallery.set_records(records, heading)


class _ToolbarProgress:
    """A clearly visible progress bar in the toolbar while scanning."""

    def __init__(self, toolbar: QToolBar):
        self.bar = QProgressBar()
        self.bar.setFixedWidth(300)
        self.bar.setTextVisible(True)
        self.bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bar.setVisible(False)
        # Visibility of a widget added to a QToolBar is controlled through the
        # QAction that addWidget returns. Hiding the widget directly does not
        # stick: the toolbar re-shows it during its next relayout. Toggle the
        # action instead.
        self._action = toolbar.addWidget(self.bar)
        self._action.setVisible(False)

    def set_busy(self, folder: str) -> None:
        self._action.setVisible(True)
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self.bar.setFormat(f"Scanning {os.path.basename(folder)}...")

    def update(self, done: int, total: int, path: str) -> None:
        self._action.setVisible(True)
        self.bar.setVisible(True)
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)
        name = os.path.basename(path) if path else ""
        self.bar.setFormat(f"{done} / {total}  ({int(done * 100 / max(1, total))}%)  {name}")

    def finish(self) -> None:
        self._action.setVisible(False)
        self.bar.setVisible(False)


def run_gui(initial_folder: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow(initial_folder)
    window.show()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    folder = args[0] if args else None
    return run_gui(folder)


if __name__ == "__main__":
    raise SystemExit(main())