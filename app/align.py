"""Align the transcript (what Whisper heard) to the EPUB (what is written).

Result: every EPUB display word gets a whole-file audio time in seconds, or None if it
was never narrated (front matter, skipped passages).

Steps, all forward-only so repeated phrases can't send the match backwards:
1. Normalize both sides into tokens (normalize.py).
2. Anchors: 7-word phrases that occur exactly once in the whole EPUB and once in the
   whole transcript. Keep the longest chain that moves forward in both (LIS).
   This also finds which EPUB text belongs to which audio chapter, with no chapter
   matching needed, and skips narrator announcements and credits on its own.
3. Between anchors: word-level edit-distance alignment (rapidfuzz). Big gaps are split
   with shorter unique anchors first.
4. Words Whisper misheard get times interpolated from their matched neighbours.
5. Per audio chapter: confidence = share of heard tokens that matched the EPUB.
"""
import bisect
import json
import time
from pathlib import Path

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from . import db
from .config import BOOKS_DIR
from .normalize import normalize_seq

ANCHOR_N = 7
SUB_NS = (5, 4, 3)
MAX_CELLS = 4_000_000     # biggest gap we hand straight to edit distance
INTERP_MAX_WORDS = 400    # longest unmatched run we interpolate over
INTERP_MIN_RATE = 0.06    # seconds per word; faster than this means the text wasn't narrated
INTERP_MAX_RATE = 1.5     # slower than this means a pause or music, not narration
GAP_REPORT_MIN = 30       # report unmatched runs at least this long
RARE_MAX = 25             # a word seen at most this often in the book counts as rare

OK_RATIO = 0.75
LOW_RATIO = 0.45
SHORT_CHAPTER_TOKENS = 300


# ---------------- loading ----------------

def epub_words(data):
    disp, words, linear = [], [], {}
    for s in data["sections"]:
        linear[s["idx"]] = s.get("linear", True)
        wi = 0
        for b in s["blocks"]:
            for w in b.get("text", "").split(" "):
                if w:
                    disp.append((s["idx"], wi))
                    words.append(w)
                    wi += 1
    return disp, words, linear


def section_len(epub, sec_idx):
    s = epub["sections"][sec_idx]
    return sum(len([w for w in b.get("text", "").split(" ") if w]) for b in s["blocks"])


def heard_words(chapters, chapter_words):
    """Concatenate chapter transcripts. Times become whole-file seconds."""
    words, starts, ends, chap = [], [], [], []
    for ch in chapters:
        if ch["idx"] not in chapter_words:
            continue
        for s, e, w, _prob in chapter_words[ch["idx"]]:
            if w:
                words.append(w)
                starts.append(ch["start"] + s)
                ends.append(ch["start"] + e)
                chap.append(ch["idx"])
    return words, starts, ends, chap


def token_times(src, starts, ends):
    """Spread each heard word's time span over the tokens it produced."""
    out = [0.0] * len(src)
    i = 0
    while i < len(src):
        j = i
        while j < len(src) and src[j] == src[i]:
            j += 1
        w = src[i]
        k = j - i
        for m in range(k):
            out[i + m] = starts[w] + (ends[w] - starts[w]) * m / k
        i = j
    return out


# ---------------- anchors ----------------

def unique_ngrams(ids, n, lo, hi):
    first, dup = {}, set()
    for i in range(lo, hi - n + 1):
        g = tuple(ids[i:i + n])
        if g in first:
            dup.add(g)
        else:
            first[g] = i
    return {g: p for g, p in first.items() if g not in dup}


def lis(points):
    """Longest chain of (t, e) points increasing in both. points must be sorted by t."""
    tails, tails_idx, prev = [], [], [-1] * len(points)
    for i, (_, e) in enumerate(points):
        k = bisect.bisect_left(tails, e)
        if k == len(tails):
            tails.append(e)
            tails_idx.append(i)
        else:
            tails[k] = e
            tails_idx[k] = i
        prev[i] = tails_idx[k - 1] if k else -1
    out, i = [], tails_idx[-1] if tails_idx else -1
    while i >= 0:
        out.append(points[i])
        i = prev[i]
    return out[::-1]


