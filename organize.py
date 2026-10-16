#!/usr/bin/env python3
"""Organize files from one or more source folders into a categorized
destination folder, setting duplicates aside instead of overwriting
anything.

Usage:
    python organize.py --dest D:/Organized C:/Users/me/Downloads E:/PhoneBackup

    Add --dry-run first to preview what would happen without copying anything.
    Add --group-by size|camera|none to change how files are grouped
    within each category (default: year, which nests a month subfolder
    inside each year, e.g. Photos/2024/05-May/).
    Add --move to move files instead of copying them, and --no-details to
    skip reading photo EXIF data (much faster on huge photo libraries).

Work is done in chunks so very large libraries stay responsive, and the
whole thing can be cancelled part-way; organizing again afterwards skips
files that already made it to the destination.
"""

import argparse
import calendar
import csv
import hashlib
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image
    from PIL.ExifTags import TAGS
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

try:
    import cv2
    HAS_CV2 = True
    try:
        cv2.setLogLevel(0)
    except Exception:
        pass
except ImportError:
    HAS_CV2 = False

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
HAS_VIDEO = HAS_CV2 or bool(FFMPEG and FFPROBE)

EXTENSION_CATEGORIES = {
    "Photos": {
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic", ".heif",
        ".webp", ".tiff", ".tif", ".raw", ".cr2", ".nef", ".dng", ".svg",
    },
    "Videos": {
        ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
        ".m4v", ".3gp", ".mpg", ".mpeg",
    },
    "Audio": {
        ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma",
    },
    "Documents/PDFs": {".pdf"},
    "Documents/Others": {
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".txt", ".csv", ".odt", ".rtf", ".md",
    },
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
}
EXT_TO_CATEGORY = {
    ext: category for category, exts in EXTENSION_CATEGORIES.items() for ext in exts
}

GROUP_CRITERIA = ("year", "size", "camera", "none")

DUPLICATES_FOLDER = "_Duplicates"
LOG_FILENAME = "organize_log.csv"

LARGE_FILE = 1024 * 1024
HASH_CHUNK_SIZE = 4 * 1024 * 1024
QUICK_HASH_SAMPLE = 64 * 1024
READ_WORKERS = 16
FULL_HASH_WORKERS = 6
DECODE_WORKERS = 8
COPY_WORKERS = 8
BIG_FILE_COPY_WORKERS = 2
BIG_FILE_AVERAGE = 8 * 1024 * 1024
COPY_CHUNK = 200
VIDEO_FRAME_POSITIONS = (0.1, 0.3, 0.5, 0.7, 0.9)
VIDEO_FRAME_DISTANCE = 12
VIDEO_MIN_MATCHING_FRAMES = 3
DETAIL_PROBE = 100
SLOW_READ_SECONDS = 0.0008
DRY_RUN_LOG_LIMIT = 200

_CANCELLED = object()


def _cancelled(cancel):
    return bool(cancel and cancel())


def _report(progress, stage, done, total):
    if progress:
        progress(stage, done, total)


# ---------- basic helpers ----------

def categorize(path: Path) -> str:
    return EXT_TO_CATEGORY.get(path.suffix.lower(), "Others")


def file_hash(path, size=None) -> str:
    """SHA-256 of a whole file. Small files are read in one go; large ones
    stream through a big reusable buffer.
    """
    if size is None:
        size = os.path.getsize(path)
    if size <= LARGE_FILE:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    hasher = hashlib.sha256()
    buffer = bytearray(HASH_CHUNK_SIZE)
    view = memoryview(buffer)
    with open(path, "rb", buffering=0) as f:
        while True:
            n = f.readinto(buffer)
            if not n:
                break
            hasher.update(view[:n])
    return hasher.hexdigest()


