#!/usr/bin/env python3
"""Stress test for the aligner with a made-up book and a simulated, imperfect transcript.

    .venv/bin/python tests/synthetic_align.py [seed]

Simulates: front and back matter that is never read, opening and closing credits,
narrator announcements, roman-numeral headings, digits vs words, misheard names,
substituted / dropped / extra words, one skipped passage, one extra passage in the audio,
and a refrain repeated many times through the book.
Prints how close each EPUB word's time is to the truth.
"""
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.align import align_book  # noqa: E402

_seed = next((a for a in sys.argv[1:] if a.isdigit()), "7")
rng = random.Random(int(_seed))

FUNC = "the a and of to in he she it was had that with his her for on at by as but they from".split()
CONTENT = sorted({("".join(rng.choice("bcdfghjklmnprstvw") + rng.choice("aeiou") for _ in range(rng.randint(1, 3))))
                for _ in range(1500)})
NAMES = {"Thraxios": ["thrax", "eos"], "Elowenne": ["ella", "when"], "Quillamor": ["quill", "a", "more"]}
REFRAIN = "The wind howled across the empty moor that night".split()
import os
from num2words import num2words
NCH = int(os.environ.get("SYN_CHAPTERS", "8"))
SUB_RATE = float(os.environ.get("SYN_SUB", "0.07"))


