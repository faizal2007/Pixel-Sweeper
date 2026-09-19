"""Perceptual hashing of images.

Implements two 64-bit image hashes that are robust to resizing,
recompression and small edits:

* ``perceptual_hash`` - a DCT-based pHash (the classic "pHash"),
  computed over the low-frequency 8x8 block of a 32x32 greyscale DCT.
* ``difference_hash`` - a cheap horizontal-difference hash (dHash).

Comparison uses Hamming distance: two images are considered
near-duplicates when their hashes differ in a handful of bit positions.
"""

from __future__ import annotations

import math

from PIL import Image, ImageOps

DCT_SIZE = 32
HASH_SIZE = 8

# Phone cameras comfortably beat Pillow's default guard: a 200 MP photo is
# 199,756,800 pixels against a default ceiling of 89,478,485, so those files
# were refused as suspected decompression bombs and dropped from the scan
# without a word. These are images the user pointed us at, so the ceiling is
# raised past any real camera - while still refusing something absurd.
Image.MAX_IMAGE_PIXELS = 500_000_000

_M2 = math.sqrt(2.0)


def _open_image(path: str) -> Image.Image:
    image = Image.open(path)
    # Ask the decoder for a small picture up front. For JPEG that makes libjpeg
    # decode with scaled DCTs, so a 200 MP photo costs a few MB instead of
    # ~600 MB, and it is quicker too. The hashes only ever look at a 32x32
    # image, so nothing is lost. Other formats ignore this, as before.
    image.draft("L", (DCT_SIZE, DCT_SIZE))
    image = ImageOps.exif_transpose(image)
    return image.convert("L")


def _dct2d(size: int, pixels: list[list[float]]) -> list[list[float]]:
    """Separable 2D DCT-II of an NxN square matrix."""
    coeff = [math.sqrt(1.0 / size) if k == 0 else math.sqrt(2.0 / size) for k in range(size)]
    base = math.pi / (2.0 * size)

    # Transform along rows.
    rows = []
    for y in range(size):
        src = pixels[y]
        row = []
        for u in range(size):
            cu = coeff[u]
            an = base * u
            acc = 0.0
            for x in range(size):
                acc += src[x] * math.cos((2 * x + 1) * an)
            row.append(cu * acc)
        rows.append(row)

    # Transform along columns.
    out = []
    for y in range(size):
        out.append([0.0] * size)
    for x in range(size):
        for v in range(size):
            cv = coeff[v]
            an = base * v
            acc = 0.0
            for y in range(size):
                acc += rows[y][x] * math.cos((2 * y + 1) * an)
            out[v][x] = cv * acc
    return out


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def perceptual_hash(path: str) -> int:
    """Return a 64-bit pHash integer for the image at ``path``."""
    image = _open_image(path).resize((DCT_SIZE, DCT_SIZE), Image.Resampling.BILINEAR)
    pixels = [[float(image.getpixel((x, y))) for x in range(DCT_SIZE)] for y in range(DCT_SIZE)]
    dct = _dct2d(DCT_SIZE, pixels)
    frequencies = [dct[y][x] for y in range(HASH_SIZE) for x in range(HASH_SIZE)]
    med = _median(frequencies)
    bits = 0
    for i, value in enumerate(frequencies):
        if value > med:
            bits |= 1 << i
    return bits


def difference_hash(path: str) -> int:
    """Return a 64-bit dHash integer for the image at ``path``."""
    image = _open_image(path).resize((HASH_SIZE + 1, HASH_SIZE), Image.Resampling.BILINEAR)
    bits = 0
    i = 0
    for y in range(HASH_SIZE):
        for x in range(HASH_SIZE):
            if image.getpixel((x, y)) > image.getpixel((x + 1, y)):
                bits |= 1 << i
            i += 1
    return bits


def hamming(a: int, b: int) -> int:
    """Number of differing bit positions between two hashes."""
    return int(bin(a ^ b).count("1"))