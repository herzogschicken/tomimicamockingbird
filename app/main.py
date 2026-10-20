import json
import re
import shutil
import subprocess
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import adbsync, db, gpu, phoneimport, settings
from .config import AUDIO_EXTS, BOOKS_DIR, DATA_DIR, EPUB_EXTS, IS_WINDOWS, TMP_DIR
from .epub import parse_epub
from .media import aac_decoder_ok, probe_audio, quick_hash, which

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="tomimicamockingbird")
db.init()

from .jobs import run_alignment, set_status, transcriber  # noqa: E402  (needs the DB ready)


# ---------- helpers ----------

def _book_dir(book_id):
    if not book_id.isalnum():
        raise HTTPException(400, "Bad book id")
    return BOOKS_DIR / book_id


def _resolve_path(raw, exts):
    p = Path(raw.strip().strip('"').strip("'")).expanduser()
    try:
        p = p.resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        raise HTTPException(400, f"File not found: {p}")
    if not p.is_file():
        raise HTTPException(400, f"Not a file: {p}")
    if p.suffix.lower() not in exts:
        raise HTTPException(400, f"Expected {', '.join(sorted(exts))}, got {p.name}")
    return p


async def _save_upload(up, dest):
    with open(dest, "wb") as f:
        while chunk := await up.read(8 << 20):
            f.write(chunk)


def _has_upload(up):
    return up is not None and bool(up.filename)


@lru_cache(maxsize=32)
def _load_epub_json(path_str, mtime):
    return json.loads(Path(path_str).read_text(encoding="utf-8"))


def _epub_json(book_id):
    p = _book_dir(book_id) / "epub.json"
    if not p.exists():
        raise HTTPException(404, "Parsed EPUB not found")
    return _load_epub_json(str(p), p.stat().st_mtime)


@lru_cache(maxsize=8)
def _load_alignment(path_str, mtime):
    return json.loads(Path(path_str).read_text(encoding="utf-8"))


def _alignment(book_id):
    p = _book_dir(book_id) / "alignment.json"
    return _load_alignment(str(p), p.stat().st_mtime) if p.exists() else None


def _book_row(con, book_id):
    row = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Book not found")
    return dict(row)


# ---------- ingest ----------

def _ingest(book_id, bdir, epub, epub_mode, audio, audio_mode):
    epub_hash = quick_hash(epub)
    audio_hash = quick_hash(audio)

    with db.db() as con:
        dup = con.execute("SELECT id FROM books WHERE epub_hash=? AND m4b_hash=?",
                          (epub_hash, audio_hash)).fetchone()
    if dup:
        shutil.rmtree(bdir, ignore_errors=True)
        return {"id": dup["id"], "duplicate": True}

    meta = probe_audio(audio)
    parsed = parse_epub(epub, bdir)

    title = parsed["title"] or meta["title"] or Path(epub).stem
    author = parsed["author"] or meta["author"]
    with db.db() as con:
        con.execute(
            "INSERT INTO books (id,title,author,epub_path,epub_mode,epub_hash,"
            "m4b_path,m4b_mode,m4b_hash,duration,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (book_id, title, author, str(epub), epub_mode, epub_hash,
             str(audio), audio_mode, audio_hash, meta["duration"],
             "not_transcribed", time.time()),
        )
        con.executemany(
            'INSERT INTO chapters (book_id,idx,title,start,"end") VALUES (?,?,?,?,?)',
            [(book_id, c["idx"], c["title"], c["start"], c["end"]) for c in meta["chapters"]],
        )
        con.executemany(
            "INSERT INTO sections (book_id,idx,href,title,word_count) VALUES (?,?,?,?,?)",
            [(book_id, s["idx"], s["href"], s["title"], s["word_count"]) for s in parsed["sections"]],
        )
    return {"id": book_id, "duplicate": False}


# ---------- API ----------

@app.get("/api/health")
def health():
    return {
        "ffprobe": bool(which("ffprobe")),
        "ffmpeg": bool(which("ffmpeg")),
        "aac_decoder": aac_decoder_ok(),
        "adb": bool(which("adb")),
        "data_dir": str(DATA_DIR),
    }


