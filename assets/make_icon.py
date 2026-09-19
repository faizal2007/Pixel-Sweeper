"""Rasterise the Pixel Sweeper artwork into the icons the app and exe use.

    python assets/make_icon.py

The artwork is the two SVG files next to this script - edit those, then re-run
this to regenerate ``pixelsweeper.ico`` (all the sizes Windows asks for) and
``pixelsweeper-256.png`` for documentation.

Each size is rendered natively rather than downscaled from one big bitmap, and
sizes below 32 px use the simplified drawing where the photo glyph would
otherwise be an unreadable smudge. Requires PyQt6 (for QtSvg) and Pillow, both
of which the project already depends on.
"""

from __future__ import annotations

import io
import os
import sys

from PIL import Image
from PyQt6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

HERE = os.path.dirname(os.path.abspath(__file__))
SIZES = (16, 24, 32, 48, 64, 128, 256)
SIMPLIFIED_BELOW = 32
ICO = os.path.join(HERE, "pixelsweeper.ico")
PNG = os.path.join(HERE, "pixelsweeper-256.png")


def artwork(size: int) -> str:
    name = "pixelsweeper-small.svg" if size < SIMPLIFIED_BELOW else "pixelsweeper.svg"
    return os.path.join(HERE, name)


def render(size: int) -> Image.Image:
    """Draw the artwork for ``size`` straight into a bitmap of that size."""
    renderer = QSvgRenderer(artwork(size))
    if not renderer.isValid():
        raise SystemExit(f"could not read {artwork(size)}")

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()

    # Round-trip through PNG rather than poking at raw pixels: exact, and it
    # keeps the alpha channel intact.
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGBA")


def main() -> int:
    frames = {size: render(size) for size in SIZES}

    largest = max(SIZES)
    frames[largest].save(
        ICO,
        format="ICO",
        sizes=[(size, size) for size in SIZES],
        append_images=[frames[size] for size in SIZES if size != largest],
    )
    frames[largest].save(PNG, format="PNG")

    written = Image.open(ICO)
    print(f"{ICO}")
    print(f"  sizes in the file: {sorted(size[0] for size in written.ico.sizes())}")
    print(f"  written from     : {len(SIZES)} native renders")
    print(f"{PNG}  {frames[largest].size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
