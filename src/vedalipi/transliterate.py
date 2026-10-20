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
    return raw, (raw.replace(A8F3, a8f3) if a8f3 is not None else raw)


def transliterate_text(text: str, script: str, *, a8f3: str | None = None) -> str:
    """Convert one Devanagari string to `script`, preserving U+0951/U+0952/U+1CDA exactly.

    U+A8F3 becomes `a8f3` (default DEFAULT_A8F3[script] when present). Non-Devanagari characters pass through.
    """
    _, out = _render_line(text, script, _a8f3_for(script, a8f3))
    return out


def _roundtrip_key(text: str) -> str:
    # The one sanctioned equivalence: targets without an om sign write ॐ as ओं (ಓಂ, ఓం), which reads back as ओं.
    return normalize_text(text).replace("ॐ", "ओं")


def roundtrip_back(converted: str, script: str) -> str:
    """`converted` read back into Devanagari."""
    return _convert(converted, script, DEVA)


def roundtrip_ok(devanagari: str, converted: str, script: str) -> bool:
    """Convert `converted` back to Devanagari and compare it with `devanagari` directly (after normalize_text).

    The comparison is in Devanagari, never after a second forward conversion: a character the forward
    conversion drops would vanish from both sides of such a comparison and go unnoticed.
    """
    return _roundtrip_key(devanagari) == _roundtrip_key(roundtrip_back(converted, script))


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