def find_anchors(ctx, ts, te, es, ee, n):
    T, E, allowed = ctx["T"], ctx["E"], ctx["allowed"]
    tu = unique_ngrams(T, n, ts, te)
    eu = unique_ngrams(E, n, es, ee)
    pts = sorted((tp, eu[g]) for g, tp in tu.items() if g in eu and allowed(tp, eu[g]))
    chain = lis(pts)
    blocks = []  # [t, e, length], non-overlapping, increasing
    for t, e in chain:
        if blocks:
            bt, be, bl = blocks[-1]
            if t - bt == e - be and t <= bt + bl:          # same diagonal, overlapping: extend
                blocks[-1][2] = max(bl, t - bt + n)
                continue
            if t < bt + bl or e < be + bl:                  # would overlap a different diagonal
                continue
        blocks.append([t, e, n])
    return blocks


# ---------------- gap alignment ----------------

def align_gap(ctx, ts, te, es, ee, pairs, depth=0):
    T, E, rare = ctx["T"], ctx["E"], ctx["rare"]
    lt, le = te - ts, ee - es
    if lt <= 0 or le <= 0:
        return
    if lt * le <= MAX_CELLS:
        balanced = min(lt, le) / max(lt, le) >= 0.5
        last_e = es - 1   # highest EPUB index paired so far in this gap
        for op in Levenshtein.opcodes(T[ts:te], E[es:ee]):
            run = op.src_end - op.src_start
            if op.tag == "equal":
                d = es + op.dest_start
                if run >= 3:
                    # A repeated phrase can match a later copy at equal cost. A narrator reads in
                    # order, so use the earliest unread copy instead.
                    seq = E[d:d + run]
                    for p in range(last_e + 1, d):
                        if E[p:p + run] == seq:
                            d = p
                            break
                # In a lopsided gap (credits vs front matter, or a skipped passage) common words
                # like "the" match by chance. There, trust runs of 3+ or rare words only.
                for k in range(run):
                    if balanced or run >= 3 or E[d + k] in rare:
                        pairs.append((ts + op.src_start + k, d + k, "M"))
                        last_e = d + k
            elif op.tag == "replace" and balanced and run == op.dest_end - op.dest_start and run <= 4:
                for k in range(run):  # same-length swap: almost always a misheard word
                    if es + op.dest_start + k > last_e:
                        pairs.append((ts + op.src_start + k, es + op.dest_start + k, "F"))
                        last_e = es + op.dest_start + k
        return
    for n in SUB_NS[depth:]:
        blocks = find_anchors(ctx, ts, te, es, ee, n)
        if blocks:
            break
    else:
        return  # big gap with nothing in common: leave it unaligned
    walk_blocks(ctx, ts, te, es, ee, blocks, pairs, depth + 1)


def walk_blocks(ctx, ts, te, es, ee, blocks, pairs, depth):
    ct, ce = ts, es
    for t, e, n in blocks:
        align_gap(ctx, ct, t, ce, e, pairs, depth)
        for k in range(n):
            pairs.append((t + k, e + k, "M"))
        ct, ce = t + n, e + n
    align_gap(ctx, ct, te, ce, ee, pairs, depth)


def drop_isolated(pairs, max_gap=15, min_cluster=4):
    """Real narration matches densely. A cluster with fewer than 4 exact matches and nothing
    else nearby is a coincidence (credits vs acknowledgements sharing a word), so drop it.
    Misheard-word swaps ("F") ride along but don't count as evidence."""
    out, cluster = [], []

    def flush():
        if sum(1 for c in cluster if c[2] == "M") >= min_cluster:
            out.extend(cluster)

    for p in pairs:
        if cluster and (p[1] - cluster[-1][1] > max_gap or p[0] - cluster[-1][0] > 2 * max_gap
                        or p[0] < cluster[-1][0]):
            flush()
            cluster = []
        cluster.append(p)
    flush()
    return out


