"""Turn display words (EPUB) and heard words (Whisper) into comparable tokens.

Both sides go through the same rules, so "1984" and "nineteen eighty-four" both become
["nineteen", "eighty", "four"], "Chapter IV" and "chapter four" match, and
"don't" / "dont" / "Don’t" are the same token.
One display word can give zero tokens (a lone dash) or several ("well-known" -> 2).
"""
import re
import unicodedata

from num2words import num2words

QUOTES = str.maketrans({"’": "'", "‘": "'", "‛": "'", "´": "'", "`": "'",
                        "“": '"', "”": '"', "„": '"', "«": '"', "»": '"', "­": None})
SPLIT_RE = re.compile(r"[-‐‑‒–—―/_…~]+|\.{2,}")
STRIP_CHARS = "\"'()[]{}<>.,;:!?*•·¡¿#|"
ORDINAL_RE = re.compile(r"^(\d+)(st|nd|rd|th)$")
NUMBER_RE = re.compile(r"^\d{1,3}(,\d{3})+$|^\d+$")
DECIMAL_RE = re.compile(r"^\d+\.\d+$")
ROMAN_RE = re.compile(r"^M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
ROMAN_VAL = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
ROMAN_CONTEXT = {"chapter", "part", "book", "volume", "act", "scene", "section"}
NON_WORD = re.compile(r"[^\w]+")


def _roman(s):
    total = 0
    for i, ch in enumerate(s):
        v = ROMAN_VAL[ch]
        total += -v if i + 1 < len(s) and ROMAN_VAL[s[i + 1]] > v else v
    return total


def _words(n_text):
    return [w for w in NON_WORD.sub(" ", n_text.replace("-", " ")).split() if w != "and"]


def _number_words(digits):
    n = int(digits.replace(",", ""))
    if 1100 <= n <= 1999 and "," not in digits and len(digits) == 4:
        return _words(num2words(n, to="year"))
    if n > 10**12:
        return list(digits)
    return _words(num2words(n))


def normalize_word(raw, prev_word=""):
    """Normalize one display/heard word. prev_word is the previous normalized token (for roman numerals)."""
    t = unicodedata.normalize("NFKC", raw).translate(QUOTES)
    t = t.replace("&", " and ").replace("%", " percent ")
    out = []
    for part in SPLIT_RE.split(t):
        for piece in part.split():
            orig = piece.strip(STRIP_CHARS + "$£€")
            if not orig:
                continue
            low = orig.lower()
            m = ORDINAL_RE.match(low)
            if m:
                out += _words(num2words(int(m[1]), to="ordinal"))
                continue
            if NUMBER_RE.match(low):
                out += _number_words(low)
                continue
            if DECIMAL_RE.match(low):
                a, b = low.split(".")
                out += _number_words(a) + ["point"] + [_words(num2words(int(d)))[0] for d in b]
                continue
            context = (out[-1] if out else prev_word) in ROMAN_CONTEXT
            if orig.isupper() and ROMAN_RE.match(orig):
                val = _roman(orig)
                # With "chapter"/"part" before it, any numeral counts. Without, only short numerals
                # up to 100, so shouted words like "MIX" stay words and the pronoun "I" stays "i".
                if context or (len(orig) >= 2 and val <= 100):
                    out += _words(num2words(val))
                    continue
            word = NON_WORD.sub("", low.replace("'", ""))
            if word:
                out.append(word)
    return out


def normalize_seq(words):
    """Normalize a list of words. Returns (tokens, src) where src[i] is the index in `words`
    that token i came from."""
    tokens, src = [], []
    prev = ""
    for i, w in enumerate(words):
        toks = normalize_word(w, prev)
        tokens += toks
        src += [i] * len(toks)
        if toks:
            prev = toks[-1]
    return tokens, src