def quick_hash(path, size: int) -> str:
    """Cheap fingerprint from the size plus 64 KB from the start, middle and
    end. Files that differ here are certainly different, so only files that
    match still need a full read to confirm they are identical.
    """
    if size <= QUICK_HASH_SAMPLE * 3:
        return file_hash(path, size)
    hasher = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        for offset in (0, size // 2 - QUICK_HASH_SAMPLE // 2, size - QUICK_HASH_SAMPLE):
            f.seek(offset)
            hasher.update(f.read(QUICK_HASH_SAMPLE))
    return hasher.hexdigest()


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def unique_destination(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# ---------- file properties ----------

def _new_meta(path, root, folder, name, size, mtime):
    try:
        modified = datetime.fromtimestamp(mtime)
    except (OverflowError, OSError, ValueError):
        modified = datetime(1970, 1, 1)
    ext = os.path.splitext(name)[1].lower()
    return {
        "path": path,
        "name": name,
        "source_folder": folder,
        "root": root,
        "category": EXT_TO_CATEGORY.get(ext, "Others"),
        "ext": ext,
        "size_bytes": size,
        "mtime": mtime,
        "modified": modified,
        "taken": None,
        "year": modified.year,
        "month": modified.month,
        "width": None,
        "height": None,
        "camera": None,
        "duration": None,
        "duration_seconds": None,
    }


_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run_tool(args, timeout=30):
    import subprocess
    return subprocess.run(
        args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW,
    ).stdout


def video_info(path):
    """(duration_seconds, width, height) of a video, or None if unreadable.
    Uses OpenCV when installed, otherwise ffprobe.
    """
    if HAS_CV2:
        cap = cv2.VideoCapture(str(path))
        try:
            if not cap.isOpened():
                return None
            fps = cap.get(cv2.CAP_PROP_FPS)
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        finally:
            cap.release()
        if fps > 0 and frames > 0:
            return frames / fps, width, height
        return None
    if FFPROBE:
        try:
            out = _run_tool([
                FFPROBE, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path),
            ])
            import json
            data = json.loads(out or b"{}")
            stream = data["streams"][0]
            return float(data["format"]["duration"]), int(stream["width"]), int(stream["height"])
        except Exception:
            return None
    return None


def _dhash_pixels(pixels, hash_size=8):
    bits = 0
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def video_signature(path):
    """(duration, [frame hashes]) fingerprint of a video: a perceptual hash of
    frames at 10%, 30%, 50%, 70% and 90% of its length. Re-encoded, resized or
    re-compressed copies of one video share a similar signature. Returns None
    if the video can't be read.
    """
    info = video_info(path)
    if info is None:
        return None
    duration = info[0]
    hashes = []
    if HAS_CV2:
        cap = cv2.VideoCapture(str(path))
        try:
            for fraction in VIDEO_FRAME_POSITIONS:
                cap.set(cv2.CAP_PROP_POS_MSEC, duration * fraction * 1000)
                ok, frame = cap.read()
                if not ok:
                    hashes.append(None)
                    continue
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                hashes.append(_dhash_pixels(cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA).tobytes()))
        finally:
            cap.release()
    elif FFMPEG:
        for fraction in VIDEO_FRAME_POSITIONS:
            try:
                raw = _run_tool([
                    FFMPEG, "-v", "error", "-ss", f"{duration * fraction:.2f}", "-i", str(path),
                    "-frames:v", "1", "-vf", "scale=9:8:flags=area,format=gray", "-f", "rawvideo", "-",
                ], timeout=60)
            except Exception:
                raw = b""
            hashes.append(_dhash_pixels(raw) if len(raw) == 72 else None)
    if sum(h is not None for h in hashes) < VIDEO_MIN_MATCHING_FRAMES:
        return None
    return duration, hashes


def _videos_match(a, b):
    """True if two video signatures look like the same video: near-equal
    duration and most sampled frames nearly identical.
    """
    (dur_a, hashes_a), (dur_b, hashes_b) = a, b
    if abs(dur_a - dur_b) > max(1.0, 0.03 * max(dur_a, dur_b)):
        return False
    close = sum(
        1 for x, y in zip(hashes_a, hashes_b)
        if x is not None and y is not None and bin(x ^ y).count("1") <= VIDEO_FRAME_DISTANCE
    )
    return close >= VIDEO_MIN_MATCHING_FRAMES


