import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    author      TEXT,
    epub_path   TEXT NOT NULL,
    epub_mode   TEXT NOT NULL,          -- copied | linked
    epub_hash   TEXT NOT NULL,
    m4b_path    TEXT NOT NULL,
    m4b_mode    TEXT NOT NULL,          -- copied | linked
    m4b_hash    TEXT NOT NULL,
    duration    REAL,
    status      TEXT NOT NULL DEFAULT 'not_transcribed',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chapters (
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    idx     INTEGER NOT NULL,
    title   TEXT,
    start   REAL NOT NULL,
    "end"   REAL NOT NULL,
    PRIMARY KEY (book_id, idx)
);

CREATE TABLE IF NOT EXISTS sections (
    book_id    TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    href       TEXT,
    title      TEXT,
    word_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (book_id, idx)
);

CREATE TABLE IF NOT EXISTS transcripts (
    book_id      TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,          -- audio chapter index
    model        TEXT,
    compute_type TEXT,
    device       TEXT,
    language     TEXT,
    word_count   INTEGER NOT NULL DEFAULT 0,
    audio_s      REAL,                      -- chapter length in seconds
    wall_s       REAL,                      -- time it took to transcribe
    path         TEXT NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (book_id, idx)
);

CREATE TABLE IF NOT EXISTS chapter_map (
    book_id   TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,          -- audio chapter index
    mode      TEXT NOT NULL,             -- auto | range | none
    sec_start INTEGER,
    sec_end   INTEGER,
    PRIMARY KEY (book_id, idx)
);

CREATE TABLE IF NOT EXISTS highlights (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id  TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    sec      INTEGER NOT NULL,           -- EPUB section index
    w_start  INTEGER NOT NULL,           -- first word index in the section
    w_end    INTEGER NOT NULL,           -- last word index in the section (inclusive)
    text     TEXT NOT NULL,
    color    TEXT NOT NULL DEFAULT 'yellow',
    note     TEXT NOT NULL DEFAULT '',
    created  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


@contextmanager
def db():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with db() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(books)")}
        if "status_note" not in cols:
            c.execute("ALTER TABLE books ADD COLUMN status_note TEXT")
        if "phone_path" not in cols:   # where the audio lives on the phone, for imports
            c.execute("ALTER TABLE books ADD COLUMN phone_path TEXT")
        if "audio_removed" not in cols:  # 1 = you removed the audio on purpose (no warning)
            c.execute("ALTER TABLE books ADD COLUMN audio_removed INTEGER NOT NULL DEFAULT 0")
        if "audio_temp" not in cols:   # 1 = delete the PC copy of the audio after transcription
            c.execute("ALTER TABLE books ADD COLUMN audio_temp INTEGER NOT NULL DEFAULT 0")
