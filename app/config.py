import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


def _default_data_dir():
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "tomimicamockingbird"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "tomimicamockingbird"


DATA_DIR = Path(os.environ.get("TMM_DATA") or _default_data_dir())
BOOKS_DIR = DATA_DIR / "books"
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "library.db"

EPUB_EXTS = {".epub"}
AUDIO_EXTS = {".m4b", ".m4a"}

for d in (BOOKS_DIR, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)