def add_details(meta: dict) -> None:
    """Fill in photo dimensions/date taken/camera, audio duration and video
    duration/size, when the optional libraries are available. Reads part of
    the file, so it is the slowest part of scanning.
    """
    category, path = meta["category"], meta["path"]

    if category == "Videos" and HAS_VIDEO:
        info = video_info(path)
        if info:
            meta["duration_seconds"] = info[0]
            meta["duration"] = format_duration(info[0])
            meta["width"], meta["height"] = info[1], info[2]
        return

    if category == "Photos" and HAS_PIL:
        try:
            with Image.open(path) as img:
                meta["width"], meta["height"] = img.size
                exif = img._getexif() if hasattr(img, "_getexif") else None
                if exif:
                    tags = {TAGS.get(tag_id, tag_id): value for tag_id, value in exif.items()}
                    taken_str = tags.get("DateTimeOriginal")
                    if taken_str:
                        try:
                            taken = datetime.strptime(taken_str, "%Y:%m:%d %H:%M:%S")
                            meta["taken"] = taken
                            meta["year"] = taken.year
                            meta["month"] = taken.month
                        except ValueError:
                            pass
                    camera = f"{tags.get('Make', '')} {tags.get('Model', '')}".strip()
                    if camera:
                        meta["camera"] = camera
        except Exception:
            pass

    elif category == "Audio" and HAS_MUTAGEN:
        try:
            audio = MutagenFile(path)
            if audio is not None and audio.info is not None:
                length = getattr(audio.info, "length", None)
                if length:
                    meta["duration"] = format_duration(length)
                    meta["duration_seconds"] = length
        except Exception:
            pass


def _needs_details(meta):
    category = meta["category"]
    return (
        (category == "Photos" and HAS_PIL)
        or (category == "Audio" and HAS_MUTAGEN)
        or (category == "Videos" and HAS_VIDEO)
    )


def get_metadata(path, details=True) -> dict:
    path = Path(path)
    st = path.stat()
    meta = _new_meta(path, path.parent, str(path.parent), path.name, st.st_size, st.st_mtime)
    if details:
        add_details(meta)
    return meta


def group_folder(criterion: str, meta: dict):
    """Return the subfolder name to use inside a file's category, or None
    for no subfolder, based on the chosen grouping criterion.
    """
    if criterion == "size":
        mb = meta["size_bytes"] / (1024 * 1024)
        if mb < 1:
            return "Small (under 1MB)"
        if mb < 10:
            return "Medium (1-10MB)"
        if mb < 100:
            return "Large (10-100MB)"
        return "Very Large (100MB+)"
    if criterion == "camera":
        return meta["camera"] or "Unknown Camera"
    if criterion == "none":
        return None
    month = meta.get("month")
    if month:
        return f"{meta['year']}/{month:02d}-{calendar.month_name[month]}"
    return str(meta["year"])


# ---------- scanning ----------

def walk_files(sources, log=print, cancel=None):
    """Yield (source_root, folder, DirEntry) for every file, using os.scandir
    (the fastest way to list a folder tree). Folders named _Duplicates are
    skipped so files already set aside are not scanned again.
    """
    for source in sources:
        root = Path(source)
        if not root.exists():
            log(f"  ! Source not found, skipping: {root}")
            continue
        stack = [str(root)]
        while stack:
            if _cancelled(cancel):
                return
            folder = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name != DUPLICATES_FOLDER:
                                    stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                yield root, folder, entry
                        except OSError:
                            continue
            except OSError as e:
                log(f"  ! Could not open folder {folder}: {e}")


