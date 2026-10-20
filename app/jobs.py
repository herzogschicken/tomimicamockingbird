"""Background transcription queue. One book at a time, each in its own subprocess."""
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from . import adbsync, align, db, gpu, settings
from .config import BOOKS_DIR

ROOT = Path(__file__).resolve().parent.parent


def _die_with_parent():
    """Linux only: kill the job if the server process dies, so a restart never runs two jobs at once.
    (On Windows the job is stopped by the server's shutdown instead.)"""
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def _popen_extra():
    if sys.platform.startswith("linux"):
        return {"preexec_fn": _die_with_parent}
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def set_status(book_id, status, note=None):
    with db.db() as con:
        con.execute("UPDATE books SET status=?, status_note=? WHERE id=?", (status, note, book_id))


def run_alignment(book_id):
    """Align a book. On failure, keep the transcript and say what went wrong."""
    try:
        return align.run(book_id)
    except Exception as e:
        set_status(book_id, "transcribed", f"Alignment failed: {type(e).__name__}: {e}")
        return None


class Transcriber:
    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue = deque()
        self.current = None
        self.proc = None
        self.stop_requested = False
        self.progress = {}  # book_id -> dict
        threading.Thread(target=self._loop, daemon=True, name="transcriber").start()

    # ---------- public ----------

    def enqueue(self, book_id):
        with self.lock:
            if book_id == self.current or book_id in self.queue:
                return
            self.queue.append(book_id)
            self.progress[book_id] = self._fresh_progress(book_id, phase="queued")
        set_status(book_id, "queued")
        self.wake.set()

    def pause(self, book_id, wait=False):
        with self.lock:
            if book_id in self.queue:
                self.queue.remove(book_id)
                self.progress.pop(book_id, None)
                set_status(book_id, self._idle_status(book_id))
                return True
            if book_id != self.current:
                return False
            self.stop_requested = True
            proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            if wait:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        return True

    @staticmethod
    def _public(p):
        return {k: v for k, v in p.items() if not k.startswith("_")}

    def snapshot(self, book_id):
        with self.lock:
            p = self.progress.get(book_id)
            return self._public(p) if p else None

    def all_snapshots(self):
        with self.lock:
            return {k: self._public(v) for k, v in self.progress.items()}

    def resume_interrupted(self):
        """Called at startup: requeue books that were queued or mid-transcription when the app stopped."""
        with db.db() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM books WHERE status IN ('queued','transcribing') ORDER BY created_at")]
        for i in ids:
            self.enqueue(i)
        with db.db() as con:
            stuck = [r["id"] for r in con.execute("SELECT id FROM books WHERE status='aligning'")]
        for i in stuck:  # app stopped mid-alignment: just run it again
            threading.Thread(target=run_alignment, args=(i,), daemon=True).start()
        # Fully transcribed books that were never aligned (e.g. transcribed before alignment existed).
        with db.db() as con:
            rows = con.execute(
                "SELECT b.id FROM books b WHERE b.status='transcribed' AND "
                "(SELECT COUNT(*) FROM transcripts t WHERE t.book_id=b.id) >= "
                "(SELECT COUNT(*) FROM chapters c WHERE c.book_id=b.id)").fetchall()
        for r in rows:
            if not (BOOKS_DIR / r["id"] / "alignment.json").exists():
                threading.Thread(target=run_alignment, args=(r["id"],), daemon=True).start()

    # ---------- internals ----------

    def _idle_status(self, book_id):
        with db.db() as con:
            n = con.execute("SELECT COUNT(*) FROM transcripts WHERE book_id=?", (book_id,)).fetchone()[0]
            total = con.execute("SELECT COUNT(*) FROM chapters WHERE book_id=?", (book_id,)).fetchone()[0]
        if n == 0:
            return "not_transcribed"
        return "transcribed" if n >= total else "paused"

    def _fresh_progress(self, book_id, phase):
        with db.db() as con:
            chapters = {r["idx"]: r["end"] - r["start"] for r in con.execute(
                'SELECT idx,start,"end" FROM chapters WHERE book_id=?', (book_id,))}
            done = {r["idx"] for r in con.execute("SELECT idx FROM transcripts WHERE book_id=?", (book_id,))}
        return {
            "book_id": book_id, "phase": phase, "device": None, "compute_type": None, "note": None,
            "chapter_count": len(chapters), "done_chapters": sorted(done),
            "total_audio": sum(chapters.values()),
            "done_audio": sum(d for i, d in chapters.items() if i in done),
            "current_idx": None, "current_t": 0.0, "current_len": 0.0,
            "run_audio": 0.0, "run_started": None, "rate": None, "eta_s": None,
            "_lens": chapters,
        }

    def _loop(self):
        while True:
            self.wake.wait()
            with self.lock:
                if not self.queue:
                    self.wake.clear()
                    continue
                book_id = self.queue.popleft()
                self.current = book_id
                self.stop_requested = False
            try:
                self._run(book_id)
            except Exception as e:
                set_status(book_id, "error", f"{type(e).__name__}: {e}")
            finally:
                with self.lock:
                    self.current = None
                    self.proc = None
                    self.progress.pop(book_id, None)

    def _update(self, book_id, **kw):
        with self.lock:
            p = self.progress.get(book_id)
            if p is not None:
                p.update(kw)
                self._recalc(p)

    @staticmethod
    def _recalc(p):
        if p["run_started"]:
            elapsed = time.time() - p["run_started"]
            processed = p["run_audio"] + p["current_t"]
            if elapsed > 5 and processed > 0:
                p["rate"] = processed / elapsed
                remaining = p["total_audio"] - p["done_audio"] - p["current_t"]
                p["eta_s"] = max(0.0, remaining / p["rate"])

    @staticmethod
    def _drop_temp_audio(book_id):
        """Delete the PC copy of an imported book's audio once it is fully transcribed."""
        with db.db() as con:
            bk = con.execute("SELECT m4b_path, audio_temp, phone_path FROM books WHERE id=?", (book_id,)).fetchone()
            n = con.execute("SELECT COUNT(*) FROM transcripts WHERE book_id=?", (book_id,)).fetchone()[0]
            total = con.execute("SELECT COUNT(*) FROM chapters WHERE book_id=?", (book_id,)).fetchone()[0]
        if not bk or not bk["audio_temp"] or not bk["phone_path"] or n < total:
            return
        p = Path(bk["m4b_path"])
        if BOOKS_DIR in p.parents:   # never touch files outside the app's own folder
            p.unlink(missing_ok=True)

    def _run(self, book_id):
        cfg = settings.get()
        note = None
        with self.lock:
            self.progress[book_id] = self._fresh_progress(book_id, phase="checking_gpu")
        set_status(book_id, "transcribing")

        if cfg["device"] in ("auto", "cuda") and not os.environ.get("TMM_FAKE_ENGINE"):
            probe = gpu.probe()
            if probe.get("ok"):
                cfg["device"] = "cuda"
            elif cfg["device"] == "cuda":
                set_status(book_id, "error", "GPU not usable: " + (probe.get("hint") or probe.get("error") or "unknown"))
                return
            else:
                cfg["device"] = "cpu"
                note = ("No usable NVIDIA GPU, so this runs on the CPU. It works, just slower."
                        if probe.get("no_gpu") else
                        "GPU not usable, running on the CPU instead (slower). " + (probe.get("hint") or probe.get("error") or ""))
        # "auto" model: the best fit for the hardware.
        if cfg["model"] == "auto":
            if cfg["device"] == "cuda":
                cfg["model"] = "large-v3-turbo"
            else:
                cfg["model"] = "small"
                cfg["beam_size"] = 1   # greedy decoding: roughly twice as fast on a CPU
        self._update(book_id, note=note, phase="starting")

        # Imported books may have had their audio deleted after an earlier run: copy it again.
        with db.db() as con:
            bk = con.execute("SELECT m4b_path, phone_path, audio_temp FROM books WHERE id=?", (book_id,)).fetchone()
        if not Path(bk["m4b_path"]).exists():
            if not bk["phone_path"]:
                set_status(book_id, "error", f"Audiobook file not found: {bk['m4b_path']}")
                return
            ser = settings.get()["adb_serial"] or None
            size = adbsync.file_size(ser, bk["phone_path"])
            self._update(book_id, phase="copying_from_phone", pull_done=0, pull_total=size)
            Path(bk["m4b_path"]).parent.mkdir(parents=True, exist_ok=True)
            ok, msg = adbsync.pull(ser, bk["phone_path"], bk["m4b_path"], size,
                                   lambda d, t: self._update(book_id, pull_done=d))
            if not ok:
                Path(bk["m4b_path"]).unlink(missing_ok=True)
                set_status(book_id, "error", "Could not copy the audio from the phone. Connect it and press "
                                             f"Transcribe again. ({msg})")
                return
            self._update(book_id, phase="starting")

        cmd = [sys.executable, "-m", "app.transcribe_job", book_id, json.dumps(cfg)]
        proc = subprocess.Popen(cmd, cwd=ROOT, env=gpu.child_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                **_popen_extra())
        with self.lock:
            self.proc = proc
            stop_now = self.stop_requested
        if stop_now:
            proc.terminate()

        err_tail = deque(maxlen=40)
        t = threading.Thread(target=lambda: [err_tail.append(l.rstrip()) for l in proc.stderr], daemon=True)
        t.start()

        got_done, error_msg = False, None
        for line in proc.stdout:
            if not line.startswith("@@EVT "):
                continue
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            kind = ev.get("event")
            if kind == "phase":
                self._update(book_id, phase=ev["phase"])
            elif kind == "device":
                self._update(book_id, device=ev["device"], compute_type=ev["compute_type"],
                             batched=ev.get("batched", False), model=cfg["model"])
            elif kind == "chapter_start":
                with self.lock:
                    p = self.progress[book_id]
                    if p["run_started"] is None:
                        p["run_started"] = time.time()
                    p.update(phase="transcribing", current_idx=ev["idx"], current_t=0.0,
                             current_len=p["_lens"].get(ev["idx"], 0.0))
            elif kind == "progress":
                self._update(book_id, current_t=ev["t"])
            elif kind == "chapter_done":
                with self.lock:
                    p = self.progress[book_id]
                    p["done_chapters"] = sorted(set(p["done_chapters"]) | {ev["idx"]})
                    p["done_audio"] += ev["audio"]
                    p["run_audio"] += ev["audio"]
                    p["current_t"] = 0.0
                    self._recalc(p)
            elif kind == "error":
                error_msg = ev.get("message")
            elif kind == "done":
                got_done = True

        rc = proc.wait()
        t.join(timeout=2)
        with self.lock:
            stopped = self.stop_requested

        if got_done:
            set_status(book_id, "aligning", note)
            self._update(book_id, phase="aligning")
            run_alignment(book_id)
            self._drop_temp_audio(book_id)
        elif stopped:
            set_status(book_id, self._idle_status(book_id))
        else:
            tail = "\n".join(list(err_tail)[-8:])
            msg = error_msg or f"Transcription process exited with code {rc}."
            if tail and not error_msg:
                msg += "\n" + tail
            set_status(book_id, "error", msg)


transcriber = Transcriber()
