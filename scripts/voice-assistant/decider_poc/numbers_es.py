"""Spanish number, percentage, temperature and duration parsing for the Kivi decider PoC.

The decider model only picks the kind, the intent and the target; every numeric slot is read
from the transcript by this module (no model involved). Inputs are Whisper transcripts, so both
digits ("al 30 %", "21,5 grados") and words ("veintiuno y medio", "una hora y media") occur.

Pure functions, no dependencies beyond the standard library.
"""
import re
import unicodedata

UNITS = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
    "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14,
    "quince": 15, "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20,
    "veintiuno": 21, "veintiun": 21, "veintiuna": 21, "veintidos": 22, "veintitres": 23,
    "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26, "veintisiete": 27, "veintiocho": 28,
    "veintinueve": 29,
}
TENS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80,
        "noventa": 90}
HUNDRED = {"cien": 100, "ciento": 100}
# "un"/"una" are articles far more often than numbers; they count only before a duration unit.
ARTICLE_LIKE = {"un", "una"}

WORDS_0_29 = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez",
              "once", "doce", "trece", "catorce", "quince", "dieciséis", "diecisiete", "dieciocho",
              "diecinueve", "veinte", "veintiuno", "veintidós", "veintitrés", "veinticuatro", "veinticinco",
              "veintiséis", "veintisiete", "veintiocho", "veintinueve"]
TENS_WORDS = {30: "treinta", 40: "cuarenta", 50: "cincuenta", 60: "sesenta", 70: "setenta", 80: "ochenta",
              90: "noventa"}


def fold(text):
    """Lower case, accents removed, punctuation except , . % ° turned into spaces."""
    t = unicodedata.normalize("NFKD", str(text).lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"(\d)\s*([.,])\s*(\d)", r"\1\2\3", t)
    t = re.sub(r"[^\w%°.,]+", " ", t)
    t = re.sub(r"(?<!\d)[.,]|[.,](?!\d)", " ", t)
    t = t.replace("%", " % ").replace("°", " ° ")
    return re.sub(r"\s+", " ", t).strip()


def words_es(n):
    """Integer 0..100 (and halves) as Spanish words, the way a person says them."""
    half = abs(n - int(n) - 0.5) < 1e-9
    n = int(n)
    if n < 30:
        w = WORDS_0_29[n]
    elif n == 100:
        w = "cien"
    else:
        t, u = divmod(n, 10)
        w = TENS_WORDS[t * 10] + ("" if u == 0 else " y " + WORDS_0_29[u])
    return w + (" y medio" if half else "")


def tokens_to_numbers(tokens, allow_articles=False):
    """Scan folded tokens; return a list of (start, end, value) for every number found.
    Handles digits (with , or . decimals), 0-29 words, tens + "y" + unit, cien/ciento + rest,
    and a trailing "y medio"/"y media" (+0.5)."""
    out, i, n = [], 0, len(tokens)
    while i < n:
        tok = tokens[i]
        start, val = i, None
        if re.fullmatch(r"\d+([.,]\d+)?", tok):
            val = float(tok.replace(",", "."))
            i += 1
        elif tok in HUNDRED and not (i > 0 and tokens[i - 1] == "por"):  # "por ciento" is a unit
            val, i = 100.0, i + 1
            rest, j = _words_below_100(tokens, i)
            if rest is not None:
                val, i = 100.0 + rest, j
        elif tok in TENS or (tok in UNITS and (allow_articles or tok not in ARTICLE_LIKE)):
            val, i = _words_below_100(tokens, i)
        else:
            i += 1
            continue
        if val is None:
            i = start + 1
            continue
        if i + 1 < n and tokens[i] == "y" and tokens[i + 1] in ("medio", "media"):
            val += 0.5
            i += 2
        out.append((start, i, val))
    return out


