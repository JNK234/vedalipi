# ABOUTME: Derives "Vedalipi" copies of the vendored Noto fonts whose Vedic svara marks are heavier and set lower.
# ABOUTME: Only the svara outlines change (cmap, GSUB and GPOS stay as shipped), so shaping and the text layer are untouched.

from __future__ import annotations

import re
from pathlib import Path

# Fonts (by vendored file stem prefix) whose svaras are re-cut; the derived family swaps "Noto" for "Vedalipi".
SVARA_SOURCES = ("NotoSerifKannada", "NotoSansTelugu", "NotoSerifDevanagari")

# Every glyph that draws an udatta (U+0951), anudatta (U+0952) or double svarita (U+1CDA), including the
# contextual variants GSUB picks under conjuncts and beside matras.
SVARA_GLYPH_RE = re.compile(r"^(udatta|anudatta|doubleSvarita|uni0951|uni0952|uni1CDA|candrabindu_(an)?udatta)")

# Target geometry in em. Noto draws a 0.07 em anudatta bar right under the letter and 0.06-0.07 em udatta
# ticks; at reading size both vanish into the letter. prapatti.com sets the bar about 0.08 em thick and
# well clear of the akshara, which is what these values aim for.
TICK_WIDTH = 0.105  # udatta / double svarita tick width
TICK_RAISE = 0.0  # extra height added on top of each tick (none: tall bases already lift them)
BAR_THICKNESS = 0.10  # anudatta bar thickness (grows downward)
BAR_DROP = 0.045  # anudatta bar moves down by this much (more clearance from the akshara)
SHALLOW_BAR = 0.15  # a bar whose top is within this of the baseline is the default (undropped) anudatta
VERSION = 3  # bump when the geometry changes so cached derived fonts are rebuilt


def derived_family(family: str) -> str:
    """Family name of the svara-emphasised copy of a vendored family ("Noto Serif Kannada" -> "Vedalipi Serif Kannada")."""
    return family.replace("Noto", "Vedalipi", 1)


def has_derived(family: str) -> bool:
    return family.replace(" ", "") in SVARA_SOURCES


def _rect(coords, start: int, end: int) -> tuple[int, int, int, int] | None:
    """Bounds of contour points start..end if it is a 4-point axis-aligned rectangle (every Noto svara stroke is)."""
    pts = [coords[i] for i in range(start, end + 1)]
    if len(pts) != 4:
        return None
    xs = sorted({x for x, _ in pts})
    ys = sorted({y for _, y in pts})
    if len(xs) != 2 or len(ys) != 2:
        return None
    return xs[0], ys[0], xs[1], ys[1]


def recut_svaras(font) -> int:
    """Thicken and reposition the rectangular svara strokes of a TTFont in place; returns glyphs changed."""
    upm = font["head"].unitsPerEm
    glyf = font["glyf"]
    tick_w, raise_, bar_t, drop = (round(v * upm) for v in (TICK_WIDTH, TICK_RAISE, BAR_THICKNESS, BAR_DROP))
    shallow = round(SHALLOW_BAR * upm)
    changed = 0
    for name in font.getGlyphOrder():
        if not SVARA_GLYPH_RE.match(name):
            continue
        g = glyf[name]
        if g.isComposite() or g.numberOfContours <= 0:
            continue  # composites reference a re-cut base glyph and inherit its new shape
        coords = g.coordinates
        start = 0
        touched = False
        for end in g.endPtsOfContours:
            box = _rect(coords, start, end)
            if box is not None:
                x0, y0, x1, y1 = box
                w, h = x1 - x0, y1 - y0
                if h > w and y0 > 0:  # a vertical tick above the letter
                    cx = (x0 + x1) / 2
                    nx0, nx1 = round(cx - max(w, tick_w) / 2), round(cx + max(w, tick_w) / 2)
                    ny0, ny1 = y0, y1 + raise_
                elif w > h and y1 <= 0:  # a horizontal bar below the letter
                    nx0, nx1 = x0, x1
                    # Only the default bar drops; the contextual .low/.lower variants already sit deep.
                    ny1 = y1 - drop if y1 > -shallow else y1
                    ny0 = ny1 - max(h, bar_t)
                else:
                    start = end + 1
                    continue
                for i in range(start, end + 1):
                    x, y = coords[i]
                    coords[i] = (nx0 if x == x0 else nx1, ny0 if y == y0 else ny1)
                touched = True
            start = end + 1
        if touched:
            g.recalcBounds(glyf)
            changed += 1
    return changed


def _rename(font, family: str) -> None:
    style = font["name"].getDebugName(2) or "Regular"
    ps = family.replace(" ", "") + "-" + style.replace(" ", "")
    for rec in font["name"].names:
        if rec.nameID == 1 or rec.nameID == 16:
            rec.string = family
        elif rec.nameID == 4:
            rec.string = f"{family} {style}"
        elif rec.nameID == 3:
            rec.string = f"{ps};vedalipi-svara-{VERSION}"
        elif rec.nameID == 6:
            rec.string = ps


def derive_fonts(fonts_dir: Path, out_dir: Path) -> Path:
    """Write svara-emphasised copies of the SVARA_SOURCES fonts into out_dir (cached by VERSION); returns out_dir.

    The vendored fonts are OFL 1.1 with no Reserved Font Name, so modified copies may be made; they are
    renamed so they never shadow the originals, and OFL.txt is copied alongside."""
    from fontTools.ttLib import TTFont

    out_dir = Path(out_dir) / f"v{VERSION}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(Path(fonts_dir).glob("*.ttf")):
        if not src.stem.startswith(SVARA_SOURCES):
            continue
        dest = out_dir / src.name.replace("Noto", "Vedalipi", 1)
        if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
            continue
        font = TTFont(src)
        recut_svaras(font)
        _rename(font, derived_family(font["name"].getDebugName(1)))
        tmp = dest.with_suffix(".tmp")
        font.save(tmp)
        tmp.replace(dest)
    lic = Path(fonts_dir) / "OFL.txt"
    if lic.exists():
        (out_dir / "OFL.txt").write_bytes(lic.read_bytes())
    return out_dir