def _read_details(targets, cancel, progress):
    """Read photo/audio details for many files, picking the fastest strategy.

    Cached files read in ~0.1 ms, where threads only add overhead, so they are
    read one by one. Files being read for the first time (antivirus scanning,
    external or network drives) cost milliseconds of waiting each, which
    threads overlap. The first files are timed to decide which case applies.
    """
    total = len(targets)
    if not total:
        return
    probe = min(DETAIL_PROBE, total)
    started = time.perf_counter()
    for meta in targets[:probe]:
        add_details(meta)
    per_file = (time.perf_counter() - started) / probe
    _report(progress, "Reading file details", probe, total)

    rest = targets[probe:]
    if per_file > SLOW_READ_SECONDS:
        shifted = (lambda stage, done, _total: _report(progress, stage, probe + done, total)) if progress else None
        for _ in _run_parallel(add_details, rest, cancel, shifted, "Reading file details", chunk=256):
            pass
        return

    for i, meta in enumerate(rest, probe + 1):
        add_details(meta)
        if i % 200 == 0:
            if _cancelled(cancel):
                return
            _report(progress, "Reading file details", i, total)
    _report(progress, "Reading file details", total, total)


def scan_files(sources, log=print, cancel=None, details=True, progress=None):
    """List every file under the source folders and collect its properties.

    Listing is fast (names, sizes and dates come straight from the directory
    listing). Reading photo/audio details is optional and done in chunks with
    progress reporting. If `cancel()` turns true, the files found so far are
    returned.
    """
    results = []
    for root, folder, entry in walk_files(sources, log=log, cancel=cancel):
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError as e:
            log(f"  ! Could not read {entry.path}: {e}")
            continue
        results.append(_new_meta(Path(entry.path), root, folder, entry.name, st.st_size, st.st_mtime))
        if len(results) % 2000 == 0:
            _report(progress, "Listing files", len(results), None)
    _report(progress, "Listing files", len(results), None)

    if details and not _cancelled(cancel):
        _read_details([m for m in results if _needs_details(m)], cancel, progress)
    return results


# ---------- duplicate detection ----------

def dhash(path: Path, hash_size: int = 8):
    """A tiny perceptual hash: compares neighbouring pixels of a shrunken
    grayscale copy, so resized or re-compressed versions of one photo end up
    with nearly the same hash. Returns None if the image can't be read.
    """
    if not HAS_PIL:
        return None
    try:
        with Image.open(path) as img:
            img.draft("L", (256, 256))
            img.thumbnail((256, 256))
            img = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
            pixels = img.tobytes()
    except Exception:
        return None
    return _dhash_pixels(pixels, hash_size)


def _keeper_key(strategy):
    def when(m):
        return m["taken"] or m["modified"]

    def pixels(m):
        return (m["width"] or 0) * (m["height"] or 0)

    if strategy == "largest":
        return lambda m: (-pixels(m), -m["size_bytes"], when(m))
    return lambda m: (when(m),)


def _choose_keeper(members, strategy):
    if strategy == "first":
        return members[0]
    return min(members, key=_keeper_key(strategy))


def _mark_group(members, strategy, kind, group_id, info):
    keeper = _choose_keeper(members, strategy)
    for m in members:
        info[str(m["path"])] = {
            "group": group_id,
            "keep": m is keeper,
            "kind": kind,
            "keeper": keeper["name"],
            "copies": len(members),
        }


def _run_parallel(func, items, cancel=None, progress=None, stage="", chunk=128, workers=None):
    """Run func over items on a thread pool, chunk by chunk, yielding
    (item, result-or-exception) in order. Stops early if cancelled. Use for
    I/O-bound work only; CPU-bound Python work gets slower with threads.
    """
    def safe(item):
        if _cancelled(cancel):
            return _CANCELLED
        try:
            return func(item)
        except Exception as e:
            return e

    total, done = len(items), 0
    with ThreadPoolExecutor(workers or READ_WORKERS) as pool:
        for start in range(0, total, chunk):
            if _cancelled(cancel):
                return
            batch = items[start:start + chunk]
            for item, outcome in zip(batch, pool.map(safe, batch)):
                if outcome is not _CANCELLED:
                    yield item, outcome
            done += len(batch)
            _report(progress, stage, done, total)


