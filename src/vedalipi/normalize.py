# ABOUTME: Normalize stage: canonical Unicode for OCR'd Devanagari, including Vedic svara mark order.
# ABOUTME: NFC, then base+virama/matra, then anusvara/visarga/candrabindu, then svara marks (U+0951/U+0952/U+1CDA).

from __future__ import annotations

from .schema import Page


def normalize_text(text: str) -> str:
    """Return canonical Devanagari: NFC, then reorder marks within each akshara as
    consonant cluster + virama/matra, then anusvara/visarga/candrabindu, then svaras.

    Svara marks on digits (kampa, e.g. १॒॑) are allowed and kept. Non-Devanagari text is untouched.
    """
    raise NotImplementedError


def normalize_page(page: Page) -> Page:
    """Return a copy of page with normalize_text applied to every line and table cell of lang=="sa" blocks."""
    raise NotImplementedError
