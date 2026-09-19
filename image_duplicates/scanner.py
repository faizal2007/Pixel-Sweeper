"""Walk an image library, hash every image and group near-duplicates.

The walk is recursive and covers the whole tree at any depth, following folder
links (with cycle protection) and reporting folders it could not read.

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


class ScanCancelled(Exception):
    """Raised when ``should_cancel`` asks a running scan to stop.

    Anything already hashed is left in the index cache, so a later scan of the
    same folder picks up where this one stopped.
    """


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
    # Folders that could not be listed at all, and why. Surfaced in the UI so a
    # missing subfolder is visible instead of being silently ignored.
    skipped_dirs: tuple[tuple[str, str], ...] = ()

    @property
    def total(self) -> int:
        return sum(len(g) for g in self.groups) + len(self.uniques)

    @property
    def duplicated_count(self) -> int:
        return sum(len(g) for g in self.groups)

    @property
    def wasted_bytes(self) -> int:
        return sum((len(g) - 1) * max(r.size for r in g) for g in self.groups)


def _walk_images(root: str, skipped: list[tuple[str, str]]) -> list[str]:
    """Every image below ``root``, at any depth.

    ``os.walk`` is used with ``onerror`` so a folder that cannot be listed is
    recorded rather than dropped in silence, and with ``followlinks=True`` so
    folders reached through a symlink are scanned too - some libraries keep a
    subfolder behind one. A set of real paths keeps linked folders from being
    walked twice or looping for ever.
    """

    def on_error(error: OSError) -> None:
        skipped.append((getattr(error, "filename", None) or root, str(error)))

    paths: list[str] = []
    visited = {os.path.realpath(root)}
    for dirpath, dirnames, filenames in os.walk(
        root, onerror=on_error, followlinks=True
    ):
        # Prune in place so a link back up the tree cannot recurse for ever.
        keep: list[str] = []
        for name in dirnames:
            real = os.path.realpath(os.path.join(dirpath, name))
            if real in visited:
                continue
            visited.add(real)
            keep.append(name)
        dirnames[:] = keep

        for name in filenames:
            if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                paths.append(os.path.join(dirpath, name))
    return paths


def _display_size(path: str) -> tuple[int, int]:
    """The size the image is meant to be shown at, without decoding it.

    The dimensions come from the header, swapped when the EXIF orientation says
    the picture is stored on its side. ``exif_transpose`` would give the same
    answer but has to decode the whole image first - for a 200 MP photo that is
    ~600 MB of pixels just to learn two numbers.
    """
    from PIL import ExifTags, Image

    with Image.open(path) as image:
        width, height = image.size
        try:
            orientation = image.getexif().get(ExifTags.Base.Orientation, 1)
        except Exception:  # noqa: BLE001 - malformed EXIF is not worth failing over
            orientation = 1
    if orientation in (5, 6, 7, 8):
        # Those orientations store the picture rotated a quarter turn.
        width, height = height, width
    return width, height


def _hash_one(path: str) -> ImageRecord:
    width, height = _display_size(path)
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
    should_cancel: Callable[[], bool] | None = None,
    on_record: Callable[[ImageRecord], None] | None = None,
) -> ScanResult:
    """Hash every image under ``root`` and return grouped duplicates.

    The whole tree is scanned, at any depth, including folders reached through
    a symlink or junction. Folders that cannot be listed are collected into
    ``ScanResult.skipped_dirs`` rather than being passed over in silence.

    When ``cache_dir`` is given, the persisted per-folder index is consulted
    first: images whose (size, mtime_ns) still match their cached entry are
    reused without re-opening/re-hashing, and the refreshed index is written
    back so repeated scans of an unchanged folder avoid re-hashing everything.

    ``should_cancel`` is polled while walking and hashing. As soon as it
    returns True the scan stops, the index cache is still written with the
    hashes gathered so far, and ``ScanCancelled`` is raised.

    ``on_record`` is called from the scanning thread for every image as soon as
    it is identified - cached images first, then hashed ones as the pool
    finishes them - which lets a caller show results while the scan runs.
    """
    if not os.path.isdir(root):
        raise NotADirectoryError(f"Not a directory: {root}")

    def cancelled() -> bool:
        return should_cancel is not None and should_cancel()

    skipped: list[tuple[str, str]] = []
    paths = _walk_images(root, skipped)
    total = len(paths)
    if on_progress:
        on_progress(0, total, "")

    cached = load_index_cache(cache_dir, root) if cache_dir else {}

    records: list[ImageRecord] = []
    failed: list[tuple[str, str]] = []
    to_hash: list[str] = []
    stat_cache: dict[str, tuple[int, int]] = {}  # path -> (size, mtime_ns)

    for position, path in enumerate(paths):
        # Cheap enough to poll often: no images have been opened yet, so a
        # cancel here can stop before any real work happens.
        if position % 256 == 0 and cancelled():
            raise ScanCancelled(f"Cancelled before hashing {root}")
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
            record = ImageRecord(
                path,
                entry.phash,
                entry.dhash,
                entry.width,
                entry.height,
                entry.size,
            )
            records.append(record)
            if on_record is not None:
                on_record(record)
        else:
            to_hash.append(path)

    stopped_early = False
    if to_hash:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_hash_one, p): p for p in to_hash}
            done = 0
            for future in as_completed(futures):
                if cancelled():
                    stopped_early = True
                    # Drop whatever has not started yet; the ones already
                    # running finish, and the context manager waits for those.
                    for pending in futures:
                        pending.cancel()
                    break
                done += 1
                path = futures[future]
                try:
                    record = future.result()
                except Exception as exc:  # noqa: BLE001 - report and continue
                    failed.append((path, str(exc)))
                else:
                    records.append(record)
                    if on_record is not None:
                        on_record(record)
                if on_progress and done % 8 == 0:
                    on_progress(done, total, path)
    if on_progress and not stopped_early:
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

    if stopped_early:
        # The index above keeps the hashes that did complete, so a later scan
        # only has to do the rest.
        raise ScanCancelled(f"Cancelled while hashing {root}")

    return _build_result(records, failed, skipped, threshold)


def _build_result(
    records: list[ImageRecord],
    failed: list[tuple[str, str]],
    skipped: list[tuple[str, str]],
    threshold: int,
) -> ScanResult:
    groups, uniques = _group_records(records, threshold)
    groups.sort(key=lambda g: (-len(g), g[0].path.lower()))
    uniques.sort(key=lambda r: (r.phash, r.path.lower()))
    for group in groups:
        group.sort(key=lambda r: (r.phash, r.path.lower()))
    return ScanResult(groups, uniques, failed, threshold, tuple(skipped))


def _group_records(
    records: list[ImageRecord], threshold: int
) -> tuple[list[list[ImageRecord]], list[ImageRecord]]:
    if not records:
        # Nothing to group - an empty folder, or a scan cancelled before the
        # first hash. The BK-tree below also needs at least one hash to exist.
        return [], []
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