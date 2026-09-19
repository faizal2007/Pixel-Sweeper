"""PyQt6 gallery that scans an image library and shows images sorted by
duplicate groups.

Usage: ``python main.py [folder]`` or pick a folder from the toolbar.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from io import BytesIO

from PIL import Image, ImageOps
from PyQt6.QtCore import (
    QAbstractItemModel,
    QAbstractListModel,
    QDir,
    QEvent,
    QFile,
    QModelIndex,
    QPointF,
    QRect,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFileSystemModel,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
)
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
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QToolBar,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .hashing import hamming
from .scanner import (
    DEFAULT_THRESHOLD,
    ImageRecord,
    ScanCancelled,
    ScanResult,
    scan_folder,
)

_DEFAULT_GROUP_ROLE = int(Qt.ItemDataRole.UserRole)

_THUMB_PIXEL = 200
# Full-size preview ceiling. 4096 covers any screen at 1:1 and keeps the pixmap
# to ~50 MB, where a 200 MP photo at native size would be about 600 MB.
_FULL_PIXEL = 4096
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


def _forget_thumb(path: str) -> None:
    """Drop the cached thumbnail of a file that no longer exists."""
    _THUMB_CACHE.pop(path, None)
    disk_path = _thumb_disk_path(path)
    if disk_path:
        try:
            os.remove(disk_path)
        except OSError:
            pass


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
            # Decode at roughly the size we are going to show, so a 200 MP photo
            # does not have to be unpacked in full first. Ignored by formats
            # that cannot do it, and it never scales anything up.
            image.draft("RGB", (max_size, max_size))
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
    return _build_pixmap(path, _FULL_PIXEL)


def _count(count: int, noun: str) -> str:
    """'1 image', '18 images' - plural only when it should be."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _fit_to_screen(
    width: int, height: int, available: QSize, margin: int = 48
) -> QSize:
    """A preferred window size, shrunk to what the screen actually has.

    Widget sizes are in logical pixels, so a 1200x900 window on a 150% display
    is 1800x1350 real pixels - taller than a 1280x800 desktop, which pushes the
    bottom row of buttons off the screen. The margin leaves room for the title
    bar and window frame, which live outside the client area.
    """
    return QSize(
        max(480, min(width, available.width() - margin)),
        max(360, min(height, available.height() - margin)),
    )


def _screen_limited(width: int, height: int, widget: QWidget | None = None) -> QSize:
    """``_fit_to_screen`` against the screen ``widget`` is (or will be) on."""
    screen = widget.screen() if widget is not None else None
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        return QSize(width, height)
    return _fit_to_screen(width, height, screen.availableGeometry())


def _hsize(value: int) -> str:
    units = ("B", "KB", "MB", "GB")
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} GB"