def _similar_pairs(hashed, distance):
    """Neighbour lists of photos whose hashes differ by <= distance bits.

    A 64-bit hash is cut into distance+1 slices; two hashes within `distance`
    bits must agree exactly on at least one slice, so only hashes sharing a
    slice value need comparing (instead of every pair).
    """
    slices = distance + 1
    bounds = [round(i * 64 / slices) for i in range(slices + 1)]
    buckets = {}
    for idx, (_, h) in enumerate(hashed):
        for k in range(slices):
            lo, hi = bounds[k], bounds[k + 1]
            buckets.setdefault((k, (h >> lo) & ((1 << (hi - lo)) - 1)), []).append(idx)

    neighbours = [set() for _ in hashed]
    for members in buckets.values():
        if len(members) < 2:
            continue
        for a in range(len(members)):
            i = members[a]
            for b in range(a + 1, len(members)):
                j = members[b]
                if j in neighbours[i]:
                    continue
                if bin(hashed[i][1] ^ hashed[j][1]).count("1") <= distance:
                    neighbours[i].add(j)
                    neighbours[j].add(i)
    return neighbours


def _similar_videos(signatures):
    """Neighbour lists for videos (signatures sorted by duration): only videos
    whose durations are close are compared.
    """
    neighbours = [set() for _ in signatures]
    for i, sig_i in enumerate(signatures):
        limit = sig_i[0] + max(1.0, 0.03 * sig_i[0]) * 1.04
        for j in range(i + 1, len(signatures)):
            if signatures[j][0] > limit:
                break
            if _videos_match(sig_i, signatures[j]):
                neighbours[i].add(j)
                neighbours[j].add(i)
    return neighbours


def _group_similar(items, neighbours, keep, group_id, info):
    """Turn neighbour lists into groups (each item joins at most one group)
    and mark them in info. Returns the updated group id counter.
    """
    assigned = set()
    for i, seed in enumerate(items):
        if i in assigned:
            continue
        members = [seed]
        for j in sorted(neighbours[i]):
            if j not in assigned and j > i:
                members.append(items[j])
                assigned.add(j)
        if len(members) > 1:
            assigned.add(i)
            group_id += 1
            _mark_group(members, keep, "similar", group_id, info)
    return group_id


