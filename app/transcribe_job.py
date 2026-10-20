"""Transcribe one book, chapter by chapter. Runs as a subprocess:

    python -m app.transcribe_job <book_id> '<settings json>'

Progress goes to stdout as lines starting with "@@EVT " followed by JSON.
Each finished chapter is written to disk and recorded in the DB at once,
so killing this process loses at most the chapter in progress.
Chapters already in the transcripts table are skipped, which is how resume works.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from . import db
from .config import BOOKS_DIR, DATA_DIR

SAMPLE_RATE = 16000
MODELS_DIR = DATA_DIR / "models"


def emit(event, **data):
    sys.stdout.write("@@EVT " + json.dumps({"event": event, **data}) + "\n")
    sys.stdout.flush()


def load_chapter_audio(path, start, end):
    """Decode one chapter to 16 kHz mono float32 with ffmpeg. Seeking with -ss before -i is
    sample-accurate for audio, so time 0 in the result is exactly the chapter start."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
           "-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {res.stderr.decode(errors='replace')[-400:]}")
    return np.frombuffer(res.stdout, dtype=np.float32)


# ---------------- engines ----------------

class WhisperEngine:
    def __init__(self, model, device, compute_type, batched=False, batch_size=16):
        from . import gpu
        gpu.add_dll_dirs()
        from faster_whisper import BatchedInferencePipeline, WhisperModel
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        if device == "cpu":
            compute_type = "int8"
        self.device, self.compute_type = device, compute_type
        self.model = WhisperModel(model, device=device, compute_type=compute_type,
                                  download_root=str(MODELS_DIR))
        # Batched mode: VAD cuts the audio into speech chunks and the GPU decodes
        # batch_size of them at once instead of one after another.
        self.batched = batched and device != "cpu"
        self.batch_size = batch_size
        self.pipeline = BatchedInferencePipeline(self.model) if self.batched else None

    def transcribe(self, audio, language, beam_size):
        if self.pipeline:
            return self.pipeline.transcribe(
                audio,
                language=language or None,
                beam_size=beam_size,
                word_timestamps=True,
                vad_filter=True,
                batch_size=self.batch_size,
            )
        segments, info = self.model.transcribe(
            audio,
            language=language or None,
            beam_size=beam_size,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            # Off: stops one misheard line from snowballing into repeated junk over a long chapter.
            condition_on_previous_text=False,
            hallucination_silence_threshold=2.0,
        )
        return segments, info


class FakeEngine:
    """Test hook (TMM_FAKE_ENGINE=1): emits one fake word every 0.4 s of audio."""
    device, compute_type, batched = "fake", "none", False

    def __init__(self, *a):
        pass

    def transcribe(self, audio, language, beam_size):
        from types import SimpleNamespace as NS
        dur = len(audio) / SAMPLE_RATE
        delay = float(os.environ.get("TMM_FAKE_DELAY", "0"))

        def gen():
            t, n = 0.0, 0
            while t + 2 <= dur:
                words = [NS(start=t + i * 0.4, end=t + i * 0.4 + 0.3, word=f" w{n + i}", probability=0.9)
                         for i in range(5)]
                n += 5
                time.sleep(delay)
                yield NS(start=t, end=t + 2, text=" ".join(w.word for w in words), words=words)
                t += 2
        return gen(), NS(language=language or "en", language_probability=0.99)


def make_engine(model, device, compute_type, batched=False, batch_size=16):
    if os.environ.get("TMM_FAKE_ENGINE") == "1":
        return FakeEngine()
    return WhisperEngine(model, device, compute_type, batched, batch_size)


# ---------------- main ----------------

def main(book_id, cfg):
    with db.db() as con:
        book = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        if not book:
            emit("error", message="Book not found")
            return 2
        chapters = [dict(r) for r in con.execute(
            'SELECT idx,title,start,"end" FROM chapters WHERE book_id=? ORDER BY idx', (book_id,))]
        done = {r["idx"]: r["language"] for r in con.execute(
            "SELECT idx,language FROM transcripts WHERE book_id=?", (book_id,))}

    audio_path = Path(book["m4b_path"])
    if not audio_path.exists():
        emit("error", message=f"Audiobook file not found: {audio_path}")
        return 2

    todo = [c for c in chapters if c["idx"] not in done]
    if not todo:
        emit("done")
        return 0

    out_dir = BOOKS_DIR / book_id / "transcript"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Reuse the language already detected for this book, if any.
    language = cfg.get("language") or next((l for l in done.values() if l), None)

    emit("phase", phase="loading_model", model=cfg["model"])
    try:
        engine = make_engine(cfg["model"], cfg["device"], cfg["compute_type"],
                             cfg.get("batched", False), cfg.get("batch_size", 16))
    except Exception as e:
        emit("error", message=f"Could not load model {cfg['model']} on {cfg['device']}: "
                              f"{type(e).__name__}: {e}. The first run needs internet to download the model.")
        return 1
    emit("device", device=engine.device, compute_type=engine.compute_type,
         batched=bool(getattr(engine, "batched", False)))

    for ch in todo:
        idx, length = ch["idx"], ch["end"] - ch["start"]
        emit("chapter_start", idx=idx)
        t0 = time.time()
        audio = load_chapter_audio(audio_path, ch["start"], ch["end"])
        segments, info = engine.transcribe(audio, language, cfg.get("beam_size", 5))

        words, segs = [], []
        last_emit = 0.0
        for seg in segments:
            segs.append([round(seg.start, 3), round(seg.end, 3), seg.text.strip()])
            for w in seg.words or []:
                words.append([round(w.start, 3), round(w.end, 3), w.word.strip(), round(w.probability, 3)])
            if time.time() - last_emit > 0.5:
                emit("progress", idx=idx, t=min(seg.end, length))
                last_emit = time.time()

        if not language and getattr(info, "language_probability", 0) >= 0.8 and length > 60:
            language = info.language
        wall = time.time() - t0

        data = {
            "book_id": book_id, "idx": idx, "title": ch["title"],
            "start": ch["start"], "end": ch["end"],
            "model": cfg["model"], "device": engine.device, "compute_type": engine.compute_type,
            "batched": bool(getattr(engine, "batched", False)), "beam_size": cfg.get("beam_size", 5),
            "language": info.language, "created": time.time(), "wall_s": round(wall, 2),
            "segments": segs,
            "words": words,  # [start, end, word, probability], seconds from chapter start
        }
        path = out_dir / f"ch{idx:04d}.json"
        tmp = path.with_suffix(".json.part")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        with db.db() as con:
            con.execute(
                "INSERT OR REPLACE INTO transcripts (book_id,idx,model,compute_type,device,language,"
                "word_count,audio_s,wall_s,path,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (book_id, idx, cfg["model"], engine.compute_type, engine.device, info.language,
                 len(words), length, wall, str(path), time.time()))
        emit("chapter_done", idx=idx, words=len(words), wall=wall, audio=length)

    emit("done")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1], json.loads(sys.argv[2])))
    except Exception as e:
        emit("error", message=f"{type(e).__name__}: {e}")
        sys.exit(1)