def fuzzy_fill(e_tok, t_tok, e_lo, e_hi, t_lo, t_hi, min_score=72, window=6):
    """Greedy forward match of leftover EPUB tokens to leftover heard tokens by spelling.
    Catches near-misses like 'grey'/'gray' or a name heard slightly wrong. Returns [(t, e)]."""
    out, tp = [], t_lo
    for e in range(e_lo, e_hi):
        best, best_t = 0, None
        for t in range(tp, min(t_hi, tp + window)):
            sc = fuzz.ratio(e_tok[e], t_tok[t])
            if sc > best:
                best, best_t = sc, t
        if best_t is not None and best >= min_score and len(e_tok[e]) >= 3:
            out.append((best_t, e))
            tp = best_t + 1
    return out


# ---------------- main ----------------

def align_book(chapters, chapter_words, epub, cmap):
    """Pure function. chapters: [{idx,start,end}], chapter_words: {idx: [[s,e,word,p],...]} with
    times from chapter start, epub: parsed epub.json, cmap: {idx: {mode,sec_start,sec_end}}."""
    t_start = time.time()
    paths = chapter_words  # chapters that have a transcript
    disp, ewords, linear = epub_words(epub)
    hwords, hstart, hend, hchap = heard_words(chapters, chapter_words)
    if not hwords:
        raise ValueError("The transcript has no words")

    e_tok, e_src = normalize_seq(ewords)
    t_tok, t_src = normalize_seq(hwords)
    t_time = token_times(t_src, hstart, hend)
    t_chap = [hchap[i] for i in t_src]
    e_sec = [disp[i][0] for i in e_src]

    vocab = {}
    E = [vocab.setdefault(w, len(vocab)) for w in e_tok]
    T = [vocab.setdefault(w, len(vocab)) for w in t_tok]

    def allowed(tp, ep):
        m = cmap.get(t_chap[tp])
        if not m or m["mode"] == "auto":
            return True
        if m["mode"] == "none":
            return False
        return m["sec_start"] <= e_sec[ep] <= m["sec_end"]

    freq = {}
    for x in E:
        freq[x] = freq.get(x, 0) + 1
    ctx = {"T": T, "E": E, "allowed": allowed,
           "rare": {x for x, c in freq.items() if c <= RARE_MAX}}

    # 1-3: anchors, then gaps
    pairs = []
    blocks = find_anchors(ctx, 0, len(T), 0, len(E), ANCHOR_N)
    walk_blocks(ctx, 0, len(T), 0, len(E), blocks, pairs, 0)
    pairs = [p for p in pairs if allowed(p[0], p[1])]
    pairs.sort(key=lambda p: p[1])
    pairs = drop_isolated(pairs)
    # A chapter that barely matches (opening/closing credits) only produces chance matches.
    chap_tokens, chap_hits = {}, {}
    for c in t_chap:
        chap_tokens[c] = chap_tokens.get(c, 0) + 1
    for t, _e, k in pairs:
        if k == "M":
            chap_hits[t_chap[t]] = chap_hits.get(t_chap[t], 0) + 1
    weak = {c for c, n in chap_tokens.items() if chap_hits.get(c, 0) / n < 0.3 and n < SHORT_CHAPTER_TOKENS}
    pairs = [p for p in pairs if t_chap[p[0]] not in weak]

    # token times on the EPUB side, kept strictly forward in time
    e_time = [None] * len(E)
    e_kind = ["N"] * len(E)
    e_t = [None] * len(E)       # which heard token each EPUB token is paired with
    paired_t = set()
    last_t = -1
    for t, e, k in pairs:
        if t <= last_t:
            continue
        e_time[e], e_kind[e], e_t[e] = t_time[t], k, t
        last_t = t
        paired_t.add(t)

    # 4a: spelling-based match for leftovers between paired neighbours
    matched_e = [i for i, v in enumerate(e_time) if v is not None]
    for a, b in zip(matched_e, matched_e[1:]):
        n, m = b - a - 1, e_t[b] - e_t[a] - 1
        if n > 0 and m > 0 and n * m <= 40000:
            for t, e in fuzzy_fill(e_tok, t_tok, a + 1, b, e_t[a] + 1, e_t[b]):
                if allowed(t, e):
                    e_time[e], e_kind[e], e_t[e] = t_time[t], "F", t
                    paired_t.add(t)

    # 4b: interpolate short unmatched runs, report long ones
    gaps = []
    matched_e = [i for i, v in enumerate(e_time) if v is not None]
    for a, b in zip(matched_e, matched_e[1:]):
        n = b - a - 1
        if n <= 0:
            continue
        m = e_t[b] - e_t[a] - 1   # heard tokens between the two neighbours
        span = e_time[b] - e_time[a]
        rate = span / (n + 1)
        fits = n <= 2 * m + 5      # roughly as many words heard as written
        if n <= 3 and rate <= 3 * INTERP_MAX_RATE:
            ok = True
        else:
            ok = fits and n <= INTERP_MAX_WORDS and INTERP_MIN_RATE <= rate <= INTERP_MAX_RATE
        if ok:
            for i in range(a + 1, b):
                e_time[i] = e_time[a] + rate * (i - a)
                e_kind[i] = "I"
        elif n <= 3:
            # Short run next to audio that isn't in the book. Put it on the side whose nearby
            # heard words look more like it; on a tie, the right side (extra audio such as an
            # announcement usually comes before text, not after).
            run_toks = [x for x in e_tok[a + 1:b] if len(x) >= 3]  # "on", "a" match anything
            left = t_tok[e_t[a] + 1:e_t[a] + 1 + n + 2]
            right = t_tok[max(e_t[a] + 1, e_t[b] - n - 2):e_t[b]]
            score = lambda side: max((fuzz.ratio(x, y) for x in run_toks for y in side), default=0)
            sl, sr = score(left), score(right)
            if max(sl, sr) < 72:
                continue  # no evidence for either side: leave untimed, its line still highlights
            if sl > sr:
                for i in range(a + 1, b):
                    e_time[i] = e_time[a] + 0.35 * (i - a)
                    e_kind[i] = "I"
            else:
                for i in range(a + 1, b):
                    e_time[i] = e_time[b] - 0.35 * (b - i)
                    e_kind[i] = "I"
        elif n >= GAP_REPORT_MIN and linear.get(e_sec[a + 1], True):
            d0, d1 = disp[e_src[a + 1]], disp[e_src[b - 1]]
            whole = d0[0] == d1[0] and d0[1] == 0 and d1[1] == section_len(epub, d0[0]) - 1
            gaps.append({
                "type": "text_not_heard", "whole_section": whole,
                "words": e_src[b - 1] - e_src[a + 1] + 1,
                "time": round(e_time[a], 2), "sec": d0[0], "word_start": d0[1],
                "sec_end": d1[0], "word_end": d1[1],
                "preview": " ".join(ewords[e_src[a + 1]:e_src[a + 1] + 14]),
            })

    # audio that has no text, in the middle of a chapter (start/end announcements are expected)
    by_chap = {}
    for i, c in enumerate(t_chap):
        by_chap.setdefault(c, []).append(i)
    for c, toks in by_chap.items():
        hits = [i for i in toks if i in paired_t]
        if len(hits) < 2:
            continue
        run_start = None
        for i in range(hits[0], hits[-1] + 1):
            if i not in paired_t:
                run_start = i if run_start is None else run_start
            elif run_start is not None:
                if i - run_start >= GAP_REPORT_MIN:
                    gaps.append({
                        "type": "audio_not_in_text", "chapter": c, "words": i - run_start,
                        "time": round(t_time[run_start], 2), "time_end": round(t_time[i - 1], 2),
                        "preview": " ".join(hwords[t_src[run_start]:t_src[run_start] + 14]),
                    })
                run_start = None

    # display words: time and kind of their first timed token
    nd = len(ewords)
    d_time, d_kind = [None] * nd, ["P"] * nd
    for ti, d in enumerate(e_src):
        if d_kind[d] == "P" or (d_time[d] is None and e_time[ti] is not None):
            d_time[d] = e_time[ti]
            d_kind[d] = e_kind[ti]

    sections = {}
    for d, (sec, _w) in enumerate(disp):
        s = sections.setdefault(sec, {"t": [], "k": []})
        s["t"].append(None if d_time[d] is None else round(d_time[d], 2))
        s["k"].append(d_kind[d])
    for s in sections.values():
        s["k"] = "".join(s["k"])

    # 5: chapter stats
    chap_out, needs_review = [], False
    for ch in chapters:
        idx = ch["idx"]
        mode = (cmap.get(idx) or {}).get("mode", "auto")
        toks = by_chap.get(idx, [])
        entry = {"idx": idx, "mode": mode, "tokens": len(toks), "matched": 0, "ratio": None,
                 "status": "not_transcribed" if idx not in paths else None,
                 "epub_start": None, "epub_end": None}
        if mode != "auto":
            entry["sec_start"] = cmap[idx].get("sec_start")
            entry["sec_end"] = cmap[idx].get("sec_end")
        if idx in paths:
            hit = [i for i in toks if i in paired_t]
            entry["matched"] = len(hit)
            ratio = len(hit) / len(toks) if toks else 0.0
            entry["ratio"] = round(ratio, 3)
            if mode == "none":
                entry["status"] = "excluded"
            elif ratio >= OK_RATIO:
                entry["status"] = "ok"
            elif ratio < 0.3 and len(toks) < SHORT_CHAPTER_TOKENS:
                entry["status"] = "no_text"   # credits, intro music, short announcements
            elif ratio >= LOW_RATIO:
                entry["status"] = "low"
            else:
                entry["status"] = "unmatched"
            if entry["status"] in ("low", "unmatched"):
                needs_review = True
            # EPUB range covered by this chapter
            es = sorted(e for t, e, _ in pairs if t_chap[t] == idx and t in paired_t)
            if es:
                entry["epub_start"] = list(disp[e_src[es[0]]])
                entry["epub_end"] = list(disp[e_src[es[-1]]])
        chap_out.append(entry)

    timed = sum(1 for v in d_time if v is not None)
    content = sum(1 for k in d_kind if k != "P")
    summary = {
        "words_total": nd, "words_timed": timed,
        "words_matched": sum(1 for k in d_kind if k in "MF"),
        "timed_ratio": round(timed / content, 4) if content else 0,
        "chapters_ok": sum(1 for c in chap_out if c["status"] == "ok"),
        "chapters_review": sum(1 for c in chap_out if c["status"] in ("low", "unmatched")),
        "chapters_no_text": sum(1 for c in chap_out if c["status"] in ("no_text", "excluded")),
        "chapters_missing": sum(1 for c in chap_out if c["status"] == "not_transcribed"),
        "gaps": len(gaps), "anchors": len(blocks), "seconds": round(time.time() - t_start, 2),
    }
    result = {"version": 1, "created": time.time(), "summary": summary, "needs_review": needs_review,
              "chapters": chap_out, "gaps": gaps, "sections": sections}
    return result