def to_roman(n):
    out = ""
    for v, r in ((50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= v:
            out, n = out + r, n - v
    return out


ROMAN = [to_roman(i + 1) for i in range(NCH)]
NUMWORDS = [num2words(i + 1).replace(" ", "-") for i in range(NCH)]


def sentence():
    n = rng.randint(6, 18)
    words = []
    for _ in range(n):
        r = rng.random()
        if r < 0.45:
            words.append(rng.choice(FUNC))
        elif r < 0.5:
            words.append(rng.choice(list(NAMES)))
        else:
            words.append(rng.choice(CONTENT))
    if rng.random() < 0.08:
        words += ["in", "1984"]
    words[0] = words[0].capitalize()
    words[-1] += rng.choice([".", ".", ",", "!", "?"])
    if rng.random() < 0.15:
        words[0] = "“" + words[0]
        words[-1] += "”"
    return words


def paragraph():
    p = []
    for _ in range(rng.randint(2, 6)):
        p += sentence()
    if rng.random() < 0.25:
        p += REFRAIN[:-1] + [REFRAIN[-1] + "."]
    return " ".join(p)


# ---------- EPUB ----------
sections = [{"idx": 0, "title": "Title page", "blocks": [
    {"type": "h1", "text": "The Synthetic Moor"},
    {"type": "p", "text": "Copyright 2026 by Nobody. All rights reserved. " + " ".join(sentence() * 10)}]}]
for c in range(NCH):
    blocks = [{"type": "h2", "text": f"Chapter {ROMAN[c]}: The {CONTENT[c].capitalize()}"}]
    blocks += [{"type": "p", "text": paragraph()} for _ in range(rng.randint(12, 20))]
    sections.append({"idx": c + 1, "title": f"Chapter {ROMAN[c]}", "blocks": blocks})
sections.append({"idx": NCH + 1, "title": "Acknowledgements", "blocks": [
    {"type": "p", "text": paragraph()} for _ in range(4)]})
for s in sections:
    s["linear"] = True
epub = {"sections": sections}

# ---------- audio + truth ----------
chapters, chapter_words, truth = [], {}, {}  # truth[(sec, word)] = whole-file time or None
clock = 0.0


def credits(n):
    return [rng.choice(CONTENT) for _ in range(n)]


def speak(words, out, t):
    for w in words:
        d = rng.uniform(0.25, 0.5)
        out.append([round(t, 3), round(t + d * 0.9, 3), w, 0.9])
        t += d
    return t


# chapter 0: opening credits
words = []
t = speak(["This", "is", "Big", "Sound", "Audio."] + credits(60), words, 1.0)
chapters.append({"idx": 0, "start": 0.0, "end": t + 1})
chapter_words[0] = words
clock = t + 1

skip_chapter, extra_chapter = 3, 5
for c in range(NCH):
    sec = sections[c + 1]
    start = clock
    words, t = [], 0.8
    t = speak(["Chapter", NUMWORDS[c] + ".", "The", CONTENT[c] + "."], words, t)
    t += 1.0
    wi = 0
    for bi, b in enumerate(sec["blocks"]):
        toks = b["text"].split(" ")
        if bi == 0:  # heading: already announced, the narrator doesn't read it again
            for _ in toks:
                truth[(sec["idx"], wi)] = None
                wi += 1
            continue
        skipping = c == skip_chapter and bi == 4
        for tok in toks:
            if skipping:
                truth[(sec["idx"], wi)] = None
                wi += 1
                continue
            truth[(sec["idx"], wi)] = start + t
            clean = tok.strip("“”.,!?")
            heard = [tok]
            r = rng.random()
            if clean in NAMES:
                heard = NAMES[clean]
            elif clean == "1984":
                heard = ["nineteen", "eighty-four."]
            elif r < SUB_RATE:
                heard = [rng.choice(CONTENT)]
            elif r < SUB_RATE + 0.03:
                heard = []
            elif r < SUB_RATE + 0.05:
                heard = [tok, rng.choice(FUNC)]
            if not heard:
                t += 0.3
            else:
                t = speak(heard, words, t)
            wi += 1
        t += rng.uniform(0.5, 1.5)
        if c == extra_chapter and bi == 6:
            t = speak(credits(70), words, t)
    end = start + t + 1.0
    chapters.append({"idx": c + 1, "start": start, "end": end})
    chapter_words[c + 1] = [[w[0], w[1], w[2], w[3]] for w in words]
    clock = end

# closing credits
words = []
t = speak(credits(50), words, 1.0)
chapters.append({"idx": NCH + 1, "start": clock, "end": clock + t + 1})
chapter_words[NCH + 1] = words

for s in (sections[0], sections[NCH + 1]):
    wi = 0
    for b in s["blocks"]:
        for _ in b["text"].split(" "):
            truth[(s["idx"], wi)] = None
            wi += 1

# ---------- run ----------
CMAP = {}
if os.environ.get("SYN_MAP"):   # e.g. "2:none,3:4-4"  (audio chapter: none | sec_start-sec_end)
    for item in os.environ["SYN_MAP"].split(","):
        k, v = item.split(":")
        CMAP[int(k)] = {"mode": "none"} if v == "none" else \
            {"mode": "range", "sec_start": int(v.split("-")[0]), "sec_end": int(v.split("-")[1])}
res = align_book(chapters, chapter_words, epub, CMAP)
print("summary:", res["summary"])
print("\nchapters:")
for c in res["chapters"]:
    print(f"  {c['idx']}: {c['status']:9s} ratio={c['ratio']}  epub {c['epub_start']} -> {c['epub_end']}")
print("\ngaps:")
for g in res["gaps"]:
    print(f"  {g['type']}: {g['words']} words at {g['time']:.1f}s  '{g['preview'][:50]}'")

errs, wrong_timed, missed, n_none = [], 0, 0, 0
for (sec, wi), true_t in truth.items():
    got = res["sections"][sec]["t"][wi]
    if true_t is None:
        n_none += 1
        if got is not None and sec not in (0, NCH + 1):  # headings near chapter start may interpolate
            pass
        if got is not None and sec in (0, NCH + 1):
            wrong_timed += 1
        continue
    if got is None:
        missed += 1
        continue
    errs.append(abs(got - true_t))

within = lambda x: sum(1 for e in errs if e <= x) / len(errs)
print(f"\nnarrated words: {len(errs) + missed}, given a time: {len(errs)}, left without time: {missed}")
print(f"timing error: median {statistics.median(errs):.3f}s, 95th pct {sorted(errs)[int(len(errs) * .95)]:.2f}s,"
      f" max {max(errs):.2f}s")
print(f"within 0.5s: {within(0.5):.1%}   within 1s: {within(1):.1%}   within 2s: {within(2):.1%}")
print(f"front/back matter words wrongly given a time: {wrong_timed} of {n_none}")
skipped = [v for (s, w), v in truth.items() if s == skip_chapter + 1 and v is None]
got_skipped = sum(1 for (s, w), v in truth.items()
                  if s == skip_chapter + 1 and v is None and res["sections"][s]["t"][w] is not None)
print(f"skipped-passage + heading words given a time anyway: {got_skipped} of {len(skipped)}")
print(f"aligner took {res['summary']['seconds']}s")

if "--debug" in sys.argv:
    bad = sorted(((abs(res["sections"][s]["t"][w] - v), s, w, v) for (s, w), v in truth.items()
                  if v is not None and res["sections"][s]["t"][w] is not None), reverse=True)[:12]
    for e, s, w, v in bad:
        toks = [x for b in sections[s]["blocks"] for x in b["text"].split(" ")]
        print(f"err {e:5.1f}s sec {s} word {w} kind {res['sections'][s]['k'][w]} true {v:.1f} "
              f"got {res['sections'][s]['t'][w]:.1f}  '{' '.join(toks[max(0, w - 3):w + 3])}'")
    s = skip_chapter + 1
    sk = [w for (ss, w), v in truth.items() if ss == s and v is None]
    print("skipped words kinds:", "".join(res["sections"][s]["k"][w] for w in sk))

if "--back" in sys.argv:
    b = NCH + 1
    toks = [x for bl in sections[b]["blocks"] for x in bl["text"].split(" ")]
    k = res["sections"][b]["k"]
    t = res["sections"][b]["t"]
    timed = [i for i, v in enumerate(t) if v is not None]
    print("back matter timed idx:", timed[:5], "...", timed[-3:], "kinds:", "".join(k[i] for i in timed))
    print("text:", " ".join(toks[timed[0]:timed[-1] + 1])[:300] if timed else "")
    last = sections[NCH]
    lt = [x for bl in last["blocks"] for x in bl["text"].split(" ")]
    print("last chapter ends:", " ".join(lt[-25:]))
    print("heard tail:", " ".join(w[2] for w in chapter_words[NCH][-25:]))
