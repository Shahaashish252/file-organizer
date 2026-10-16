# File Organizer

A desktop tool for sorting photos, videos, documents and other files scattered
across a laptop, phone backups and external drives into one clean, organized
folder — while finding and separating duplicates (exact copies, renamed
copies, and resized/re-compressed copies of the same photo or video).

## Running it

Requires Python 3. Optional but recommended libraries:

```
pip install Pillow mutagen opencv-python
```

- `Pillow` — photo dimensions, EXIF date taken, camera make/model, and
  resized/re-compressed similar-photo detection.
- `mutagen` — audio duration.
- `opencv-python` — video duration, resolution, and similar-video detection
  (falls back to `ffmpeg`/`ffprobe` on PATH if not installed).

### Desktop app

```
python gui.py
```

Add one or more messy folders, pick a destination, click **Scan Files** to
preview every file's properties in a sortable/searchable table, check or
uncheck exactly which files to include, then **Organize**. Scanning and
organizing both run in chunks with a progress bar and a **Stop** button —
stopping and running again later skips files already done.

### Command line

```
python organize.py --dest D:/Organized C:/Users/me/Downloads E:/PhoneBackup
```

Add `--dry-run` to preview without changing anything, `--move` to move files
instead of copying, and `--group-by size|camera|none` to change how files are
grouped within each category (default: by year, with a month subfolder).

## How duplicates are handled

- **Exact duplicates** (identical content, any name or date) are found for
  every file type.
- **Similar photos and videos** (resized, re-compressed, or re-encoded
  copies) can optionally be matched too.
- One copy in each group is kept — oldest, largest/best quality, or first
  scanned, your choice — and the rest are set aside in a `_Duplicates`
  folder instead of being deleted.
