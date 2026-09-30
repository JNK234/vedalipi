# ABOUTME: Transliterate stage: converts the Devanagari master into a target Indic script (vidyut, sanscript fallback).
# ABOUTME: Every sa line is round-tripped back to Devanagari; mismatches get "roundtrip_fail" but never stop the run.

from __future__ import annotations

import re

from functools import lru_cache

from .normalize import normalize_text
from .schema import FLAG_ROUNDTRIP_FAIL, Block, Master

# U+A8F3 (Vedic candrabindu-virama) replacement per target script; Noto lacks the glyph.
A8F3 = "ꣳ"
DEFAULT_A8F3 = {"kannada": "ಗ್ಂ", "telugu": "గ్ం"}

# Devanagari is the master language; every conversion starts from it.
DEVA = "devanagari"


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
    raw = _convert(text, DEVA, script)
    return raw, (raw.replace(A8F3, a8f3) if a8f3 else raw)


def transliterate_text(text: str, script: str, *, a8f3: str | None = None) -> str:
    """Convert one Devanagari string to `script`, preserving U+0951/U+0952/U+1CDA exactly.

    U+A8F3 becomes `a8f3` (default DEFAULT_A8F3[script] when present). Non-Devanagari characters pass through.
    """
    _, out = _render_line(text, script, _a8f3_for(script, a8f3))
    return out


def roundtrip_ok(devanagari: str, converted: str, script: str) -> bool:
    """Convert `converted` back to Devanagari and compare with `devanagari` after normalize_text."""
    back = _convert(converted, script, DEVA)
    if normalize_text(devanagari) == normalize_text(back):
        return True
    # vidyut folds ॐ and its target forms (ಓಂ/ఓం) together, so ॐ and ओं compare equal in the target
    # script even though they differ in Devanagari. Re-convert both and compare there.
    return _convert(normalize_text(devanagari), DEVA, script) == _convert(normalize_text(back), DEVA, script)


def _convert_line(text: str, script: str, a8f3: str | None) -> tuple[str, bool]:
    """Return (converted line, round trip ok) for one Devanagari string."""
    raw, out = _render_line(text, script, a8f3)
    return out, roundtrip_ok(text, raw, script)


def transliterate_master(master: Master, script: str, *, a8f3: str | None = None) -> Master:
    """Return a copy of master with every lang=="sa" line and table cell in `script`.

    Other languages pass through unchanged. Blocks with any failed round trip get "roundtrip_fail".
    The input master is not modified.
    """
    out = master.model_copy(deep=True)
    a8f3 = _a8f3_for(script, a8f3)
    for page in out.pages:
        for block in page.blocks:
            if not block.is_sanskrit:
                # An English/Tamil block can quote Sanskrit, e.g. "Acharya Vandhanam (आचार्य वन्दनम्)":
                # only its Devanagari runs are converted, everything else passes through.
                if _has_devanagari(block) and _transliterate_block(block, script, a8f3, runs_only=True):
                    block.add_flag(FLAG_ROUNDTRIP_FAIL)
                continue
            if _transliterate_block(block, script, a8f3):
                block.add_flag(FLAG_ROUNDTRIP_FAIL)
    return out


# A run of Devanagari words (with the spaces and punctuation between them) inside other-language text.
_DEVA_RUN = re.compile(r"[\u0900-\u097F\u1CD0-\u1CFF\uA8E0-\uA8FF]+(?:[ ,.;:'’-]+[\u0900-\u097F\u1CD0-\u1CFF\uA8E0-\uA8FF]+)*")


def _has_devanagari(block: Block) -> bool:
    cells = [c for row in block.rows or [] for c in row]
    return any(_DEVA_RUN.search(t) for t in [*block.lines, *cells])


def _convert_runs(text: str, script: str, a8f3: str | None) -> tuple[str, bool]:
    """Convert only the Devanagari runs of a mixed-language string."""
    ok_all = True
    parts, pos = [], 0
    for m in _DEVA_RUN.finditer(text):
        converted, ok = _convert_line(m.group(0), script, a8f3)
        parts += [text[pos : m.start()], converted]
        ok_all = ok_all and ok
        pos = m.end()
    parts.append(text[pos:])
    return "".join(parts), ok_all


def _transliterate_block(block: Block, script: str, a8f3: str | None, *, runs_only: bool = False) -> bool:
    """Convert a block's lines and cells in place; return True if any round trip failed."""
    convert = _convert_runs if runs_only else _convert_line
    failed = False
    lines = []
    for line in block.lines:
        text, ok = convert(line, script, a8f3)
        lines.append(text)
        failed = failed or not ok
    block.lines = lines
    if block.rows is not None:
        rows = []
        for row in block.rows:
            cells = []
            for cell in row:
                text, ok = convert(cell, script, a8f3)
                cells.append(text)
                failed = failed or not ok
            rows.append(cells)
        block.rows = rows
    return failed