class ThumbnailModel(QAbstractListModel):
    """Model exposing lazy, cached thumbnails, captions and a tick state.

    The tick state lives here (as ``CheckStateRole``) rather than in the view
    so it survives repaints and resizes, and so the deletion code has a single
    place to ask "what did the user pick?".
    """

    checked_changed = pyqtSignal()

    def __init__(self, records: list[ImageRecord], parent: QWidget | None = None):
        super().__init__(parent)
        self.records = list(records)
        # True while this model is the streamed preview shown during a scan,
        # where images are still arriving and ticking would be thrown away.
        self.live = False
        self._checked: set[str] = set()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.records)

    def add_records(self, records: list[ImageRecord]) -> None:
        """Append streamed records instead of rebuilding the whole model."""
        if not records:
            return
        first = len(self.records)
        self.beginInsertRows(QModelIndex(), first, first + len(records) - 1)
        self.records.extend(records)
        self.endInsertRows()

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        # Not ItemIsUserCheckable: the delegate alone paints the box and turns
        # clicks into ticks, so Qt's own checkable machinery (indicator, and the
        # QCheckBox editor it can drop on top of the tile) stays out of the way.
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

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
        if role == Qt.ItemDataRole.CheckStateRole:
            return (
                Qt.CheckState.Checked
                if record.path in self._checked
                else Qt.CheckState.Unchecked
            )
        return None

    def setData(
        self,
        index: QModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != Qt.ItemDataRole.CheckStateRole or not index.isValid():
            return False
        checked = Qt.CheckState(value) == Qt.CheckState.Checked
        self._notify(self._apply([index.row()], checked))
        return True

    def is_checked(self, row: int) -> bool:
        return 0 <= row < len(self.records) and self.records[row].path in self._checked

    def checked_count(self) -> int:
        return sum(1 for record in self.records if record.path in self._checked)

    def checked_paths(self) -> list[str]:
        """Ticked paths in the order the gallery shows them."""
        return [record.path for record in self.records if record.path in self._checked]

    def toggle_checked(self, rows: list[int], checked: bool | None = None) -> None:
        """Tick ``rows``. With ``checked=None`` the state of the first row is
        inverted and applied to all of them (what Space does)."""
        if not rows:
            return
        if checked is None:
            checked = not self.is_checked(rows[0])
        self._notify(self._apply(rows, checked))

    def set_all_checked(self, checked: bool, rows: list[int] | None = None) -> None:
        target = range(len(self.records)) if rows is None else rows
        self._notify(self._apply(list(target), checked))

    def _apply(self, rows: list[int], checked: bool) -> list[int]:
        """Store the new state and return only the rows that really changed."""
        changed: list[int] = []
        for row in rows:
            if not 0 <= row < len(self.records):
                continue
            path = self.records[row].path
            if checked == (path in self._checked):
                continue
            if checked:
                self._checked.add(path)
            else:
                self._checked.discard(path)
            changed.append(row)
        return changed

    def _notify(self, changed: list[int]) -> None:
        if not changed:
            return
        self.dataChanged.emit(
            self.index(min(changed), 0),
            self.index(max(changed), 0),
            [Qt.ItemDataRole.CheckStateRole],
        )
        self.checked_changed.emit()


_CHECK_COLOR = QColor("#1a73e8")
_CHECK_SIZE = 22
_STRIP_HEIGHT = 30  # caption row: tick box + filename, always below the picture
_STRIP_PAD = 6
_CELL_EXTRA = 8  # keeps a little air either side of the widest thumbnail


def _image_area(item_rect: QRect) -> QRect:
    """The part of a tile that holds the picture."""
    return QRect(
        item_rect.left(),
        item_rect.top(),
        item_rect.width(),
        max(0, item_rect.height() - _STRIP_HEIGHT),
    )


def _caption_strip(item_rect: QRect) -> QRect:
    """The bottom row of a tile. The tick box lives here, so it can never
    cover the image."""
    height = min(_STRIP_HEIGHT, max(0, item_rect.height()))
    return QRect(
        item_rect.left(),
        item_rect.bottom() - height + 1,
        item_rect.width(),
        height,
    )


def _checkbox_rect(item_rect: QRect) -> QRect:
    """Where the tick box sits inside one tile."""
    strip = _caption_strip(item_rect)
    return QRect(
        strip.left() + _STRIP_PAD,
        strip.top() + max(0, (strip.height() - _CHECK_SIZE) // 2),
        _CHECK_SIZE,
        _CHECK_SIZE,
    )


def _is_live(index: QModelIndex) -> bool:
    """True while a model is the streamed scan preview rather than a result."""
    return bool(getattr(index.model(), "live", False))


class ThumbnailDelegate(QStyledItemDelegate):
    """Draws exactly one tick box per tile, in a caption row under the image.

    The box is painted here in full, instead of drawing with
    ``QStyledItemDelegate.paint`` and adding the box on top. That is not just
    tidiness: ``initStyleOption`` (called inside the base ``paint``) sets
    ``HasCheckIndicator`` whenever ``CheckStateRole`` holds a value, so the base
    paints Qt's own checkbox as well - two boxes per tile, one of them at the
    style's own spot on the picture. Painting everything here means the style
    only ever draws the background, and the single box is ours.
    """

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        # One size for every tile keeps the grid even and guarantees room for
        # both the picture and the caption row.
        return QSize(_THUMB_PIXEL + _CELL_EXTRA, _THUMB_PIXEL + _STRIP_HEIGHT)

    def paint(
        self,
        painter: QPainter | None,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        if painter is None:
            return
        style = (
            option.widget.style()
            if option.widget is not None
            else QApplication.style()
        )
        # Background and selection only - no text, no icon, no check indicator.
        background = QStyleOptionViewItem(option)
        background.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        style.drawPrimitive(
            QStyle.PrimitiveElement.PE_PanelItemViewItem,
            background,
            painter,
            option.widget,
        )
        if option.state & QStyle.StateFlag.State_HasFocus:
            style.drawPrimitive(
                QStyle.PrimitiveElement.PE_FrameFocusRect,
                background,
                painter,
                option.widget,
            )

        live = _is_live(index)
        checked = (
            not live
            and index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        )
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        area = _image_area(option.rect)
        strip = _caption_strip(option.rect)
        box = _checkbox_rect(option.rect)

        # The picture, centred in its own area and never blown up.
        thumbnail = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(thumbnail, QPixmap) and not thumbnail.isNull():
            shown = thumbnail
            if shown.width() > area.width() or shown.height() > area.height():
                shown = shown.scaled(
                    area.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            painter.drawPixmap(
                area.left() + (area.width() - shown.width()) // 2,
                area.top() + (area.height() - shown.height()) // 2,
                shown,
            )

        # A tint confined to the caption row, so nothing covers the picture.
        if checked:
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(26, 115, 232, 40))
            painter.drawRoundedRect(strip.adjusted(1, 1, -1, -1), 4, 4)
            painter.restore()

        if not live:
            self._paint_box(painter, option, box, checked)

        metrics = QFontMetrics(option.font)
        # With no tick box in the preview, the filename starts at the strip edge.
        text_left = (
            strip.left() + _STRIP_PAD if live else box.right() + _STRIP_PAD
        )
        text_rect = QRect(
            text_left,
            strip.top(),
            max(0, strip.right() - text_left),
            strip.height(),
        )
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if checked:
            colour = _CHECK_COLOR
        elif selected:
            colour = option.palette.color(QPalette.ColorRole.HighlightedText)
        else:
            colour = option.palette.color(QPalette.ColorRole.Text)
        painter.save()
        painter.setPen(colour)
        painter.setFont(option.font)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, text_rect.width()),
        )
        painter.restore()

    @staticmethod
    def _paint_box(
        painter: QPainter,
        option: QStyleOptionViewItem,
        box: QRect,
        checked: bool,
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if checked:
            painter.setPen(QPen(_CHECK_COLOR, 2))
            painter.setBrush(_CHECK_COLOR)
            painter.drawRoundedRect(box, 5, 5)
            pen = QPen(QColor("#ffffff"), 3)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            x, y, w, h = box.x(), box.y(), box.width(), box.height()
            painter.drawLine(
                QPointF(x + w * 0.24, y + h * 0.54), QPointF(x + w * 0.44, y + h * 0.74)
            )
            painter.drawLine(
                QPointF(x + w * 0.44, y + h * 0.74), QPointF(x + w * 0.77, y + h * 0.29)
            )
        else:
            painter.setPen(QPen(option.palette.color(QPalette.ColorRole.Mid), 2))
            painter.setBrush(option.palette.color(QPalette.ColorRole.Base))
            painter.drawRoundedRect(box, 5, 5)
        painter.restore()

    def editorEvent(
        self,
        event: QEvent | None,
        model: QAbstractItemModel,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> bool:
        """Turns a click on the box into a tick; the box is the only target.

        The base implementation would additionally toggle inside the
        style-computed indicator rect, which is *not* where we paint the box -
        clicking there would flip an invisible checkbox. So mouse events are
        handled here in full and never passed on.
        """
        if event is None:
            return False

        kind = event.type()
        if kind not in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
        ):
            return super().editorEvent(event, model, option, index)

        if _is_live(index):
            # Streaming preview: nothing is tickable until the scan is done.
            return False

        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if not _checkbox_rect(option.rect).contains(event.position().toPoint()):
            return False

        if kind == QEvent.Type.MouseButtonPress:
            model.setData(
                index,
                Qt.CheckState.Unchecked
                if index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
                else Qt.CheckState.Checked,
                Qt.ItemDataRole.CheckStateRole,
            )
        # Consume the matching release / double click: it must not toggle a
        # second time, and it must not open the preview either.
        return True


class ThumbnailList(QListView):
    """Icon-mode list that keeps double clicks off the tick boxes."""

    toggle_selection_requested = pyqtSignal()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and _checkbox_rect(self.visualRect(index)).contains(
            event.position().toPoint()
        ):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Space:
            self.toggle_selection_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class GalleryView(QWidget):
    """Thumbnail grid with a tick box per image and a delete action."""

    delete_requested = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.model: ThumbnailModel | None = None
        self._live = False
        self._live_heading = ""

        self.title = QLabel("Open a folder to start.")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-weight: 700; padding: 4px;")

        self._idle_hint = (
            "Tick the images you want to remove - click the box in the corner of "
            "a thumbnail, or select some and press Space - then press Delete Checked."
        )
        self.hint = QLabel(self._idle_hint)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: palette(mid); padding: 0 4px 2px 4px;")

        self.check_all_button = QPushButton("Check All")
        self.check_all_button.setToolTip("Tick every image shown here.")
        self.check_all_button.clicked.connect(self._check_every_image)

        self.uncheck_all_button = QPushButton("Uncheck All")
        self.uncheck_all_button.setToolTip("Remove every tick.")
        self.uncheck_all_button.clicked.connect(self._clear_every_tick)

        self.check_rest_button = QPushButton("Check All Except First")
        self.check_rest_button.setToolTip(
            "Keep the first image of this duplicate group and tick the others, "
            "so a group can be cleaned up in one click."
        )
        self.check_rest_button.clicked.connect(self._check_all_but_first)

        self.delete_button = QPushButton("Delete Checked")
        self.delete_button.setToolTip(
            "Move the ticked images to the recycle bin, where they stay recoverable."
        )
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._delete_checked)

        actions = QHBoxLayout()
        actions.setContentsMargins(4, 0, 4, 0)
        actions.addWidget(self.check_all_button)
        actions.addWidget(self.uncheck_all_button)
        actions.addWidget(self.check_rest_button)
        actions.addStretch(1)
        actions.addWidget(self.delete_button)

        self.list = ThumbnailList()
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setUniformItemSizes(True)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setGridSize(QSize(_THUMB_PIXEL + 12, _THUMB_PIXEL + _STRIP_HEIGHT + 6))
        self.list.setIconSize(QSize(_THUMB_PIXEL, _THUMB_PIXEL))
        self.list.setItemDelegate(ThumbnailDelegate(self.list))
        self.list.doubleClicked.connect(self._open_viewer)
        self.list.toggle_selection_requested.connect(self._toggle_selected)

        delete_shortcut = QShortcut(QKeySequence("Delete"), self)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        delete_shortcut.activated.connect(self._delete_checked)

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(4, 4, 4, 4)
        self.layout.addWidget(self.title)
        self.layout.addWidget(self.hint)
        self.layout.addLayout(actions)
        self.layout.addWidget(self.list, 1)

    def show_raw_folder(self):
        self.set_records([], "Open a folder, or choose one in the toolbar.")

    def set_records(self, records: list[ImageRecord], heading: str) -> None:
        self.end_live()
        previous = self.model
        self.model = ThumbnailModel(records, self)
        self.model.checked_changed.connect(self._on_checked_changed)
        self.list.setModel(self.model)
        self.title.setText(heading)
        self._on_checked_changed()
        if previous is not None:
            # Every click in the tree builds a fresh model; release the old one
            # instead of letting it pile up as an unused child widget.
            previous.deleteLater()

    def begin_live(self, heading: str) -> None:
        """Show images as they arrive, while the scan is still running.

        The preview is deliberately read-only. What streams in is a flat list of
        everything found so far, not the duplicate groups, so anything ticked
        here would be silently discarded the moment the scan finishes.
        """
        self._live = True
        self._live_heading = heading
        previous, self.model = self.model, ThumbnailModel([], self)
        self.model.live = True
        self.model.checked_changed.connect(self._on_checked_changed)
        self.list.setModel(self.model)
        self.title.setText(f"{heading}  -  no images yet")
        self.hint.setText(
            "Thumbnails appear as they are scanned. Ticking is available once "
            "the scan finishes."
        )
        self._set_action_buttons_enabled(False)
        self._on_checked_changed()
        if previous is not None:
            previous.deleteLater()

    def append_records(self, records: list[ImageRecord]) -> None:
        """Add the next batch of images found by a running scan."""
        if self.model is None or not self._live or not records:
            return
        self.model.add_records(records)
        self.title.setText(
            f"{self._live_heading}  -  {len(self.model.records)} images so far"
        )

    def end_live(self) -> None:
        """Leave the streaming preview; the caller supplies the real records."""
        if not self._live:
            return
        self._live = False
        self.hint.setText(self._idle_hint)
        self._set_action_buttons_enabled(True)
        if self.model is not None:
            self.model.live = False
            self.list.viewport().update()

    def is_live(self) -> bool:
        return self._live

    def record_count(self) -> int:
        return len(self.model.records) if self.model is not None else 0

    def _set_action_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self.check_all_button,
            self.uncheck_all_button,
            self.check_rest_button,
        ):
            button.setEnabled(enabled)
        if not enabled:
            self.delete_button.setEnabled(False)

    def _selected_rows(self) -> list[int]:
        selection = self.list.selectionModel()
        if selection is None:
            return []
        return sorted(index.row() for index in selection.selectedIndexes())

    def _toggle_selected(self) -> None:
        if self.model is not None and not self._live:
            self.model.toggle_checked(self._selected_rows())

    def _check_every_image(self) -> None:
        if self.model is not None:
            self.model.set_all_checked(True)

    def _clear_every_tick(self) -> None:
        if self.model is not None:
            self.model.set_all_checked(False)

    def _check_all_but_first(self) -> None:
        """Keep one copy of a duplicate group, tick the rest."""
        if self.model is None or not self.model.records:
            return
        self.model.set_all_checked(False)
        self.model.set_all_checked(True, list(range(1, len(self.model.records))))

    def _on_checked_changed(self) -> None:
        count = (
            0 if self._live or self.model is None else self.model.checked_count()
        )
        self.delete_button.setText(
            f"Delete Checked ({count})" if count else "Delete Checked"
        )
        self.delete_button.setEnabled(count > 0)

    def _delete_checked(self) -> None:
        if self.model is None or self._live:
            return
        paths = self.model.checked_paths()
        if paths:
            self.delete_requested.emit(paths)

    def _open_viewer(self, index: QModelIndex) -> None:
        if self.model is None or not index.isValid():
            return
        records = list(self.model.records)
        viewer = ImageViewer(records, index.row(), self)
        # The viewer deletes single images; the window handles the move in the
        # same way it handles the ticked ones.
        viewer.delete_requested.connect(self.delete_requested)
        viewer.show()