def _words_below_100(tokens, i):
    if i >= len(tokens):
        return None, i
    tok = tokens[i]
    if tok in TENS:
        val = TENS[tok]
        if i + 2 < len(tokens) and tokens[i + 1] == "y" and tokens[i + 2] in UNITS and UNITS[tokens[i + 2]] < 10:
            return float(val + UNITS[tokens[i + 2]]), i + 3
        return float(val), i + 1
    if tok in UNITS:
        return float(UNITS[tok]), i + 1
    return None, i


def parse_number(text):
    """First number in the text (float) or None. Articles "un"/"una" are not numbers here."""
    nums = tokens_to_numbers(fold(text).split())
    return nums[0][2] if nums else None


MAX_WORDS = ("maximo", "tope", "todo lo que da", "a tope")
HALF_WORDS = ("la mitad", "a medias", "medio abierta", "medio cerrada", "a media altura")


def parse_percent(text):
    """A 0..100 level for brightness, volume, fan speed or cover position, or None.
    "al máximo"/"a tope" -> 100, "a la mitad" -> 50, otherwise the last number in the text
    (the level is said after the device: "pon la luz 2 del pasillo al 40" is rare; names with digits
    are not used in the inventories)."""
    f = fold(text)
    if any(w in f for w in MAX_WORDS):
        return 100
    if any(w in f for w in HALF_WORDS):
        return 50
    nums = tokens_to_numbers(f.split())
    if not nums:
        return None
    v = nums[-1][2]
    if v != int(v) or not 0 <= v <= 100:
        return None
    return int(v)


def parse_temperature(text):
    """Target temperature in degrees (float, halves allowed) or None; plausible range 5..35."""
    nums = tokens_to_numbers(fold(text).split())
    if not nums:
        return None
    v = nums[-1][2]
    return float(v) if 5 <= v <= 35 else None


DURATION_UNITS = {"hora": 3600, "horas": 3600, "minuto": 60, "minutos": 60, "min": 60, "segundo": 1,
                  "segundos": 1, "seg": 1}


def parse_duration(text):
    """Timer duration in seconds or None. Examples: "diez minutos" 600, "una hora y media" 5400,
    "media hora" 1800, "un cuarto de hora" 900, "hora y cuarto" 4500, "2 minutos y 30 segundos" 150,
    "un minuto y medio" 90, "5 min" 300."""
    toks = fold(text).split()
    s = " ".join(toks)
    total, found = 0, False
    if re.search(r"\b(un )?cuarto de hora\b", s):
        total, found = 900, True
        s = re.sub(r"\b(un )?cuarto de hora\b", " ", s)
    if re.search(r"\bmedia hora\b", s) and not re.search(r"\by media hora\b", s):
        total, found = total + 1800, True
        s = re.sub(r"\bmedia hora\b", " ", s)
    if re.search(r"\b(una )?hora y cuarto\b", s):
        total, found = total + 4500, True
        s = re.sub(r"\b(una )?hora y cuarto\b", " ", s)
    toks = s.split()
    nums = tokens_to_numbers(toks, allow_articles=True)
    for start, end, val in nums:
        unit_i = end
        if unit_i < len(toks) and toks[unit_i] in DURATION_UNITS:
            unit = DURATION_UNITS[toks[unit_i]]
            extra = 0.0
            if unit_i + 2 < len(toks) and toks[unit_i + 1] == "y" and toks[unit_i + 2] in ("medio", "media"):
                extra = 0.5
            total += int(round((val + extra) * unit))
            found = True
    # "una hora" written without a number ("hora y media" after an elided "una")
    if not found and re.search(r"\bhora y media\b", s):
        return 5400
    return total if found and total > 0 else None


VALUE_PARSERS = {
    "percent": parse_percent,
    "temperature": parse_temperature,
    "duration": parse_duration,
}


def parse_value(kind, text):
    """Dispatch by value kind (None means the intent takes no value)."""
    if kind is None:
        return None
    return VALUE_PARSERS[kind](text)