def find_duplicates(metas, keep="oldest", similar=False, similar_distance=5,
                    log=print, cancel=None, progress=None):
    """Find files that are duplicates of each other.

    Exact duplicates have identical content (any name, any date). With
    similar=True, photos that merely look alike (resized, re-compressed) are
    grouped too. In each group one file is kept, chosen by `keep`:
    'oldest', 'largest' or 'first' (first scanned).

    Only files sharing a size are read at all, and those are first compared
    by a cheap sampled fingerprint, so full reads happen only for likely
    duplicates. Full hashes are cached on each meta as meta["sha256"].

    Returns {str(path): {"group", "keep", "kind", "keeper", "copies"}} for
    every file that belongs to a group; unique files are absent.
    """
    info = {}
    group_id = 0

    by_size = {}
    for m in metas:
        if m["size_bytes"] > 0:
            by_size.setdefault(m["size_bytes"], []).append(m)
    same_size = [m for group in by_size.values() if len(group) > 1 for m in group]

    by_quick = {}
    for m, outcome in _run_parallel(
        lambda m: quick_hash(m["path"], m["size_bytes"]), same_size,
        cancel, progress, "Comparing file samples",
    ):
        if isinstance(outcome, Exception):
            log(f"  ! Could not read {m['path']}: {outcome}")
        else:
            by_quick.setdefault((m["size_bytes"], outcome), []).append(m)

    likely = [m for group in by_quick.values() if len(group) > 1 for m in group]
    by_hash = {}
    for m, outcome in _run_parallel(
        lambda m: file_hash(m["path"], m["size_bytes"]), likely,
        cancel, progress, "Confirming duplicates", chunk=32, workers=FULL_HASH_WORKERS,
    ):
        if isinstance(outcome, Exception):
            log(f"  ! Could not read {m['path']}: {outcome}")
        else:
            m["sha256"] = outcome
            by_hash.setdefault(outcome, []).append(m)

    for members in by_hash.values():
        if len(members) > 1:
            group_id += 1
            _mark_group(members, keep, "exact", group_id, info)

    if similar and not _cancelled(cancel):
        def not_exact_extra(m):
            return info.get(str(m["path"]), {}).get("keep", True)

        if HAS_PIL:
            candidates = [m for m in metas if m["category"] == "Photos" and not_exact_extra(m)]
            hashed = []
            for m, outcome in _run_parallel(
                lambda m: dhash(m["path"]), candidates, cancel, progress, "Comparing photos",
                chunk=64, workers=DECODE_WORKERS,
            ):
                if outcome is not None and not isinstance(outcome, Exception):
                    hashed.append((m, outcome))
            if not _cancelled(cancel):
                neighbours = _similar_pairs(hashed, similar_distance)
                group_id = _group_similar([m for m, _ in hashed], neighbours, keep, group_id, info)

        if HAS_VIDEO and not _cancelled(cancel):
            candidates = [m for m in metas if m["category"] == "Videos" and not_exact_extra(m)]
            signed = []
            for m, outcome in _run_parallel(
                lambda m: video_signature(m["path"]), candidates, cancel, progress,
                "Comparing videos", chunk=16, workers=DECODE_WORKERS,
            ):
                if outcome is not None and not isinstance(outcome, Exception):
                    signed.append((m, outcome))
            if not _cancelled(cancel):
                signed.sort(key=lambda item: item[1][0])
                neighbours = _similar_videos([sig for _, sig in signed])
                group_id = _group_similar([m for m, _ in signed], neighbours, keep, group_id, info)

    return info


def separate_duplicates(items, dry_run=False, log=print, cancel=None, progress=None):
    """Move duplicate files into a _Duplicates folder inside their own source
    folder (no copying, nothing is deleted). `items` is a list of
    (file_path, source_root). Returns (moved_paths, error_count).
    """
    moved, errors = [], 0
    total = len(items)
    for i, (path, root) in enumerate(items, 1):
        if _cancelled(cancel):
            log("  Stopped.")
            break
        path, root = Path(path), Path(root)
        target_dir = root / DUPLICATES_FOLDER
        dest = unique_destination(target_dir / path.name)
        if dry_run:
            if i <= DRY_RUN_LOG_LIMIT:
                log(f"  [dry-run] {path} -> {dest}")
            continue
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            moved.append(path)
        except OSError as e:
            log(f"  ! Could not move {path}: {e}")
            errors += 1
        if i % 100 == 0:
            _report(progress, "Separating duplicates", i, total)
    _report(progress, "Separating duplicates", total, total)
    return moved, errors


# ---------- organizing ----------

def _same_file(dest: Path, meta: dict) -> bool:
    """True if dest looks like the copy of meta made by an earlier run."""
    try:
        st = dest.stat()
    except OSError:
        return False
    return st.st_size == meta["size_bytes"] and abs(st.st_mtime - meta["mtime"]) <= 2


def _plan_destination(target_dir: Path, name: str, meta: dict, claimed: set):
    """Pick the destination path for a file. Returns (path, already_there).
    A name clash with a different file gets a ' (1)', ' (2)'... suffix; a clash
    with an earlier copy of the same file means it is already organized.
    """
    stem, suffix = os.path.splitext(name)
    n = 0
    while True:
        candidate = target_dir / (name if n == 0 else f"{stem} ({n}){suffix}")
        key = os.path.normcase(str(candidate))
        if key not in claimed:
            claimed.add(key)
            if not candidate.exists():
                return candidate, False
            if _same_file(candidate, meta):
                return candidate, True
        n += 1


