"""Entry point for the image duplicate gallery.

Usage:
    python main.py [folder]
"""

from __future__ import annotations

import sys

from image_duplicates.gallery import run_gui


def main() -> int:
    folder = sys.argv[1] if len(sys.argv) > 1 else None
    return run_gui(folder)


if __name__ == "__main__":
    raise SystemExit(main())