def run(book_id):
    """Load a book's transcripts, align, save alignment.json and update the book status."""
    with db.db() as con:
        chapters = [dict(r) for r in con.execute(
            'SELECT idx,title,start,"end" FROM chapters WHERE book_id=? ORDER BY idx', (book_id,))]
        paths = {r["idx"]: r["path"] for r in con.execute(
            "SELECT idx,path FROM transcripts WHERE book_id=?", (book_id,))}
        cmap = {r["idx"]: dict(r) for r in con.execute(
            "SELECT idx,mode,sec_start,sec_end FROM chapter_map WHERE book_id=?", (book_id,))}
    if not paths:
        raise ValueError("Nothing transcribed yet")
    chapter_words = {i: json.loads(Path(p).read_text(encoding="utf-8"))["words"]
                     for i, p in paths.items() if Path(p).exists()}
    epub = json.loads((BOOKS_DIR / book_id / "epub.json").read_text(encoding="utf-8"))
    result = align_book(chapters, chapter_words, epub, cmap)
    summary = result["summary"]

    out = BOOKS_DIR / book_id / "alignment.json"
    tmp = out.with_suffix(".json.part")
    tmp.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    tmp.replace(out)

    complete = summary["chapters_missing"] == 0
    status = ("needs_review" if result["needs_review"] else "ready") if complete else None
    with db.db() as con:
        if status:
            con.execute("UPDATE books SET status=?, status_note=NULL WHERE id=?", (status, book_id))
    return summary
