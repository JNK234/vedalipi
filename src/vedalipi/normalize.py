# ABOUTME: Normalize stage: canonical Unicode for OCR'd Devanagari, including Vedic svara mark order.
# ABOUTME: NFC, no ZWJ/ZWNJ, base+virama/matra, then anusvara/visarga/candrabindu, then svaras; ग्ं/ग्ग्ं become ꣳ.

from __future__ import annotations

import re
import unicodedata

from .schema import Page

# Part of the normalize stage fingerprint: bump when normalize_text changes so cached norm.json is redone.
NORMALIZE_VERSION = "3"

_VIRAMA = "्"
_NUKTA = "़"

# Rank of a combining mark inside one akshara: lower sorts first. Stable sort
# keeps the original relative order of marks that share a rank.
_RANK_CLUSTER = 10  # virama, nukta, vowel signs (matras)
_RANK_NASAL = 20  # inverted candrabindu, candrabindu, anusvara, visarga
_RANK_SVARA = 30  # Vedic svara/accent marks
_RANK_OTHER = 15  # any other combining mark: keep original relative order

# ZWNJ and ZWJ only steer how a font joins letters; in Sanskrit they carry no text, and between a letter
# and its sign (श‍ृ) they make a valid akshara look like an invalid sign order.
_JOINERS = str.maketrans("", "", "\u200c\u200d")

# Books in Kannada, Telugu and other scripts without the sign print ꣳ (U+A8F3) as ಗ್ಂ / ಗ್ಗ್ಂ, గ్ం / గ్గ్ం,
# which convert to these Devanagari forms. Either one is a single ꣳ. A virama before the anusvara tells it
# apart from गं (ga with anusvara). Svaras after the form stay put and so sit on ꣳ; canonical mark order has
# already moved a svara read between virama and anusvara to after the anusvara.
_GUM = re.compile("ग्(?:ग्)?ं")
A8F3 = "\ua8f3"


def _mark_rank(ch: str) -> int:
    o = ord(ch)
    if ch == _VIRAMA or ch == _NUKTA:
        return _RANK_CLUSTER
    if 0x093E <= o <= 0x094C or 0x0945 <= o <= 0x0948 or 0x0955 <= o <= 0x0957 or 0x0962 <= o <= 0x0963:
        return _RANK_CLUSTER
    if o in (0x0900, 0x0901, 0x0902, 0x0903):
        return _RANK_NASAL
    if o in (0x0951, 0x0952, 0x0953, 0x0954) or 0x1CD0 <= o <= 0x1CE1 or 0xA8E0 <= o <= 0xA8F1:
        return _RANK_SVARA
    if not (0x0900 <= o <= 0x097F or 0x1CD0 <= o <= 0x1CFF or 0xA8E0 <= o <= 0xA8FF):
        # A mark from outside Devanagari (U+0331 read for an anudatta) is no part of the akshara's own signs:
        # it sits outermost with the svaras, in the order it was read. Put before an anusvara, it would leave
        # the anusvara without a base in the target script (a dotted circle).
        return _RANK_SVARA
    return _RANK_OTHER


def _is_combining(ch: str) -> bool:
    return unicodedata.category(ch) in ("Mn", "Mc", "Me")


def normalize_text(text: str) -> str:
    """Return canonical Devanagari: NFC without ZWJ/ZWNJ, then reorder marks within each akshara as
    consonant cluster + virama/matra, then anusvara/visarga/candrabindu, then svaras; last, ग्ं and ग्ग्ं
    become one ꣳ (U+A8F3), keeping any svara after them.

    Svara marks on digits (kampa, e.g. १॒॑) are allowed and kept. Other non-Devanagari text is untouched.
    """
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            run.sort(key=_mark_rank)  # stable: same-rank marks keep OCR order
            out.extend(run)
            run.clear()

    for ch in unicodedata.normalize("NFC", text).translate(_JOINERS):
        if _is_combining(ch):
            run.append(ch)
        else:
            flush()
            out.append(ch)
    flush()
    return _GUM.sub(A8F3, "".join(out))


def normalize_page(page: Page) -> Page:
    """Return a copy of page with normalize_text applied to every line and table cell of lang=="sa" blocks,
    and to every lang=="sa" image label."""
    out = page.model_copy(deep=True)
    for block in out.blocks:
        for label in block.labels or []:
            if label.is_sanskrit:
                label.text = normalize_text(label.text)
        if block.lang != "sa":
            continue
        block.lines = [normalize_text(line) for line in block.lines]
        if block.rows is not None:
            block.rows = [[normalize_text(cell) for cell in row] for row in block.rows]
    return out
