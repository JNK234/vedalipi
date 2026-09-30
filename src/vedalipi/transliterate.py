# ABOUTME: Transliterate stage: converts the Devanagari master into a target Indic script (vidyut, sanscript fallback).
# ABOUTME: Every sa line is round-tripped back to Devanagari; mismatches get "roundtrip_fail" but never stop the run.

from __future__ import annotations

from .schema import Master

# U+A8F3 (candrabindu virama / "gm") replacement per target script; Noto lacks the glyph.
DEFAULT_A8F3 = {"kannada": "ಗ್ಂ", "telugu": "గ్ం"}


def supported_scripts() -> list[str]:
    """Lowercase target script names accepted by --script (kannada, telugu, and whatever vidyut supports)."""
    raise NotImplementedError


def transliterate_text(text: str, script: str, *, a8f3: str | None = None) -> str:
    """Convert one Devanagari string to `script`, preserving U+0951/U+0952/U+1CDA exactly.

    U+A8F3 becomes `a8f3` (default DEFAULT_A8F3[script] when present). Non-Devanagari characters pass through.
    """
    raise NotImplementedError


def roundtrip_ok(devanagari: str, converted: str, script: str) -> bool:
    """Convert `converted` back to Devanagari and compare with `devanagari` after normalize_text."""
    raise NotImplementedError


def transliterate_master(master: Master, script: str, *, a8f3: str | None = None) -> Master:
    """Return a copy of master with every lang=="sa" line and table cell in `script`.

    Other languages pass through unchanged. Blocks with any failed round trip get "roundtrip_fail".
    The input master is not modified.
    """
    raise NotImplementedError