def _write_log(dest_root, dry_run, log_rows, log):
    if dry_run or not log_rows:
        return
    log_path = Path(dest_root) / LOG_FILENAME
    is_new = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["source", "destination", "status", "sha256", "error"])
        writer.writerows(log_rows)
    log(f"\nLog written to: {log_path}")


def organize_files(paths, dest_root, dry_run: bool, group_by: str = "year", log=print,
                   move=False, metas=None, dedupe=True, known_duplicates=None, cancel=None, progress=None):
    """Organize an explicit list of files (e.g. a hand-picked selection from
    a preview table).

    metas:   optional {str(path): metadata} from an earlier scan, so files are
             not re-read.
    dedupe:  look for exact duplicates first and set them aside in
             _Duplicates. Pass False to skip duplicate checking entirely.
    known_duplicates: set of str(path) already identified as duplicates (for
             example by a scan); they are set aside in _Duplicates without
             re-reading any file. Overrides dedupe.
    cancel:  callable returning True to stop. Files already copied stay put;
             running again skips them, so a stopped run can be resumed.
    progress: callable(stage, done, total) for progress reporting.

    Returns stats: organized, duplicates, skipped (already at destination),
    errors and cancelled.
    """
    dest_root = Path(dest_root)
    stats = {"organized": 0, "duplicates": 0, "skipped": 0, "errors": 0, "cancelled": False}
    log_rows = []

    if not dry_run:
        dest_root.mkdir(parents=True, exist_ok=True)

    entries = []
    for i, p in enumerate(paths, 1):
        meta = metas.get(str(p)) if metas else None
        if meta is None:
            try:
                meta = get_metadata(p)
            except OSError as e:
                log(f"  ! Could not read {p}: {e}")
                stats["errors"] += 1
                log_rows.append([str(p), "", "error", "", str(e)])
                continue
        entries.append(meta)
        if i % 500 == 0 and _cancelled(cancel):
            stats["cancelled"] = True
            break

    dup_paths = set()
    if known_duplicates is not None:
        dup_paths = set(known_duplicates)
    elif dedupe and entries and not stats["cancelled"]:
        info = find_duplicates(entries, keep="first", similar=False, log=log,
                               cancel=cancel, progress=progress)
        dup_paths = {key for key, value in info.items() if not value["keep"]}
    if _cancelled(cancel):
        stats["cancelled"] = True

    plan = []
    claimed = set()
    total = len(entries)
    for i, meta in enumerate(entries):
        if stats["cancelled"]:
            break
        if i % 500 == 0:
            if _cancelled(cancel):
                stats["cancelled"] = True
                break
            _report(progress, "Planning", i, total)
        src = meta["path"]
        if str(src) in dup_paths:
            target_dir = dest_root / DUPLICATES_FOLDER / meta["category"]
            status = "duplicate"
        else:
            subfolder = group_folder(group_by, meta)
            target_dir = dest_root / meta["category"]
            if subfolder:
                target_dir = target_dir / subfolder
            status = "organized"

        if move and not os.path.exists(src):
            stats["skipped"] += 1
            continue
        dest, already_there = _plan_destination(target_dir, meta["name"], meta, claimed)
        if already_there:
            stats["skipped"] += 1
            continue
        plan.append((src, dest, status, meta.get("sha256", ""), target_dir, meta["size_bytes"]))

    if dry_run:
        for src, dest, status, *_ in plan[:DRY_RUN_LOG_LIMIT]:
            log(f"  [dry-run] {status}: {src} -> {dest}")
        if len(plan) > DRY_RUN_LOG_LIMIT:
            log(f"  ... and {len(plan) - DRY_RUN_LOG_LIMIT} more")
        for job in plan:
            stats["duplicates" if job[2] == "duplicate" else "organized"] += 1
        return stats

    for target_dir in {job[4] for job in plan}:
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as e:
            log(f"  ! Could not create folder {target_dir}: {e}")

    verb = "Moving" if move else "Copying"

    def transfer(job):
        if _cancelled(cancel):
            return _CANCELLED
        try:
            if move:
                shutil.move(str(job[0]), str(job[1]))
            else:
                shutil.copy2(job[0], job[1])
        except OSError as e:
            return e
        return None

    average_size = sum(job[5] for job in plan) / len(plan) if plan else 0
    workers = BIG_FILE_COPY_WORKERS if average_size > BIG_FILE_AVERAGE else COPY_WORKERS

    done = 0
    with ThreadPoolExecutor(workers) as pool:
        for start in range(0, len(plan), COPY_CHUNK):
            if _cancelled(cancel):
                stats["cancelled"] = True
                break
            batch = plan[start:start + COPY_CHUNK]
            for job, outcome in zip(batch, pool.map(transfer, batch)):
                src, dest, status, digest = job[:4]
                if outcome is _CANCELLED:
                    stats["cancelled"] = True
                elif outcome is None:
                    stats["duplicates" if status == "duplicate" else "organized"] += 1
                    log_rows.append([str(src), str(dest), status, digest, ""])
                else:
                    log(f"  ! Could not {'move' if move else 'copy'} {src}: {outcome}")
                    stats["errors"] += 1
                    log_rows.append([str(src), "", "error", digest, str(outcome)])
            done += len(batch)
            _report(progress, verb, done, len(plan))

    _write_log(dest_root, dry_run, log_rows, log)
    return stats


