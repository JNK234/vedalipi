# ABOUTME: Transliterate stage: converts the Devanagari master into a target Indic script (vidyut, sanscript fallback).
# ABOUTME: Every sa line is round-tripped back to Devanagari; mismatches get "roundtrip_fail" but never stop the run.

from __future__ import annotations

import re
import unicodedata

from functools import lru_cache

from .normalize import normalize_text
from .schema import FLAG_ROUNDTRIP_FAIL, Block, Master, Page

# U+A8F3 (Vedic candrabindu-virama) replacement per target script; Noto lacks the glyph.
A8F3 = "ꣳ"
DEFAULT_A8F3 = {"kannada": "ಗ್ಂ", "telugu": "గ్ం"}

# Devanagari is the master language; every conversion starts from it.
DEVA = "devanagari"

# Roman schemes (iast, iso15919, ...) have no svara letters of their own, and Latin fonts lack the Devanagari
# marks: each Vedic mark becomes the Latin combining mark that looks like it. vidyut writes svarita as U+032D.
ROMAN_SVARA = {
    "\u0951": "\u030d",  # svarita: vertical line above
    "\u032d": "\u030d",
    "\u0952": "\u0331",  # anudatta: macron below
    "\u1cda": "\u030e",  # double svarita: double vertical line above
}
_ROMAN_BACK = {"\u030d": "\u0951", "\u032d": "\u0951", "\u0331": "\u0952", "\u030e": "\u1cda"}


def _roman_marks(text: str, table: dict[str, str]) -> str:
    """Swap accent marks in roman text. Decomposed first: vidyut can fuse a vowel and its svarita into one
    precomposed letter (u + U+032D -> ṷ), which a plain character swap would miss. Each swapped mark is
    moved after the letter's own marks (the macron of ā, the dot of ṛ), so the letter recomposes and the
    accent sits outside it: ā + svarita, never a + svarita + macron."""
    out: list[str] = []
    accents: list[str] = []
    for ch in unicodedata.normalize("NFD", text):
        if ch in table:
            accents.append(table[ch])
        elif unicodedata.combining(ch):
            out.append(ch)
        else:
            out.extend(accents)
            accents.clear()
            out.append(ch)
    out.extend(accents)
    return unicodedata.normalize("NFC", "".join(out))


@lru_cache(maxsize=1)
def _vidyut_schemes() -> dict[str, object]:
    """Map lowercase script names to vidyut lipi Schemes, or {} when vidyut is unavailable."""
    try:
        from vidyut import lipi
    except ImportError:
        return {}
    return {str(s).lower(): s for s in lipi.Scheme.choices()}


@lru_cache(maxsize=1)
def _sanscript_schemes() -> dict[str, object]:
    """Map lowercase script names to sanscript scheme ids, the fallback engine."""
    from indic_transliteration import sanscript

    return dict(sanscript.SCHEMES)


def supported_scripts() -> list[str]:
    """Lowercase target script names accepted by --script (kannada, telugu, and whatever vidyut supports)."""
    names = _vidyut_schemes() or _sanscript_schemes()
    return sorted(names)


def _convert(text: str, src: str, dst: str) -> str:
    """Transliterate text from src to dst, using vidyut and falling back to sanscript."""
    schemes = _vidyut_schemes()
    if src in schemes and dst in schemes:
        from vidyut import lipi

        try:
            return lipi.transliterate(text, schemes[src], schemes[dst])
        except Exception:
            pass
    fallback = _sanscript_schemes()
    if src in fallback and dst in fallback:
        from indic_transliteration import sanscript

        return sanscript.transliterate(text, src, dst)
    raise ValueError(f"unsupported script conversion: {src} -> {dst}")


def _a8f3_for(script: str, a8f3: str | None) -> str | None:
    """Pick the U+A8F3 stand-in: the explicit override, else the per-script default, else None."""
    return a8f3 if a8f3 is not None else DEFAULT_A8F3.get(script)


def _render_line(text: str, script: str, a8f3: str | None) -> tuple[str, str]:
    """Return (raw conversion, display conversion with the U+A8F3 stand-in applied).

    Round trips are checked against the raw form so the deliberate U+A8F3 substitution is not
    mistaken for a lossy conversion.
    """
    if is_roman(script):
        # vidyut writes dandas as "." and "..", which run into fill-in dots; roman books print | and ||.
        text = text.replace("॥", "||").replace("।", "|")
    raw = _convert(text, DEVA, script)
    out = raw.replace(A8F3, a8f3) if a8f3 is not None else raw
    if is_roman(script):
        out = _roman_marks(out, ROMAN_SVARA)
    return raw, out


@lru_cache(maxsize=None)
def is_roman(script: str) -> bool:
    """True when `script` writes Sanskrit in Latin letters (iast, iso15919, harvardkyoto, ...)."""
    sample = _convert("कृष्ण", DEVA, script)
    return all(ord(ch) < 0x0370 or 0x1E00 <= ord(ch) <= 0x1EFF for ch in sample)


def transliterate_text(text: str, script: str, *, a8f3: str | None = None) -> str:
    """Convert one Devanagari string to `script`, preserving U+0951/U+0952/U+1CDA exactly.

    U+A8F3 becomes `a8f3` (default DEFAULT_A8F3[script] when present). Non-Devanagari characters pass through.
    """
    _, out = _render_line(text, script, _a8f3_for(script, a8f3))
    return out


def page_to_devanagari(page: Page, script: str) -> Page:
    """Return a copy of an OCR'd page from a `script` book with its Sanskrit (lines, cells, labels) in Devanagari.

    Text in other languages (e.g. Telugu instructions) keeps its own script. Svara marks pass through unchanged.
    """
    out = page.model_copy(deep=True)
    for block in out.blocks:
        if block.is_sanskrit:
            block.lines = [_convert(line, script, DEVA) for line in block.lines]
            if block.rows is not None:
                block.rows = [[_convert(c, script, DEVA) for c in row] for row in block.rows]
        for label in block.labels or []:
            if label.is_sanskrit:
                label.text = _convert(label.text, script, DEVA)
    return out


