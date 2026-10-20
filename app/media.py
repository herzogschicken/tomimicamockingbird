import hashlib
import json
import os
import shutil
import subprocess
from functools import lru_cache


def which(name):
    return shutil.which(name)


def probe_audio(path):
    """Read duration, tags and chapter markers with ffprobe."""
    if not which("ffprobe"):
        raise RuntimeError("ffprobe not found. Install ffmpeg (see README).")
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_chapters", "-show_format", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {res.stderr.strip()[:300]}")
    data = json.loads(res.stdout or "{}")
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or 0)
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}

    chapters = []
    for i, ch in enumerate(data.get("chapters", [])):
        t = {k.lower(): v for k, v in (ch.get("tags") or {}).items()}
        chapters.append({
            "idx": i,
            "title": t.get("title") or f"Chapter {i + 1}",
            "start": float(ch["start_time"]),
            "end": float(ch["end_time"]),
        })
    if not chapters and duration:
        chapters = [{"idx": 0, "title": "Full book", "start": 0.0, "end": duration}]

    return {
        "duration": duration,
        "title": tags.get("title") or tags.get("album"),
        "author": tags.get("artist") or tags.get("album_artist"),
        "chapters": chapters,
    }


def quick_hash(path, chunk=1 << 20):
    """Fingerprint from size plus start, middle and end chunks.
    Fast on multi-GB audio files and good enough to spot the same file twice."""
    size = os.path.getsize(path)
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(chunk))
        if size > 3 * chunk:
            f.seek(size // 2)
            h.update(f.read(chunk))
            f.seek(-chunk, 2)
            h.update(f.read(chunk))
    return h.hexdigest()


@lru_cache(maxsize=1)
def aac_decoder_ok():
    """True if this ffmpeg can decode AAC (needed to read m4b audio)."""
    if not which("ffmpeg"):
        return False
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-decoders"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15).stdout
    except Exception:
        return False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1] == "aac":
            return True
    return False