def organize(sources, dest_root, dry_run: bool, group_by: str = "year", log=print, move=False,
             details=True, cancel=None, progress=None):
    """Scan the given source folders and organize every file found in them."""
    metas = scan_files(sources, log=log, cancel=cancel, details=details, progress=progress)
    if _cancelled(cancel):
        return {"organized": 0, "duplicates": 0, "skipped": 0, "errors": 0, "cancelled": True}
    return organize_files(
        [m["path"] for m in metas], dest_root, dry_run, group_by=group_by, log=log, move=move,
        metas={str(m["path"]): m for m in metas}, cancel=cancel, progress=progress,
    )


def _cli_progress():
    last = [0.0]

    def show(stage, done, total):
        now = time.monotonic()
        if now - last[0] < 0.25 and not (total and done >= total):
            return
        last[0] = now
        text = f"{stage}: {done:,}" + (f" / {total:,}" if total else "")
        print("\r" + text.ljust(60), end="", flush=True, file=sys.stderr)

    return show


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sources", nargs="+", help="One or more source folders to scan")
    parser.add_argument("--dest", required=True, help="Destination folder for organized files")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without changing any files")
    parser.add_argument(
        "--group-by", choices=GROUP_CRITERIA, default="year",
        help="How to group files within each category (default: year)",
    )
    parser.add_argument("--move", action="store_true", help="Move files instead of copying them")
    parser.add_argument("--no-details", action="store_true", help="Skip reading photo EXIF data (faster)")
    args = parser.parse_args()

    if not HAS_PIL and not args.no_details:
        print("Note: Pillow not installed, so photo dates/dimensions/camera are unavailable.")
        print("Install with 'pip install Pillow' for full photo metadata.\n")

    print(f"Scanning: {', '.join(args.sources)}")
    print(f"Destination: {args.dest}")
    print(f"Group by: {args.group_by}")
    print(f"Mode: {'DRY RUN (no files will be changed)' if args.dry_run else ('MOVE' if args.move else 'COPY')}\n")

    try:
        stats = organize(
            args.sources, Path(args.dest), args.dry_run, group_by=args.group_by, move=args.move,
            details=not args.no_details, progress=_cli_progress(),
        )
    except KeyboardInterrupt:
        print("\nStopped. Run the same command again to continue; finished files are skipped.")
        return 1

    print("\n\nDone." if not stats["cancelled"] else "\n\nStopped.")
    print(f"  Organized: {stats['organized']}")
    print(f"  Duplicates set aside: {stats['duplicates']}")
    print(f"  Skipped (already there): {stats['skipped']}")
    print(f"  Errors: {stats['errors']}")


if __name__ == "__main__":
    sys.exit(main())
