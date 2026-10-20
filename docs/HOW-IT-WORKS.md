# How it works

A short tour for people who want to change the code or understand the results.

## Parts

| File | Job |
|---|---|
| `app/main.py` | Web server (FastAPI) and all `/api/...` endpoints |
| `app/epub.py` | Reads the EPUB: OPF, spine order, table of contents, text blocks, images |
| `app/media.py` | `ffprobe` chapter list, quick file fingerprint |
| `app/transcribe_job.py` | Runs Whisper on one book, chapter by chapter, in its own process |
| `app/jobs.py` | Background queue: one book at a time, pause, resume, crash recovery |
| `app/normalize.py` | Turns written and spoken words into comparable tokens |
| `app/align.py` | Matches the transcript to the EPUB and gives every word a time |
| `app/adbsync.py` | Reads the phone player's position over adb, scans and copies phone files |
| `app/phoneimport.py` | "Import from phone" jobs |
| `app/gpu.py` | Finds the NVIDIA libraries on Linux and Windows |
| `app/static/` | The web pages: library, book details, reader (plain JavaScript, no build step) |
| `tests/synthetic_align.py` | Stress test for the aligner with a made-up book and fake Whisper errors |

## Transcription

- `ffmpeg` decodes each audio chapter to 16 kHz mono. Each chapter is transcribed separately, so
  times start at 0 for every chapter and a crash loses at most one chapter.
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) runs Whisper with word timestamps and
  voice activity detection. On an NVIDIA card it uses batched mode (many speech pieces at once).
- The `auto` model setting picks `large-v3-turbo` on a GPU and `small` on a CPU.
- GPU support needs only the NVIDIA driver plus the `nvidia-cublas-cu12` pip package
  (`requirements-gpu.txt`). The engine (CTranslate2 4.x) loads cuBLAS for CUDA 12 at runtime.

## Matching the transcript to the book

1. **Normalize** both sides the same way: lowercase, no punctuation, curly quotes fixed, numbers
   spelled out (`1984` and "nineteen eighty-four" match), roman numerals after "Chapter" read as
   numbers, hyphenated words split.
2. **Anchors:** 7-word phrases that appear exactly once in the whole book and once in the whole
   transcript. The longest chain that moves forward in both is kept (longest increasing
   subsequence). This also finds which text belongs to which audio chapter, so chapters never need
   matching by hand.
3. **Between anchors:** word-by-word edit-distance alignment (rapidfuzz). Big gaps are first split
   with shorter unique phrases.
4. **Misheard words** (names, invented words) get a time estimated from their matched neighbours,
   or a spelling-similarity match.
5. **Forward only:** a repeated phrase always matches its earliest unread copy.
6. **Scores:** each audio chapter gets the share of heard words that matched. Long unmatched runs
   are reported as gaps ("text not heard" or "audio not in book").

`tests/synthetic_align.py` checks this against a generated book with 7 to 25% wrong words,
misheard names, announcements, credits, a skipped passage, extra audio and a repeated refrain.
Typical result: about 99.5% of words within half a second of the truth.

## The reader

- Every word is a `<span>` with its global index. Words are grouped into visual lines by their
  vertical position, and a highlight bar is moved to the current line.
- The current position comes from one of two clocks:
  - **Phone:** `adb shell "cat /proc/uptime; dumpsys media_session"` gives the player's position,
    speed and the phone time it was written. The reader adds `(uptime - updated) x speed` while
    playing, so it stays exact even though some players only refresh every few seconds.
  - **Manual:** start time + elapsed time x speed, corrected by clicks, typed times and arrow keys.
- In **auto** mode the phone wins when it answers. If it stops answering, the manual clock takes over
  from the last known position.
- A binary search over the word times finds the current word for any position.

## Data

Stored in the data folder (see README):

| Path | What |
|---|---|
| `library.db` | SQLite: books, chapters, sections, transcript index, settings, highlights, positions |
| `books/<id>/epub.json`, `images/` | Parsed EPUB |
| `books/<id>/transcript/chNNNN.json` | Word timestamps per audio chapter |
| `books/<id>/alignment.json` | Time and match type for every EPUB word, chapter scores, gaps |
| `books/<id>/files/` | Copied uploads and imported audio |
| `models/` | Downloaded Whisper models |

## Settings by environment variable

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | 8765 | Port for the web page |
| `HOST` | 127.0.0.1 | Set to `0.0.0.0` to open the app from other devices on your network (no password!) |
| `TMM_DATA` | see README | Data folder |
| `TMM_NO_GPU` | unset | Set to `1` to skip installing GPU support |
