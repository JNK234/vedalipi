# ABOUTME: Font and language tables for the renderer: one row per script, one row per foreign language code.
# ABOUTME: Adding a script is one line in SCRIPTS; every font list ends in a fallback chain over all vendored fonts.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScriptFont:
    family: str  # font family name as Typst sees it (vendored in fonts/)
    lang: str  # ISO 639 code handed to Typst for OpenType language-system shaping
    scale: float = 1.0  # relative size so mixed scripts look optically even on one page


# Target scripts (lowercase names as passed to --script). Noto Serif Kannada covers U+0951, U+0952 and
# U+1CDA on Kannada clusters with no dotted circles (checked with HarfBuzz in tests/test_render.py).
SCRIPTS: dict[str, ScriptFont] = {
    "kannada": ScriptFont("Noto Serif Kannada", "kn"),
    "telugu": ScriptFont("Noto Sans Telugu", "te"),
    "devanagari": ScriptFont("Noto Serif Devanagari", "sa"),
    "tamil": ScriptFont("Noto Sans Tamil", "ta", 0.92),
    "latin": ScriptFont("Noto Serif", "en", 0.88),
}

# Block `lang` codes -> script whose font renders them (foreign text is never transliterated).
LANG_SCRIPT: dict[str, str] = {
    "sa": "devanagari",
    "hi": "devanagari",
    "mr": "devanagari",
    "kn": "kannada",
    "te": "telugu",
    "ta": "tamil",
    "en": "latin",
}

# Tried after the primary font for characters it lacks (e.g. Latin digits inside Kannada text).
FALLBACK_ORDER = ["Noto Serif", "Noto Serif Devanagari", "Noto Serif Kannada", "Noto Sans Telugu", "Noto Sans Tamil"]


def script_font(script: str) -> ScriptFont:
    """Font for a target script; unknown scripts get Noto Serif plus the fallback chain."""
    return SCRIPTS.get(script.lower(), ScriptFont("Noto Serif", "en", 0.88))


def font_chain(primary: str) -> list[str]:
    """Primary family first, then every other vendored family as per-character fallback."""
    return [primary, *(f for f in FALLBACK_ORDER if f != primary)]


def lang_font(lang: str) -> ScriptFont:
    """Font for a block language code (foreign blocks); unknown codes fall back to Noto Serif."""
    return SCRIPTS[LANG_SCRIPT.get(lang.lower(), "latin")]
