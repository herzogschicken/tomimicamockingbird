"""Import books straight from the phone over adb.

Scan -> pick an audiobook (and an EPUB on the phone or the PC) -> the audio is copied to the PC,
the book is added, transcription starts, and (by default) the PC copy of the audio is deleted
once transcribed. If you re-transcribe later, the audio is copied from the phone again.
"""
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from rapidfuzz import fuzz

from . import adbsync, settings
from .config import BOOKS_DIR, TMP_DIR

STOP = re.compile(r"\b(unabridged|abridged|audiobook|audible|m4b|epub|retail|v\d+)\b", re.I)
# Text in brackets is usually an edition or source tag, not part of the title.
BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")


def _norm_name(path):
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    folder = path.rsplit("/", 2)[-2] if path.count("/") >= 2 else ""
    text = BRACKETS.sub(" ", f"{folder} {stem}")
    text = STOP.sub(" ", text.replace("_", " ").replace(".", " "))
    text = re.sub(r"[\(\)\[\]\{\}\-,:]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def suggest_pairs(files):
    """For each audiobook, rank the phone EPUBs by how alike their names (and folders) are."""
    epubs = [f for f in files if f["kind"] == "epub"]
    out = []
    for a in (f for f in files if f["kind"] == "audio"):
        an = _norm_name(a["path"])
        ranked = sorted(
            ({"path": e["path"], "name": e["name"], "score": round(fuzz.token_set_ratio(an, _norm_name(e["path"])))}
             for e in epubs), key=lambda x: -x["score"])
        out.append({**a, "epub_suggestions": [r for r in ranked if r["score"] >= 40][:5]})
    return out


def serial():
    return settings.get()["adb_serial"] or None


def folders():
    return [f.strip() for f in settings.get()["phone_folders"].split(",") if f.strip()]


class Importer:
    def __init__(self, ingest, enqueue, set_book_fields):
        self.ingest, self.enqueue, self.set_book_fields = ingest, enqueue, set_book_fields
        self.lock = threading.Lock()
        self.jobs = {}

    def list(self):
        with self.lock:
            return [dict(j) for j in self.jobs.values()]

    def start(self, audio_path, audio_size, epub_phone=None, epub_size=None, epub_local=None):
        jid = uuid.uuid4().hex[:8]
        job = {"id": jid, "name": audio_path.rsplit("/", 1)[-1], "phase": "starting", "done": 0,
               "total": (audio_size or 0) + (epub_size or 0), "error": None, "book_id": None,
               "duplicate": False, "started": time.time()}
        with self.lock:
            self.jobs[jid] = job
        threading.Thread(target=self._run, args=(jid, audio_path, audio_size, epub_phone, epub_size, epub_local),
                         daemon=True).start()
        return jid

    def _set(self, jid, **kw):
        with self.lock:
            self.jobs[jid].update(kw)

    def _run(self, jid, audio_path, audio_size, epub_phone, epub_size, epub_local):
        book_id = uuid.uuid4().hex[:12]
        bdir = BOOKS_DIR / book_id
        files = bdir / "files"
        files.mkdir(parents=True)
        try:
            base = 0
            if epub_phone:
                self._set(jid, phase="copying EPUB")
                epub = files / epub_phone.rsplit("/", 1)[-1]
                ok, msg = adbsync.pull(serial(), epub_phone, epub, epub_size,
                                       lambda d, t: self._set(jid, done=d))
                if not ok:
                    raise RuntimeError(f"Could not copy the EPUB from the phone: {msg}")
                base = epub_size or epub.stat().st_size
                epub_mode = "copied"
            elif TMP_DIR in Path(epub_local).parents:
                # an EPUB uploaded from the browser: keep it with the book (tmp is wiped on restart)
                epub = files / Path(epub_local).name
                shutil.move(epub_local, epub)
                shutil.rmtree(Path(epub_local).parent, ignore_errors=True)
                epub_mode = "copied"
            else:
                epub, epub_mode = Path(epub_local), "linked"

            self._set(jid, phase="copying audio", done=base)
            audio = files / audio_path.rsplit("/", 1)[-1]
            ok, msg = adbsync.pull(serial(), audio_path, audio, audio_size,
                                   lambda d, t: self._set(jid, done=base + d))
            if not ok:
                raise RuntimeError(f"Could not copy the audio from the phone: {msg}")

            self._set(jid, phase="reading chapters and EPUB")
            res = self.ingest(book_id, bdir, epub, epub_mode, audio, "copied")
            bid = res["id"]
            phase = "already in your library" if res["duplicate"] else "added"
            if not res["duplicate"]:
                self.set_book_fields(bid, phone_path=audio_path,
                                     audio_temp=1 if settings.get()["delete_audio_after"] else 0)
                # Queue first, then announce, so the library never shows a "Transcribe" button
                # for a book that is about to start by itself.
                if settings.get()["auto_transcribe"]:
                    self.enqueue(bid)
                    phase = "added, transcribing"
            self._set(jid, book_id=bid, duplicate=res["duplicate"], phase=phase)
        except Exception as e:
            shutil.rmtree(bdir, ignore_errors=True)
            self._set(jid, phase="failed", error=str(e))