@app.get("/api/books")
def list_books():
    with db.db() as con:
        rows = con.execute(
            "SELECT b.*, "
            "(SELECT COUNT(*) FROM chapters c WHERE c.book_id=b.id) AS chapter_count, "
            "(SELECT COUNT(*) FROM sections s WHERE s.book_id=b.id) AS section_count, "
            "(SELECT COALESCE(SUM(word_count),0) FROM sections s WHERE s.book_id=b.id) AS word_count, "
            "(SELECT COUNT(*) FROM transcripts t WHERE t.book_id=b.id) AS transcribed_count "
            "FROM books b ORDER BY b.created_at DESC"
        ).fetchall()
    books = []
    for r in rows:
        d = dict(r)
        d["m4b_missing"] = not Path(d["m4b_path"]).exists()
        d["audio_in_app"] = Path(d["m4b_path"]).exists() and BOOKS_DIR in Path(d["m4b_path"]).resolve().parents
        d["epub_missing"] = not Path(d["epub_path"]).exists()
        d["job"] = transcriber.snapshot(d["id"])
        books.append(d)
    return books


@app.post("/api/books")
async def add_book(
    epub_file: Optional[UploadFile] = File(None),
    epub_path: Optional[str] = Form(None),
    m4b_file: Optional[UploadFile] = File(None),
    m4b_path: Optional[str] = Form(None),
):
    book_id = uuid.uuid4().hex[:12]
    bdir = BOOKS_DIR / book_id
    files_dir = bdir / "files"
    files_dir.mkdir(parents=True)
    try:
        if _has_upload(epub_file):
            if Path(epub_file.filename).suffix.lower() not in EPUB_EXTS:
                raise HTTPException(400, "EPUB file must end in .epub")
            epub = files_dir / Path(epub_file.filename).name
            await _save_upload(epub_file, epub)
            epub_mode = "copied"
        elif epub_path and epub_path.strip():
            epub, epub_mode = _resolve_path(epub_path, EPUB_EXTS), "linked"
        else:
            raise HTTPException(400, "Give an EPUB file or a path to one")

        if _has_upload(m4b_file):
            if Path(m4b_file.filename).suffix.lower() not in AUDIO_EXTS:
                raise HTTPException(400, "Audiobook must be .m4b or .m4a")
            audio = files_dir / Path(m4b_file.filename).name
            await _save_upload(m4b_file, audio)
            audio_mode = "copied"
        elif m4b_path and m4b_path.strip():
            audio, audio_mode = _resolve_path(m4b_path, AUDIO_EXTS), "linked"
        else:
            raise HTTPException(400, "Give an audiobook file or a path to one")

        return await run_in_threadpool(_ingest, book_id, bdir, epub, epub_mode, audio, audio_mode)
    except HTTPException:
        shutil.rmtree(bdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(bdir, ignore_errors=True)
        raise HTTPException(400, f"Could not add book: {e}")


@app.get("/api/books/{book_id}")
def get_book(book_id: str):
    with db.db() as con:
        book = _book_row(con, book_id)
        book["chapters"] = [dict(r) for r in con.execute(
            'SELECT idx,title,start,"end" FROM chapters WHERE book_id=? ORDER BY idx', (book_id,))]
        book["sections"] = [dict(r) for r in con.execute(
            "SELECT idx,href,title,word_count FROM sections WHERE book_id=? ORDER BY idx", (book_id,))]
        book["transcripts"] = {r["idx"]: dict(r) for r in con.execute(
            "SELECT idx,model,device,compute_type,language,word_count,audio_s,wall_s FROM transcripts "
            "WHERE book_id=?", (book_id,))}
        book["chapter_map"] = {r["idx"]: dict(r) for r in con.execute(
            "SELECT idx,mode,sec_start,sec_end FROM chapter_map WHERE book_id=?", (book_id,))}
        runs = con.execute("SELECT value FROM app_state WHERE key=?", (f"runs:{book_id}",)).fetchone()
        book["runs"] = json.loads(runs["value"]) if runs else []
        book["current_run"] = _run_summary(con, book_id) if book["status"] in ("ready", "needs_review", "transcribed") else None
    book["job"] = transcriber.snapshot(book_id)
    al = _alignment(book_id)
    book["alignment"] = {k: al[k] for k in ("summary", "chapters", "gaps", "created")} if al else None
    book["m4b_missing"] = not Path(book["m4b_path"]).exists()
    book["audio_in_app"] = not book["m4b_missing"] and BOOKS_DIR in Path(book["m4b_path"]).resolve().parents
    book["epub_missing"] = not Path(book["epub_path"]).exists()
    return book


@app.get("/api/books/{book_id}/sections/{idx}")
def get_section(book_id: str, idx: int):
    data = _epub_json(book_id)
    if not 0 <= idx < len(data["sections"]):
        raise HTTPException(404, "Section not found")
    return data["sections"][idx]


@app.get("/api/books/{book_id}/images/{name}")
def get_image(book_id: str, name: str):
    if "/" in name or name.startswith("."):
        raise HTTPException(400, "Bad image name")
    p = _book_dir(book_id) / "images" / name
    if not p.is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(p)


@app.get("/api/books/{book_id}/transcript/{idx}")
def get_transcript(book_id: str, idx: int):
    with db.db() as con:
        row = con.execute("SELECT path FROM transcripts WHERE book_id=? AND idx=?", (book_id, idx)).fetchone()
    if not row or not Path(row["path"]).exists():
        raise HTTPException(404, "This chapter is not transcribed yet")
    data = json.loads(Path(row["path"]).read_text(encoding="utf-8"))
    data.pop("words", None)  # the preview only needs segments
    return data


@app.post("/api/books/{book_id}/transcribe")
def start_transcription(book_id: str):
    with db.db() as con:
        book = _book_row(con, book_id)
    if not Path(book["m4b_path"]).exists() and not book.get("phone_path"):
        raise HTTPException(400, f"Audiobook file not found: {book['m4b_path']}")
    transcriber.enqueue(book_id)
    return {"ok": True}


def _run_summary(con, book_id):
    """Stats of the transcript currently on disk, kept when the book is re-transcribed."""
    rows = [dict(r) for r in con.execute(
        "SELECT idx,model,device,compute_type,word_count,audio_s,wall_s,path FROM transcripts WHERE book_id=?",
        (book_id,))]
    if not rows:
        return None
    extra = {}
    try:
        d = json.loads(Path(rows[0]["path"]).read_text(encoding="utf-8"))
        extra = {"batched": d.get("batched", False), "beam_size": d.get("beam_size")}
    except Exception:
        pass
    audio = sum(r["audio_s"] or 0 for r in rows)
    wall = sum(r["wall_s"] or 0 for r in rows)
    al = _alignment(book_id)
    s = al["summary"] if al else {}
    return {
        "model": ", ".join(sorted({r["model"] for r in rows if r["model"]})),
        "device": rows[0]["device"], "compute_type": rows[0]["compute_type"], **extra,
        "chapters": len(rows), "words_heard": sum(r["word_count"] for r in rows),
        "audio_s": round(audio, 1), "wall_s": round(wall, 1),
        "speed": round(audio / wall, 1) if wall else None,
        "timed_ratio": s.get("timed_ratio"), "chapters_ok": s.get("chapters_ok"),
        "chapters_review": s.get("chapters_review"), "at": time.time(),
    }


@app.post("/api/books/{book_id}/retranscribe")
def retranscribe(book_id: str):
    """Throw away the transcript and alignment and start again with the current settings.
    Highlights, chapter mapping and reading position are kept (they hang on the EPUB text)."""
    transcriber.pause(book_id, wait=True)
    with db.db() as con:
        book = _book_row(con, book_id)
        if not Path(book["m4b_path"]).exists() and not book.get("phone_path"):
            raise HTTPException(400, f"Audiobook file not found: {book['m4b_path']}")
        prev = _run_summary(con, book_id)
        if prev:
            row = con.execute("SELECT value FROM app_state WHERE key=?", (f"runs:{book_id}",)).fetchone()
            runs = json.loads(row["value"]) if row else []
            runs.append(prev)
            con.execute("INSERT OR REPLACE INTO app_state (key,value) VALUES (?,?)",
                        (f"runs:{book_id}", json.dumps(runs[-10:])))
        con.execute("DELETE FROM transcripts WHERE book_id=?", (book_id,))
        con.execute("UPDATE books SET status='not_transcribed', status_note=NULL WHERE id=?", (book_id,))
    bdir = _book_dir(book_id)
    shutil.rmtree(bdir / "transcript", ignore_errors=True)
    (bdir / "alignment.json").unlink(missing_ok=True)
    transcriber.enqueue(book_id)
    return {"ok": True, "previous": prev}


@app.post("/api/books/{book_id}/remove-audio")
def remove_audio(book_id: str):
    """Free disk space: delete the app's copy of the audio once a book is fully transcribed.
    The reader keeps working. Audio you linked by path is your own file and is never deleted."""
    with db.db() as con:
        book = _book_row(con, book_id)
        n = con.execute("SELECT COUNT(*) FROM transcripts WHERE book_id=?", (book_id,)).fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM chapters WHERE book_id=?", (book_id,)).fetchone()[0]
    if n < total or transcriber.snapshot(book_id):
        raise HTTPException(400, "Finish transcribing this book first")
    p = Path(book["m4b_path"])
    freed = 0
    if p.exists():
        if BOOKS_DIR not in p.resolve().parents:
            raise HTTPException(400, f"This audio is your own file at {p}. The app never deletes your files. "
                                     "Delete it yourself if you want to.")
        freed = p.stat().st_size
        p.unlink()
    with db.db() as con:
        con.execute("UPDATE books SET audio_removed=1 WHERE id=?", (book_id,))
    return {"ok": True, "freed": freed}


@app.post("/api/books/{book_id}/pause")
def pause_transcription(book_id: str):
    return {"ok": transcriber.pause(book_id)}


@app.post("/api/books/{book_id}/align")
async def align_book(book_id: str):
    with db.db() as con:
        book = _book_row(con, book_id)
        n = con.execute("SELECT COUNT(*) FROM transcripts WHERE book_id=?", (book_id,)).fetchone()[0]
    if isinstance(transcriber.snapshot(book_id), dict):
        raise HTTPException(409, "Wait for transcription to finish or pause it first")
    if n == 0:
        raise HTTPException(400, "Transcribe at least one chapter first")
    prev = book["status"]
    set_status(book_id, "aligning")
    summary = await run_in_threadpool(run_alignment, book_id)
    if summary is None:
        raise HTTPException(500, "Alignment failed, see the book card for details")
    with db.db() as con:
        st = con.execute("SELECT status FROM books WHERE id=?", (book_id,)).fetchone()["status"]
    if st == "aligning":  # partial book: alignment ran but the book isn't complete yet
        set_status(book_id, prev if prev != "aligning" else "paused")
    return summary


@app.put("/api/books/{book_id}/chapters/{idx}/map")
async def map_chapter(book_id: str, idx: int, body: dict = Body(...)):
    mode = body.get("mode", "auto")
    with db.db() as con:
        _book_row(con, book_id)
        nsec = con.execute("SELECT COUNT(*) FROM sections WHERE book_id=?", (book_id,)).fetchone()[0]
        if not con.execute("SELECT 1 FROM chapters WHERE book_id=? AND idx=?", (book_id, idx)).fetchone():
            raise HTTPException(404, "Chapter not found")
        if mode == "auto":
            con.execute("DELETE FROM chapter_map WHERE book_id=? AND idx=?", (book_id, idx))
        elif mode == "none":
            con.execute("INSERT OR REPLACE INTO chapter_map (book_id,idx,mode) VALUES (?,?,'none')", (book_id, idx))
        elif mode == "range":
            try:
                a, b = int(body["sec_start"]), int(body["sec_end"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(400, "sec_start and sec_end are required")
            if not (0 <= a <= b < nsec):
                raise HTTPException(400, "Section range is out of bounds")
            con.execute("INSERT OR REPLACE INTO chapter_map (book_id,idx,mode,sec_start,sec_end) "
                        "VALUES (?,?,'range',?,?)", (book_id, idx, a, b))
        else:
            raise HTTPException(400, "mode must be auto, range or none")
    if (_book_dir(book_id) / "alignment.json").exists() and transcriber.snapshot(book_id) is None:
        return await align_book(book_id)
    return {"ok": True}


@app.get("/api/books/{book_id}/aligned/{ch_idx}")
def aligned_chapter(book_id: str, ch_idx: int):
    """EPUB text for one audio chapter, with per-word times and match kinds, for review."""
    al = _alignment(book_id)
    if not al:
        raise HTTPException(404, "Not aligned yet")
    ch = next((c for c in al["chapters"] if c["idx"] == ch_idx), None)
    if not ch or not ch.get("epub_start"):
        raise HTTPException(404, "No EPUB text matched this chapter")
    epub = _epub_json(book_id)
    s0, s1 = ch["epub_start"][0], ch["epub_end"][0]
    out = []
    for sec in epub["sections"][s0:s1 + 1]:
        a = al["sections"].get(str(sec["idx"])) or {"t": [], "k": ""}
        out.append({"idx": sec["idx"], "title": sec["title"], "blocks": sec["blocks"],
                    "t": a["t"], "k": a["k"]})
    return {"chapter": ch, "sections": out}


@app.get("/api/books/{book_id}/reader")
def reader_data(book_id: str):
    """Everything the reader needs in one go: text, word times, chapters, last position."""
    with db.db() as con:
        book = _book_row(con, book_id)
        chapters = [dict(r) for r in con.execute(
            'SELECT idx,title,start,"end" FROM chapters WHERE book_id=? ORDER BY idx', (book_id,))]
        pos = con.execute("SELECT value FROM app_state WHERE key=?", (f"pos:{book_id}",)).fetchone()
        con.execute("INSERT OR REPLACE INTO app_state (key,value) VALUES ('last_book',?)", (book_id,))
    al = _alignment(book_id)
    if not al:
        raise HTTPException(400, "This book is not aligned yet. Transcribe it first.")
    alch = {c["idx"]: c for c in al["chapters"]}
    for c in chapters:
        a = alch.get(c["idx"], {})
        c.update(status=a.get("status"), epub_start=a.get("epub_start"), epub_end=a.get("epub_end"))
    epub = _epub_json(book_id)
    return {
        "book": {k: book[k] for k in ("id", "title", "author", "duration", "status")},
        "chapters": chapters,
        "sections": [{"idx": s["idx"], "title": s["title"], "blocks": s["blocks"]} for s in epub["sections"]],
        "times": {k: v["t"] for k, v in al["sections"].items()},
        "position": json.loads(pos["value"]) if pos else None,
    }


@app.put("/api/books/{book_id}/position")
def save_position(book_id: str, body: dict = Body(...)):
    try:
        p = float(body["pos"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "pos is required")
    with db.db() as con:
        _book_row(con, book_id)
        con.execute("INSERT OR REPLACE INTO app_state (key,value) VALUES (?,?)",
                    (f"pos:{book_id}", json.dumps({"pos": round(p, 2), "at": time.time()})))
    return {"ok": True}


HL_COLORS = {"yellow", "green", "blue", "pink"}


def _hl_row(r):
    return {k: r[k] for k in ("id", "sec", "w_start", "w_end", "text", "color", "note", "created")}


@app.get("/api/books/{book_id}/highlights")
def list_highlights(book_id: str):
    with db.db() as con:
        _book_row(con, book_id)
        rows = con.execute("SELECT * FROM highlights WHERE book_id=? ORDER BY sec, w_start", (book_id,)).fetchall()
    return [_hl_row(r) for r in rows]


@app.post("/api/books/{book_id}/highlights")
def add_highlight(book_id: str, body: dict = Body(...)):
    try:
        sec, a, b = int(body["sec"]), int(body["w_start"]), int(body["w_end"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "sec, w_start and w_end are required")
    color = body.get("color", "yellow")
    if color not in HL_COLORS:
        raise HTTPException(400, f"color must be one of {sorted(HL_COLORS)}")
    epub = _epub_json(book_id)
    if not 0 <= sec < len(epub["sections"]):
        raise HTTPException(400, "Section out of range")
    words = [w for bl in epub["sections"][sec]["blocks"] for w in bl.get("text", "").split(" ") if w]
    if a > b:
        a, b = b, a
    if a < 0 or b >= len(words):
        raise HTTPException(400, "Word range out of range")
    text = " ".join(words[a:b + 1])
    with db.db() as con:
        _book_row(con, book_id)
        # A new highlight replaces any it overlaps, so there are never stacked highlights.
        con.execute("DELETE FROM highlights WHERE book_id=? AND sec=? AND w_start<=? AND w_end>=?",
                    (book_id, sec, b, a))
        cur = con.execute(
            "INSERT INTO highlights (book_id,sec,w_start,w_end,text,color,note,created) VALUES (?,?,?,?,?,?,?,?)",
            (book_id, sec, a, b, text, color, str(body.get("note", ""))[:2000], time.time()))
        row = con.execute("SELECT * FROM highlights WHERE id=?", (cur.lastrowid,)).fetchone()
    return _hl_row(row)


@app.patch("/api/books/{book_id}/highlights/{hl_id}")
def edit_highlight(book_id: str, hl_id: int, body: dict = Body(...)):
    with db.db() as con:
        row = con.execute("SELECT * FROM highlights WHERE id=? AND book_id=?", (hl_id, book_id)).fetchone()
        if not row:
            raise HTTPException(404, "Highlight not found")
        if "color" in body:
            if body["color"] not in HL_COLORS:
                raise HTTPException(400, "Bad color")
            con.execute("UPDATE highlights SET color=? WHERE id=?", (body["color"], hl_id))
        if "note" in body:
            con.execute("UPDATE highlights SET note=? WHERE id=?", (str(body["note"])[:2000], hl_id))
        row = con.execute("SELECT * FROM highlights WHERE id=?", (hl_id,)).fetchone()
    return _hl_row(row)


@app.delete("/api/books/{book_id}/highlights/{hl_id}")
def delete_highlight(book_id: str, hl_id: int):
    with db.db() as con:
        n = con.execute("DELETE FROM highlights WHERE id=? AND book_id=?", (hl_id, book_id)).rowcount
    if not n:
        raise HTTPException(404, "Highlight not found")
    return {"ok": True}


_STYLE_WORDS = re.compile(r"\s+(Regular|Bold|Italic|Oblique|Light|Medium|Semibold|SemiBold|Demibold|Black|"
                          r"Heavy|Thin|ExtraLight|Extra Light|ExtraBold|Extra Bold|Condensed|Narrow)\b.*$", re.I)


def _windows_fonts():
    import winreg
    names = set()
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        except OSError:
            continue
        i = 0
        while True:
            try:
                name, _val, _t = winreg.EnumValue(key, i)
            except OSError:
                break
            i += 1
            name = re.sub(r"\s*\((TrueType|OpenType|All res)\)\s*$", "", name)
            for part in name.split(" & "):
                part = _STYLE_WORDS.sub("", part).strip()
                if part:
                    names.add(part)
    return names


@lru_cache(maxsize=1)
def _font_families():
    try:
        if IS_WINDOWS:
            names = _windows_fonts()
        else:
            out = subprocess.run(["fc-list", "--format", "%{family[0]}\n"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=10).stdout
            names = {l.strip() for l in out.splitlines() if l.strip() and not l.startswith(".")}
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    return sorted(names, key=str.lower)


@app.get("/api/fonts")
def fonts(refresh: bool = False):
    if refresh:
        _font_families.cache_clear()
    return {"fonts": _font_families()}


READER_LOOK_KEYS = {"theme", "fs", "col", "lh", "ws", "ps", "font", "wordHL"}


def _clean_look(d):
    return {k: v for k, v in (d or {}).items() if k in READER_LOOK_KEYS and isinstance(v, (str, int, float, bool))}


@app.get("/api/reader/config")
def get_reader_config():
    with db.db() as con:
        row = con.execute("SELECT value FROM app_state WHERE key='reader'").fetchone()
    return json.loads(row["value"]) if row else {"prefs": None, "looks": {}}


@app.put("/api/reader/config")
def put_reader_config(body: dict = Body(...)):
    looks = body.get("looks") or {}
    if not isinstance(looks, dict) or len(looks) > 50:
        raise HTTPException(400, "looks must be an object with at most 50 entries")
    cfg = {"prefs": _clean_look(body.get("prefs")),
           "looks": {str(k)[:60]: _clean_look(v) for k, v in looks.items()}}
    with db.db() as con:
        con.execute("INSERT OR REPLACE INTO app_state (key,value) VALUES ('reader',?)", (json.dumps(cfg),))
    return cfg


def _set_book_fields(book_id, **fields):
    with db.db() as con:
        for k, v in fields.items():
            if k in ("phone_path", "audio_temp"):
                con.execute(f"UPDATE books SET {k}=? WHERE id=?", (v, book_id))


importer = phoneimport.Importer(_ingest, lambda bid: transcriber.enqueue(bid), _set_book_fields)


@app.get("/api/phone/files")
async def phone_files():
    res = await run_in_threadpool(adbsync.scan, phoneimport.serial(), phoneimport.folders())
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "Could not scan the phone")
    with db.db() as con:
        known = {r["phone_path"]: r["id"] for r in con.execute(
            "SELECT id, phone_path FROM books WHERE phone_path IS NOT NULL")}
    books = phoneimport.suggest_pairs(res["files"])
    for b in books:
        b["book_id"] = known.get(b["path"])
    return {"audiobooks": books, "epubs": [f for f in res["files"] if f["kind"] == "epub"],
            "folders": phoneimport.folders()}


@app.post("/api/phone/import")
async def phone_import(
    audio_path: str = Form(...),
    audio_size: Optional[int] = Form(None),
    epub_phone: Optional[str] = Form(None),
    epub_size: Optional[int] = Form(None),
    epub_path: Optional[str] = Form(None),
    epub_file: Optional[UploadFile] = File(None),
):
    if not audio_path.lower().endswith(tuple(AUDIO_EXTS)):
        raise HTTPException(400, "Pick an .m4b or .m4a audiobook")
    epub_local = None
    if _has_upload(epub_file):
        if Path(epub_file.filename).suffix.lower() not in EPUB_EXTS:
            raise HTTPException(400, "EPUB file must end in .epub")
        tmpd = TMP_DIR / uuid.uuid4().hex[:8]
        tmpd.mkdir(parents=True)
        epub_local = tmpd / Path(epub_file.filename).name
        await _save_upload(epub_file, epub_local)
    elif epub_path and epub_path.strip():
        epub_local = _resolve_path(epub_path, EPUB_EXTS)
    elif not epub_phone:
        raise HTTPException(400, "Pick the EPUB for this audiobook (on the phone or on the PC)")
    jid = importer.start(audio_path, audio_size, epub_phone if not epub_local else None,
                         epub_size, str(epub_local) if epub_local else None)
    return {"id": jid}


@app.get("/api/phone/imports")
def phone_imports():
    return importer.list()


@app.get("/api/sync/adb")
def sync_adb():
    s = settings.get()
    return adbsync.get(s["adb_serial"] or None, pkg=s["player_package"])


@app.get("/api/sync/devices")
def sync_devices():
    return {"devices": adbsync.devices(), "serial": settings.get()["adb_serial"]}


@app.post("/api/sync/connect")
def sync_connect(body: dict = Body(...)):
    address = (body.get("address") or settings.get()["adb_address"] or "").strip()
    if not address:
        raise HTTPException(400, "Give the phone's address, e.g. 192.168.1.23:5555")
    settings.update({"adb_address": address})
    return adbsync.connect(address)


@app.get("/api/jobs")
def jobs():
    return transcriber.all_snapshots()


@app.get("/api/settings")
def get_settings():
    return {"settings": settings.get(), "models": settings.MODELS,
            "compute_types": settings.COMPUTE_TYPES, "devices": settings.DEVICES}


@app.put("/api/settings")
def put_settings(changes: dict = Body(...)):
    try:
        return {"settings": settings.update(changes)}
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/gpu")
def gpu_check():
    return gpu.probe()


@app.delete("/api/books/{book_id}")
def delete_book(book_id: str):
    bdir = _book_dir(book_id)
    transcriber.pause(book_id, wait=True)
    with db.db() as con:
        _book_row(con, book_id)
        con.execute("DELETE FROM books WHERE id=?", (book_id,))
    # Linked files live outside bdir and are never touched.
    shutil.rmtree(bdir, ignore_errors=True)
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Clean up after a crash: temp files, and book folders that never made it into the DB.
shutil.rmtree(TMP_DIR, ignore_errors=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)
with db.db() as _con:
    _known = {r["id"] for r in _con.execute("SELECT id FROM books")}
for _d in BOOKS_DIR.iterdir():
    if _d.is_dir() and _d.name not in _known:
        shutil.rmtree(_d, ignore_errors=True)

transcriber.resume_interrupted()
