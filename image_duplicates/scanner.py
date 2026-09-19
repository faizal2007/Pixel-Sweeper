"""Walk an image library, hash every image and group near-duplicates.

Grouping strategy:

1. Every image is hashed (pHash + dHash) in a thread pool.
2. Images sharing the exact same pHash form a bucket.
3. Buckets are merged when their representative hashes are within a
   Hamming-distance threshold; a BK-tree makes the pairwise search fast
   even for large libraries.
4. Buckets that end up with two or more images are reported as duplicate
   groups; single-image buckets are the "unique" set.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from .hashing import difference_hash, hamming, perceptual_hash

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".tif", ".tiff", ".ico", ".qoi",
}

DEFAULT_THRESHOLD = 10


@dataclass(frozen=True)
class ImageRecord:
    path: str
    phash: int
    dhash: int
    width: int
    height: int
    size: int


@dataclass(frozen=True)
class ScanResult:
    groups: list[list[ImageRecord]]
    uniques: list[ImageRecord]
    failed: list[tuple[str, str]]
    threshold: int

    @property
    def total(self) -> int:
        return sum(len(g) for g in self.groups) + len(self.uniques)

    @property
    def duplicated_count(self) -> int:
        return sum(len(g) for g in self.groups)

    @property
    def wasted_bytes(self) -> int:
        return sum((len(g) - 1) * max(r.size for r in g) for g in self.groups)


def _find_images(root: str) -> list[str]:
    paths: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                paths.append(os.path.join(dirpath, name))
    return paths


def _hash_one(path: str) -> ImageRecord:
    from PIL import Image, ImageOps

    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        width, height = image.size
    size = os.path.getsize(path)
    phash = perceptual_hash(path)
    dhash = difference_hash(path)
    return ImageRecord(path, phash, dhash, width, height, size)


@dataclass(frozen=True)
class _HashCacheEntry:
    """Persisted hash of one image so unchanged files are not re-hashed."""

    size: int
    mtime_ns: int
    phash: int
    dhash: int
    width: int
    height: int

    def to_json(self) -> dict:
        return {
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "phash": self.phash,
            "dhash": self.dhash,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_json(cls, data: dict) -> "_HashCacheEntry":
        return cls(
            size=int(data["size"]),
            mtime_ns=int(data["mtime_ns"]),
            phash=int(data["phash"]),
            dhash=int(data["dhash"]),
            width=int(data["width"]),
            height=int(data["height"]),
        )


def index_cache_path(root: str, cache_dir: str) -> str:
    """Stable per-folder cache file name under ``cache_dir``."""
    normalized = os.path.normcase(os.path.abspath(root))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return os.path.join(cache_dir, f"index-{digest}.json")


def load_index_cache(cache_dir: str, root: str) -> dict[str, _HashCacheEntry]:
    """Read the persisted hash index for ``root`` (empty dict if absent)."""
    path = index_cache_path(root, cache_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    entries: dict[str, _HashCacheEntry] = {}
    for rel, data in raw.items():
        try:
            entries[rel] = _HashCacheEntry.from_json(data)
        except (KeyError, TypeError, ValueError):
            continue
    return entries


def save_index_cache(
    cache_dir: str, root: str, entries: dict[str, _HashCacheEntry]
) -> None:
    """Persist ``entries`` keyed by folder-relative path."""
    if not entries:
        return
    os.makedirs(cache_dir, exist_ok=True)
    path = index_cache_path(root, cache_dir)
    payload = json.dumps(
        {rel: e.to_json() for rel, e in entries.items()},
        indent=1,
        sort_keys=True,
    )
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def scan_folder(
    root: str,
    threshold: int = DEFAULT_THRESHOLD,
    workers: int | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    cache_dir: str | None = None,
) -> ScanResult:
    """Hash every image under ``root`` and return grouped duplicates.

    When ``cache_dir`` is given, the persisted per-folder index is consulted
    first: images whose (size, mtime_ns) still match their cached entry are
    reused without re-opening/re-hashing, and the refreshed index is written
    back so repeated scans of an unchanged folder avoid re-hashing everything.
    """
    if not os.path.isdir(root):
        raise NotADirectoryError(f"Not a directory: {root}")

    paths = _find_images(root)
    total = len(paths)
    if on_progress:
        on_progress(0, total, "")

    cached = load_index_cache(cache_dir, root) if cache_dir else {}

    records: list[ImageRecord] = []
    failed: list[tuple[str, str]] = []
    to_hash: list[str] = []
    stat_cache: dict[str, tuple[int, int]] = {}  # path -> (size, mtime_ns)

    for path in paths:
        rel = os.path.relpath(path, root)
        try:
            st = os.stat(path)
        except OSError:
            to_hash.append(path)
            continue
        stat_cache[path] = (st.st_size, st.st_mtime_ns)
        entry = cached.get(rel)
        if (
            entry is not None
            and entry.size == st.st_size
            and entry.mtime_ns == st.st_mtime_ns
        ):
            records.append(
                ImageRecord(
                    path,
                    entry.phash,
                    entry.dhash,
                    entry.width,
                    entry.height,
                    entry.size,
                )
            )
        else:
            to_hash.append(path)

    if to_hash:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_hash_one, p): p for p in to_hash}
            done = 0
            for future in as_completed(futures):
                done += 1
                path = futures[future]
                try:
                    records.append(future.result())
                except Exception as exc:  # noqa: BLE001 - report and continue
                    failed.append((path, str(exc)))
                if on_progress and done % 8 == 0:
                    on_progress(done, total, path)
    if on_progress:
        on_progress(total, total, "")

    if cache_dir:
        updated: dict[str, _HashCacheEntry] = {}
        for record in records:
            stat = stat_cache.get(record.path)
            if stat is None:
                try:
                    st = os.stat(record.path)
                except OSError:
                    continue
                stat = (st.st_size, st.st_mtime_ns)
            updated[os.path.relpath(record.path, root)] = _HashCacheEntry(
                stat[0],
                stat[1],
                record.phash,
                record.dhash,
                record.width,
                record.height,
            )
        save_index_cache(cache_dir, root, updated)

    groups, uniques = _group_records(records, threshold)
    groups.sort(key=lambda g: (-len(g), g[0].path.lower()))
    uniques.sort(key=lambda r: (r.phash, r.path.lower()))
    for group in groups:
        group.sort(key=lambda r: (r.phash, r.path.lower()))

    return ScanResult(groups, uniques, failed, threshold)


def _group_records(
    records: list[ImageRecord], threshold: int
) -> tuple[list[list[ImageRecord]], list[ImageRecord]]:
    buckets: dict[int, list[ImageRecord]] = defaultdict(list)
    for record in records:
        buckets.setdefault(record.phash, []).append(record)

    hash_list = sorted(buckets)
    parent = {h: h for h in hash_list}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    tree = _BKTree(hash_list)
    for node_hash in hash_list:
        for neighbor in tree.query(node_hash, threshold):
            union(node_hash, neighbor)

    merged: dict[int, list[ImageRecord]] = defaultdict(list)
    for h in hash_list:
        merged[find(h)].extend(buckets[h])

    groups: list[list[ImageRecord]] = [
        chunk for chunk in merged.values() if len(chunk) >= 2
    ]
    uniques: list[ImageRecord] = [
        chunk[0] for chunk in merged.values() if len(chunk) == 1
    ]
    return groups, uniques


class _BKTree:
    """Ball tree over 64-bit hashes supporting Hamming range queries."""

    __slots__ = ("hash", "children")

    def __init__(self, source: list[int]):
        self.hash = source[0]
        self.children: dict[int, _BKTree] = {}
        for h in source[1:]:
            self._insert(h)

    def _insert(self, h: int) -> None:
        node = self
        while True:
            d = hamming(node.hash, h)
            if d == 0:
                return
            child = node.children.get(d)
            if child is None:
                node.children[d] = _BKTree([h])
                return
            node = child

    def query(self, h: int, radius: int) -> list[int]:
        matches: list[int] = []

        def visit(node: _BKTree) -> None:
            d = hamming(node.hash, h)
            if 0 < d <= radius:
                matches.append(node.hash)
            lower, upper = d - radius, d + radius
            for edge, child in node.children.items():
                if lower <= edge <= upper:
                    visit(child)

        visit(self)
        return matches