class ImageViewer(QDialog):
    """Full-size preview of one image, with prev/next and a delete button."""

    delete_requested = pyqtSignal(list)

    def __init__(
        self,
        records: list[ImageRecord],
        start: int,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Image viewer")
        self._records = records
        self._index = max(0, min(start, len(records) - 1))
        self._records_on_show = False
        # Fitted by default: opening a 4000 px photo at 1:1 shows a corner of it,
        # which says nothing about the picture as a whole.
        self._actual_size = False
        self._source = QPixmap()

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(120, 120)

        self.area = QScrollArea()
        self.area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.area.setWidget(self.image_label)
        self._apply_fit_mode()

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setAlignment(Qt.AlignmentFlag.AlignCenter)

        prev_button = QPushButton("< Prev")
        next_button = QPushButton("Next >")
        prev_button.clicked.connect(self._previous)
        next_button.clicked.connect(self._next)

        self.actual_button = QPushButton("Actual size")
        self.actual_button.setCheckable(True)
        self.actual_button.setToolTip(
            "Show the picture at 1:1 with scroll bars, instead of fitted to the "
            "window - useful for checking whether two photos really match."
        )
        self.actual_button.toggled.connect(self._set_actual_size)

        delete_button = QPushButton("Delete This Image")
        delete_button.setToolTip(
            "Move the image on screen to the recycle bin, where it stays "
            "recoverable. The gallery re-scans afterwards."
        )
        delete_button.clicked.connect(self._delete_current)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(prev_button)
        buttons.addWidget(next_button)
        buttons.addWidget(self.actual_button)
        buttons.addStretch(1)
        # Kept apart from Prev/Next so it is not hit by accident.
        buttons.addWidget(delete_button)

        self.layout = QVBoxLayout(self)
        self.layout.addWidget(self.area, 1)
        self.layout.addWidget(self.info)
        self.layout.addLayout(buttons)

        QShortcut(QKeySequence("Left"), self, activated=self._previous)
        QShortcut(QKeySequence("Right"), self, activated=self._next)
        QShortcut(QKeySequence("Delete"), self, activated=self._delete_current)

        self.resize(_screen_limited(1200, 900, self))
        self._records_on_show = True
        self._show_current()

    def _apply_fit_mode(self) -> None:
        """A resizable area centres a shrunk picture; a fixed one keeps it at its
        own size and grows scroll bars rather than cropping."""
        self.area.setWidgetResizable(not self._actual_size)

    def _set_actual_size(self, actual: bool) -> None:
        self._actual_size = actual
        self._apply_fit_mode()
        self._refresh()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if not self._actual_size:
            # Stay fitted to the window as it changes size.
            self._refresh()

    def _delete_current(self) -> None:
        """Ask for the image on screen to be trashed, then close if it went.

        The signal is delivered straight away, so by the time it returns the
        user has either confirmed the move - the file is gone, and this preview
        closes because the list behind it is stale - or cancelled, in which case
        the file is still there and nothing here changes.
        """
        record = self._records[self._index]
        self.delete_requested.emit([record.path])
        if not os.path.isfile(record.path):
            self.accept()

    def _centre_view(self) -> None:
        """Open on the middle of a picture, rather than its top-left corner."""
        for bar in (
            self.area.horizontalScrollBar(),
            self.area.verticalScrollBar(),
        ):
            bar.setValue((bar.minimum() + bar.maximum()) // 2)

    def _refresh(self) -> None:
        """Draw the source pixmap at whichever zoom is selected."""
        if not self._records:
            return
        record = self._records[self._index]
        source = self._source

        if source.isNull():
            self.image_label.setPixmap(source)
            shown = "this image cannot be previewed"
        else:
            displayed = source
            if not self._actual_size:
                window = self.area.viewport().size()
                if (
                    source.width() > window.width()
                    or source.height() > window.height()
                ):
                    displayed = source.scaled(
                        window,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
            self.image_label.setPixmap(displayed)
            if self._actual_size:
                self.image_label.resize(displayed.size())

            if (displayed.width(), displayed.height()) == (
                record.width,
                record.height,
            ):
                shown = "shown at full size"
            elif self._actual_size:
                shown = (
                    f"shown at {displayed.width()} x {displayed.height()} "
                    f"(of {record.width} x {record.height})"
                )
            else:
                shown = (
                    f"fitted to the window ({displayed.width()} x {displayed.height()})"
                )

        anchor = self._records[0]
        similarity = hamming(anchor.phash, record.phash)
        self.info.setText(
            f"{self._index + 1} / {len(self._records)}\n"
            f"{record.path}\n"
            f"{record.width} x {record.height}   {_hsize(record.size)}   "
            f"hash diff to first: {similarity} bits\n"
            f"{shown}"
        )

    def _after_layout(self) -> None:
        """Fit against the real viewport size, once the dialog has been laid out."""
        self._refresh()
        self._centre_view()

    def _show_current(self) -> None:
        self._records_on_show = False
        record = self._records[self._index]
        self.setWindowTitle(
            f"{os.path.basename(record.path)} ({self._index + 1}/{len(self._records)})"
        )
        self._source = _load_full_pixmap(record.path)
        self._refresh()
        self._records_on_show = True
        # The viewport size is only final after the layout has run, so fit again
        # then - otherwise the first picture is scaled to a stale size.
        QTimer.singleShot(0, self._after_layout)

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

    def clear_queued(self) -> None:
        """Undo ``notify_queued`` when the queued folder gets dropped."""
        self._queued = False
        self._update_label()


class ScanThread(QThread):
    progress = pyqtSignal(int, int, str)
    records_found = pyqtSignal(list)
    scan_done = pyqtSignal(object)
    scan_failed = pyqtSignal(str)
    scan_cancelled = pyqtSignal(str)

    # Records reach the GUI in batches: one signal per image would flood the
    # event loop on a large library, and the first batch goes out at once so the
    # gallery starts filling immediately.
    _BATCH_SIZE = 48
    _BATCH_SECONDS = 0.15

    def __init__(
        self,
        folder: str,
        cache_dir: str | None = None,
        threshold: int = DEFAULT_THRESHOLD,
    ):
        super().__init__()
        self.folder = folder
        self.cache_dir = cache_dir
        self.threshold = threshold
        self._cancel = threading.Event()
        self._batch: list[ImageRecord] = []
        self._last_flush = 0.0

    def cancel(self) -> None:
        """Ask the scan to stop early. Safe to call from the GUI thread."""
        self._cancel.set()

    def _collect(self, record: ImageRecord) -> None:
        """Called from the scanning thread for every image it identifies."""
        self._batch.append(record)
        if (
            len(self._batch) >= self._BATCH_SIZE
            or time.monotonic() - self._last_flush >= self._BATCH_SECONDS
        ):
            self._flush()

    def _flush(self) -> None:
        if self._batch:
            self.records_found.emit(self._batch)
            self._batch = []
        self._last_flush = time.monotonic()

    def run(self) -> None:
        try:
            result = scan_folder(
                self.folder,
                threshold=self.threshold,
                on_progress=self.progress.emit,
                cache_dir=self.cache_dir,
                should_cancel=self._cancel.is_set,
                on_record=self._collect,
            )
        except ScanCancelled:
            self.scan_cancelled.emit(self.folder)
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the GUI
            self.scan_failed.emit(str(exc))
            return
        if self._cancel.is_set():
            # Cancelled during the grouping step, after hashing: the result is
            # complete but the user asked to stop, so it must not be shown.
            self.scan_cancelled.emit(self.folder)
            return
        self._flush()
        self.scan_done.emit(result)


class MainWindow(QMainWindow):
    scan_queued = pyqtSignal(str)

    def __init__(self, initial_folder: str | None = None):
        super().__init__()
        self.result: ScanResult | None = None
        self.result_folder: str | None = None
        self.thread: ScanThread | None = None
        self.current_folder: str | None = None
        self.pending_folder: str | None = None
        self._cancelling = False
        self.settings = QSettings("ImageDuplicates", "ImageDuplicateGallery")
        self.last_folder_key = "last_folder"
        # Fixed, not user-adjustable: 10 bits is the value that reliably catches
        # re-saved and resized copies without pairing photos that merely look
        # similar. Exposed here so the one place to change it is obvious.
        self.threshold = DEFAULT_THRESHOLD

        self.setWindowTitle("Pixel Sweeper")
        # Big enough to work in, but never taller than the desktop: the status
        # bar and the toolbar live at the edges.
        self.resize(_screen_limited(1280, 820, self))

        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)
        open_action = toolbar.addAction("Open Folder...")
        open_action.setShortcut(QKeySequence("Ctrl+O"))
        open_action.triggered.connect(self.choose_folder)
        toolbar.addSeparator()
        self.threshold_label = QLabel(f" Similarity threshold: {self.threshold} bits ")
        self.threshold_label.setToolTip(
            "How far apart two hashes may be and still count as duplicates.\n"
            "This is fixed at "
            f"{self.threshold} bits: strict enough to catch re-saved, resized\n"
            "and recompressed copies, without pairing photos that only look alike."
        )
        toolbar.addWidget(self.threshold_label)
        self.progress_bar = _ToolbarProgress(toolbar, self.cancel_scan)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(self._on_tree_change)
        splitter.addWidget(self.tree)
        splitter.addWidget(GalleryView(self))
        splitter.setSizes([380, 900])
        self.setCentralWidget(splitter)

        self.gallery: GalleryView = splitter.widget(1)
        self.gallery.delete_requested.connect(self.move_checked_to_trash)

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
        self.gallery.begin_live(f"Scanning {folder}")
        self.counts_label.setText("Scanning...")
        self.status.showMessage(f"Scanning {folder}...")
        self.progress_bar.set_busy(folder)

        cache_dir: str | None = None
        cache_root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
        if cache_root:
            cache_dir = os.path.join(cache_root, "index")
        self.thread = ScanThread(folder, cache_dir, self.threshold)
        self.thread.progress.connect(self._on_progress)
        self.thread.records_found.connect(self._on_records_found)
        self.thread.scan_done.connect(self._on_scan_done)
        self.thread.scan_failed.connect(self._on_scan_failed)
        self.thread.scan_cancelled.connect(self._on_scan_cancelled)
        self.thread.start()

    def cancel_scan(self) -> None:
        """Stop the running scan and drop whatever folder is queued behind it."""
        dropped, self.pending_folder = self.pending_folder, None
        self.folder_picker.clear_queued()

        running = self.thread is not None and self.thread.isRunning()
        if not running:
            self.progress_bar.finish()
            self.status.showMessage(
                f"Dropped the queued folder: {dropped}" if dropped else "Nothing to cancel."
            )
            return

        self._cancelling = True
        self.thread.cancel()
        self.progress_bar.set_cancelling()
        self.status.showMessage(
            f"Cancelling scan... (queued folder dropped: {dropped})"
            if dropped
            else "Cancelling scan..."
        )

    def _on_progress(self, done: int, total: int, path: str) -> None:
        if self._cancelling:
            # Late updates must not bring the progress bar back after a cancel.
            return
        self.progress_bar.update(done, total, path)

    def _on_records_found(self, records: list[ImageRecord]) -> None:
        """Show images as the scan identifies them, rather than at the end."""
        self.gallery.append_records(records)
        found = self.gallery.record_count()
        self.counts_label.setText(f"Scanning... {found} images found so far")

    def _on_scan_failed(self, message: str) -> None:
        self.tree.setEnabled(True)
        self.progress_bar.finish()
        self.status.showMessage("Scan failed.")
        QMessageBox.critical(self, "Scan failed", message)
        next_folder, self.pending_folder = self.pending_folder, None
        if next_folder:
            self.status.showMessage(f"Scan failed - moving on to {next_folder}.")
            self.start_scan(next_folder)

    def _display_result(self, result: ScanResult) -> None:
        self.result = result
        self.result_folder = self.current_folder
        self.tree.setEnabled(True)
        self.progress_bar.finish()
        self._populate_tree(result)
        if self.gallery.is_live():
            # Nothing was selectable in the tree (an empty folder), so the
            # streaming preview has to be dismissed by hand.
            self.gallery.set_records([], "No images found in this folder.")
        self._update_counts(result)
        notice = self._scan_notice(result)
        if notice:
            self.status.showMessage(f"{self.current_folder or ''}  -  {notice}")
        else:
            self.status.showMessage(self.current_folder or "")

    def _on_scan_done(self, result: ScanResult) -> None:
        self._display_result(result)
        next_folder, self.pending_folder = self.pending_folder, None
        if next_folder:
            self.start_scan(next_folder)

    def _on_scan_cancelled(self, folder: str) -> None:
        """Put the window back in a usable state after a cancelled scan."""
        self._cancelling = False
        self.progress_bar.finish()

        # The thread emits this from inside run(), so it is about to finish;
        # waiting here means a scan started right after this cannot end up
        # queued behind a thread that is still winding down.
        thread, self.thread = self.thread, None
        if thread is not None:
            thread.wait(5000)
            thread.deleteLater()

        if self.result is not None and self.result_folder:
            self.current_folder = self.result_folder
            self.settings.setValue(self.last_folder_key, self.result_folder)
            self._display_result(self.result)
            self.status.showMessage(
                f"Cancelled scanning {folder} - still showing {self.result_folder}."
            )
            return

        self.current_folder = None
        self.settings.remove(self.last_folder_key)
        self.tree.clear()
        self.tree.setEnabled(True)
        self.gallery.show_raw_folder()
        self.counts_label.setText("No images scanned")
        self.status.showMessage(f"Cancelled scanning {folder}.")

    def move_checked_to_trash(self, paths: list[str]) -> None:
        """Move the images the user ticked to the recycle bin, then re-scan."""
        existing = [path for path in paths if os.path.isfile(path)]
        if not existing:
            self.status.showMessage("Those images are already gone - re-scanning.")
            if self.current_folder:
                self.start_scan(self.current_folder)
            return

        sizes: dict[str, int] = {}
        for path in existing:
            try:
                sizes[path] = os.path.getsize(path)
            except OSError:
                sizes[path] = 0

        answer = QMessageBox.question(
            self,
            "Move to recycle bin",
            f"Move {len(existing)} image(s) ({_hsize(sum(sizes.values()))}) to the "
            "recycle bin?\n\nThey stay recoverable there, and nothing else is touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        moved: list[str] = []
        failed: list[str] = []
        for path in existing:
            if QFile.moveToTrash(path):
                moved.append(path)
                _forget_thumb(path)
            else:
                failed.append(path)

        if failed:
            preview = "\n".join(failed[:8])
            if len(failed) > 8:
                preview += f"\n... and {len(failed) - 8} more"
            QMessageBox.warning(
                self,
                "Some images were not moved",
                f"{len(failed)} of {len(existing)} image(s) could not be moved to "
                f"the recycle bin:\n{preview}",
            )

        freed = _hsize(sum(sizes[path] for path in moved))
        self.status.showMessage(
            f"Moved {len(moved)} image(s) to the recycle bin ({freed}) - re-scanning..."
        )
        if self.current_folder:
            self.start_scan(self.current_folder)

    def _update_counts(self, result: ScanResult) -> None:
        wasted = _hsize(result.wasted_bytes)
        text = (
            f"{result.total} images  |  {len(result.groups)} duplicate groups "
            f"({result.duplicated_count} images)  |  {len(result.uniques)} unique  "
            f"|  {wasted} wasted"
        )

        # Without this a folder that cannot be read just looks like an empty or
        # half-scanned one, with no hint as to why images are missing.
        problems: list[str] = []
        details: list[str] = []
        if result.skipped_dirs:
            problems.append(f"{len(result.skipped_dirs)} folder(s) unreadable")
            details.append("Folders that could not be read:")
            details += [
                f"  {path}  ({reason})" for path, reason in result.skipped_dirs[:20]
            ]
        if result.failed:
            problems.append(f"{len(result.failed)} image(s) failed")
            details.append("Images that could not be hashed:")
            details += [f"  {path}  ({reason})" for path, reason in result.failed[:20]]
        if problems:
            text += "  |  " + ", ".join(problems)

        self.counts_label.setText(text)
        self.counts_label.setToolTip("\n".join(details))

    @staticmethod
    def _scan_notice(result: ScanResult) -> str:
        """A short note about anything the scan could not read."""
        parts: list[str] = []
        if result.skipped_dirs:
            parts.append(f"{len(result.skipped_dirs)} folder(s) could not be read")
        if result.failed:
            parts.append(f"{len(result.failed)} image(s) could not be hashed")
        return " and ".join(parts)

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
                            f"- {self._relative(first.path)}"
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
            self._insert_folder_summary(result)
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

    def _relative(self, path: str) -> str:
        """A path as the user thinks of it: relative to the scanned folder."""
        if not self.current_folder:
            return os.path.basename(path)
        try:
            return os.path.relpath(path, self.current_folder)
        except ValueError:  # different drive
            return os.path.basename(path)

    def _folder_of(self, path: str) -> str:
        return os.path.dirname(self._relative(path))

    def _all_records(self, result: ScanResult) -> list[ImageRecord]:
        return [record for group in result.groups for record in group] + list(
            result.uniques
        )

    def _insert_folder_summary(self, result: ScanResult) -> None:
        """One node per folder that contributed images, with its count.

        Nothing else in the window names a folder: the gallery shows one flat
        grid, and group headings used to show only a file name. Without this
        there is no way to tell that a given subfolder was scanned at all.
        """
        by_folder: dict[str, list[ImageRecord]] = {}
        for record in self._all_records(result):
            # dirname() of a file sitting directly in the root is "", not ".".
            folder = self._folder_of(record.path) or "."
            by_folder.setdefault(folder, []).append(record)

        root = QTreeWidgetItem(self.tree, [f"Scanned Folders ({len(by_folder)})"])
        for folder in sorted(by_folder, key=lambda name: (name != ".", name.lower())):
            records = sorted(by_folder[folder], key=lambda r: r.path.lower())
            label = "(top level)" if folder == "." else folder
            item = QTreeWidgetItem(
                root, [f"{label}  ({_count(len(records), 'image')})"]
            )
            item.setData(0, _DEFAULT_GROUP_ROLE, records)

    def _on_tree_change(self, current: QTreeWidgetItem | None, _previous) -> None:
        if current is None:
            return
        records = current.data(0, _DEFAULT_GROUP_ROLE)
        if not records:
            return
        heading = f"{current.text(0)}  ({len(records)} images)"
        self.gallery.set_records(records, heading)


class _ToolbarProgress:
    """The toolbar progress bar, plus the Cancel button that goes with it."""

    def __init__(self, toolbar: QToolBar, on_cancel=None):
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

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip(
            "Stop the running scan and drop any folder queued behind it."
        )
        self.cancel_button.setEnabled(False)
        self._cancel_action = toolbar.addWidget(self.cancel_button)
        self._cancel_action.setVisible(False)
        if on_cancel is not None:
            self.cancel_button.clicked.connect(on_cancel)

    def _show(self) -> None:
        for action, widget in (
            (self._action, self.bar),
            (self._cancel_action, self.cancel_button),
        ):
            action.setVisible(True)
            widget.setVisible(True)

    def set_busy(self, folder: str) -> None:
        self._show()
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel")
        self.bar.setRange(0, 0)
        self.bar.setFormat(f"Scanning {os.path.basename(folder)}...")

    def update(self, done: int, total: int, path: str) -> None:
        self._show()
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)
        name = os.path.basename(path) if path else ""
        self.bar.setFormat(f"{done} / {total}  ({int(done * 100 / max(1, total))}%)  {name}")

    def set_cancelling(self) -> None:
        """Show that the cancel request was accepted, and stop repeat clicks."""
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling...")

    def finish(self) -> None:
        self._action.setVisible(False)
        self.bar.setVisible(False)
        self._cancel_action.setVisible(False)
        self.cancel_button.setVisible(False)
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(False)


def _asset_path(name: str) -> str:
    """Where a bundled asset lives, from a checkout or inside the packaged exe.

    PyInstaller unpacks ``datas`` next to the executable's temporary directory,
    reachable as ``sys._MEIPASS``; from source the assets sit beside the package.
    """
    roots = []
    frozen = getattr(sys, "_MEIPASS", "")
    if frozen:
        roots.append(frozen)
    roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for root in roots:
        candidate = os.path.join(root, "assets", name)
        if os.path.isfile(candidate):
            return candidate
    return ""


def run_gui(initial_folder: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    # One call covers every window, the preview and the dialogs. The packaged
    # exe also carries the icon in its own file, but that is not enough here:
    # without this, Qt shows its default logo in the title bar and taskbar.
    icon_path = _asset_path("pixelsweeper.ico")
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    window = MainWindow(initial_folder)
    window.show()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    folder = args[0] if args else None
    return run_gui(folder)


if __name__ == "__main__":
    raise SystemExit(main())