def _roundtrip_key(text: str, script: str = DEVA) -> str:
    # The one sanctioned equivalence: targets without an om sign write ॐ as ओं (ಓಂ, ఓం), which reads back as ओं.
    key = normalize_text(text).replace("ॐ", "ओं")
    # Roman schemes write the Vedic anusvara ꣳ as m̐, which reads back as candrabindu.
    return key.replace(A8F3, "ँ") if is_roman(script) else key


def roundtrip_back(converted: str, script: str) -> str:
    """`converted` read back into Devanagari (a roman display form's Latin svara marks read as Vedic marks)."""
    if not is_roman(script):
        return _convert(converted, script, DEVA)
    # Roman text: | and || are the dandas, and dots are fill-in blanks that vidyut would read as dandas.
    # Typographic quotes are quotes: vidyut writes the avagraha as ASCII "'", but reads ’ as one too.
    keep = {".": "\u2024", "\u2018": "\ue000", "\u2019": "\ue001"}
    converted = _roman_marks(converted, _ROMAN_BACK).translate(str.maketrans(keep))
    back = _convert(converted, script, DEVA).translate(str.maketrans({v: k for k, v in keep.items()}))
    return back.replace("||", "॥").replace("|", "।")


def roundtrip_ok(devanagari: str, converted: str, script: str) -> bool:
    """Convert `converted` back to Devanagari and compare it with `devanagari` directly (after normalize_text).

    The comparison is in Devanagari, never after a second forward conversion: a character the forward
    conversion drops would vanish from both sides of such a comparison and go unnoticed.
    """
    return _roundtrip_key(devanagari, script) == _roundtrip_key(roundtrip_back(converted, script), script)


def _convert_line(text: str, script: str, a8f3: str | None) -> tuple[str, bool, str]:
    """Return (converted line, round trip ok, raw conversion) for one Devanagari string."""
    raw, out = _render_line(text, script, a8f3)
    return out, roundtrip_ok(text, raw, script), raw


def transliterate_master(
    master: Master, script: str, *, a8f3: str | None = None, diagnostics: list[dict] | None = None
) -> Master:
    """Return a copy of master with every lang=="sa" line, table cell and image label in `script`.

    Other languages pass through unchanged. Blocks with any failed round trip get "roundtrip_fail",
    and each failing string is appended to `diagnostics` as {page, block, text, converted, back}.
    The input master is not modified.
    """
    if script not in supported_scripts():
        raise ValueError(f"unsupported script {script!r}; choose from {', '.join(supported_scripts())}")
    out = master.model_copy(deep=True)
    a8f3 = _a8f3_for(script, a8f3)
    for page in out.pages:
        for block in page.blocks:
            fails: list[dict] = []
            if not block.is_sanskrit:
                # An English/Tamil block can quote Sanskrit, e.g. "Acharya Vandhanam (आचार्य वन्दनम्)":
                # only its Devanagari runs are converted, everything else passes through.
                if _has_devanagari(block):
                    _transliterate_block(block, script, a8f3, fails, runs_only=True)
            else:
                _transliterate_block(block, script, a8f3, fails)
            # Labels inside a drawing follow their own language, exactly as lines do.
            for label in block.labels or []:
                if label.is_sanskrit:
                    label.text = _convert_string(label.text, script, a8f3, fails, runs_only=False)
                elif _DEVA_RUN.search(label.text):
                    label.text = _convert_string(label.text, script, a8f3, fails, runs_only=True)
            if fails:
                block.add_flag(FLAG_ROUNDTRIP_FAIL)
                if diagnostics is not None:
                    diagnostics.extend({"page": page.page, "block": block.id, **f} for f in fails)
    return out


# A run of Devanagari words (with the spaces and punctuation between them) inside other-language text.
_DEVA_RUN = re.compile(r"[\u0900-\u097F\u1CD0-\u1CFF\uA8E0-\uA8FF]+(?:[ ,.;:'’-]+[\u0900-\u097F\u1CD0-\u1CFF\uA8E0-\uA8FF]+)*")


def _has_devanagari(block: Block) -> bool:
    cells = [c for row in block.rows or [] for c in row]
    return any(_DEVA_RUN.search(t) for t in [*block.lines, *cells])


def _convert_string(text: str, script: str, a8f3: str | None, fails: list[dict], *, runs_only: bool) -> str:
    """Convert a whole Devanagari string, or only the Devanagari runs of a mixed-language one.

    Each piece whose round trip fails is recorded in `fails`.
    """
    pieces = [(m.start(), m.end()) for m in _DEVA_RUN.finditer(text)] if runs_only else [(0, len(text))]
    parts, pos = [], 0
    for a, b in pieces:
        converted, ok, raw = _convert_line(text[a:b], script, a8f3)
        if not ok:
            fails.append({"text": text[a:b], "converted": converted, "back": roundtrip_back(raw, script)})
        parts += [text[pos:a], converted]
        pos = b
    parts.append(text[pos:])
    return "".join(parts)


def _transliterate_block(block: Block, script: str, a8f3: str | None, fails: list[dict], *, runs_only: bool = False) -> None:
    """Convert a block's lines and cells in place; failed round trips are appended to `fails`."""
    block.lines = [_convert_string(line, script, a8f3, fails, runs_only=runs_only) for line in block.lines]
    if block.rows is not None:
        block.rows = [[_convert_string(c, script, a8f3, fails, runs_only=runs_only) for c in row] for row in block.rows]
