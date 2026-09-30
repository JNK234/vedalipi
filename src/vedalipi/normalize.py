# ABOUTME: Normalize stage: canonical Unicode for OCR'd Devanagari, including Vedic svara mark order.
# ABOUTME: NFC, then base+virama/matra, then anusvara/visarga/candrabindu, then svara marks (U+0951/U+0952/U+1CDA).

from __future__ import annotations

import unicodedata

from .schema import Page

# Part of the normalize stage fingerprint: bump when normalize_text changes so cached norm.json is redone.
NORMALIZE_VERSION = "1"

_VIRAMA = "्"
_NUKTA = "़"

# Rank of a combining mark inside one akshara: lower sorts first. Stable sort
# keeps the original relative order of marks that share a rank.
_RANK_CLUSTER = 10  # virama, nukta, vowel signs (matras)
_RANK_NASAL = 20  # inverted candrabindu, candrabindu, anusvara, visarga
_RANK_SVARA = 30  # Vedic svara/accent marks
_RANK_OTHER = 15  # any other combining mark: keep original relative order


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
    return _RANK_OTHER


def _is_combining(ch: str) -> bool:
    return unicodedata.category(ch) in ("Mn", "Mc", "Me")


def normalize_text(text: str) -> str:
    """Return canonical Devanagari: NFC, then reorder marks within each akshara as
    consonant cluster + virama/matra, then anusvara/visarga/candrabindu, then svaras.

    Svara marks on digits (kampa, e.g. १॒॑) are allowed and kept. Non-Devanagari text is untouched.
    """
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            run.sort(key=_mark_rank)  # stable: same-rank marks keep OCR order
            out.extend(run)
            run.clear()

    for ch in unicodedata.normalize("NFC", text):
        if _is_combining(ch):
            run.append(ch)
        else:
            flush()
            out.append(ch)
    flush()
    return "".join(out)


def normalize_page(page: Page) -> Page:
    """Return a copy of page with normalize_text applied to every line and table cell of lang=="sa" blocks."""
    out = page.model_copy(deep=True)
    for block in out.blocks:
        if block.lang != "sa":
            continue
        block.lines = [normalize_text(line) for line in block.lines]
        if block.rows is not None:
            block.rows = [[normalize_text(cell) for cell in row] for row in block.rows]
    return out
