# ABOUTME: Validate stage: cross-checks normalized text against the page image and flags suspect lines.
# ABOUTME: Svara marks counted per image line give "svara_count_mismatch"; impossible sign orders give "invalid_sequence".

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalize_text
from .schema import FLAG_INVALID_SEQUENCE, FLAG_READER_DISAGREEMENT, FLAG_SVARA_COUNT_MISMATCH, FlagReport, Page

# Part of the validate stage fingerprint: bump when the checks change so cached flags are redone.
VALIDATE_VERSION = "3"

# Text codepoints counted as marks above / below the line.
_ABOVE = {"॑", "᳚"}  # svarita stroke, double svarita
_BELOW = {"॒"}  # anudatta bar

# Characters a combining mark cannot attach to: without a base akshara before
# them, marks are OCR noise.
_PUNCT = set("।॥.,:;!?\"''…’‘“”()-–—/")


def _is_combining(ch: str) -> bool:
    return unicodedata.category(ch) in ("Mn", "Mc", "Me")


def _text_counts(line: str) -> tuple[int, int]:
    """Return (above, below) svara mark counts in a line of text."""
    above = sum(1 for ch in line if ch in _ABOVE)
    below = sum(1 for ch in line if ch in _BELOW)
    return above, below


def _is_consonant(ch: str) -> bool:
    o = ord(ch)
    return 0x0915 <= o <= 0x0939 or 0x0958 <= o <= 0x095F or 0x0978 <= o <= 0x097F


def _is_vowel_sign(ch: str) -> bool:
    o = ord(ch)
    return 0x093A <= o <= 0x094C and o not in (0x093C, 0x093D) or o in (0x094E, 0x094F) or 0x0955 <= o <= 0x0957 or o in (0x0962, 0x0963)


def invalid_sequences(line: str) -> list[str]:
    """Devanagari sign orders no word can have, e.g. two vowel signs on one letter (सिु) or a vowel sign
    after a virama. These are printing glitches or misreadings; they are reported, never auto-corrected,
    and fonts often drop the second sign silently."""
    bad = []
    for i, ch in enumerate(line):
        if ch == "्" and (i == 0 or not (_is_consonant(line[i - 1]) or line[i - 1] == "़")):
            bad.append(line[max(0, i - 2) : i + 2])
        if not _is_vowel_sign(ch):
            continue
        prev = line[i - 1] if i else ""
        # A vowel sign sits on a consonant, or on a nukta that sits on one.
        if not (_is_consonant(prev) or (prev == "़" and i > 1 and _is_consonant(line[i - 2]))):
            bad.append(line[max(0, i - 2) : i + 2])
    return bad


def _orphan_marks(line: str) -> int:
    """Count combining marks with no base character before them (line start, space or punctuation).

    Marks after matras or other combining marks belong to the same akshara
    (e.g. न्ति॑) and are fine. Marks on Devanagari digits (kampa, e.g. १॒॑)
    are legal: digits are base characters.
    """
    bad = 0
    prev: str | None = None
    for ch in line:
        if _is_combining(ch) and (prev is None or prev.isspace() or prev in _PUNCT):
            bad += 1
        prev = ch
    return bad


@dataclass
class _Line:
    """One printed text line found on the page image, anchored on its headline (shirorekha)."""

    top: int  # headline top row
    bot: int  # headline bottom row
    above: list[int] = field(default_factory=list)  # x centres of svarita strokes over this line
    below: list[int] = field(default_factory=list)  # x centres of anudatta bars under this line

    @property
    def centre(self) -> float:
        return (self.top + self.bot) / 2


def _headlines(bw: object) -> list[tuple[int, int]]:
    """Find headline row spans: rows rich in horizontal strokes after a horizontal opening.

    Ruling lines (one unbroken run across much of the page) are rejected; word headlines break
    between words.
    """
    import cv2
    import numpy as np

    h, w = bw.shape
    k = max(25, int(0.022 * w))
    opened = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
    ink = opened.sum(axis=1) / 255.0
    rows = np.flatnonzero(ink >= max(3 * k, 0.02 * w))
    spans: list[tuple[int, int]] = []
    for r in rows:
        if spans and r - spans[-1][1] <= 3:
            spans[-1] = (spans[-1][0], int(r))
        else:
            spans.append((int(r), int(r)))
    out = []
    for a, b in spans:
        peak = a + int(np.argmax(ink[a : b + 1]))
        keep = np.flatnonzero(ink[a : b + 1] >= 0.5 * ink[peak]) + a
        top, bot = int(keep.min()), int(keep.max())
        row = opened[peak] > 0
        # Longest unbroken run on the peak row: a rule spans the page, a headline stops at the word.
        edges = np.flatnonzero(np.diff(np.concatenate(([0], row.astype(np.int8), [0]))))
        longest = int((edges[1::2] - edges[0::2]).max()) if len(edges) else 0
        if longest > 0.45 * w or bot - top > max(30, 0.01 * h):
            continue
        out.append((top, bot))
    return out


def _page_lines(gray: object) -> tuple[list[_Line], float]:
    """Lines on the page with their svara marks, and the typical body height (headline to baseline).

    Every mark is a small detached component. A tall thin one (svarita stroke) belongs to the first
    headline below it; a flat wide one (anudatta bar) sitting clearly under a line's baseline belongs
    to the nearest headline above it. Glyph parts are attached to their letters and so never count.
    """
    import cv2
    import numpy as np

    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    heads = _headlines(bw)
    if not heads:
        return [], 0.0
    lines = [_Line(t, b) for t, b in heads]
    n, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    # Body height: median depth of the letter masses hanging from the headlines.
    depths = []
    tops = np.array([t for t, _ in heads])
    for i in range(1, n):
        x, y, ww, hh, _ = (int(v) for v in stats[i])
        # The headline this component hangs from: the last one starting above its foot. Letters can
        # rise above the headline, so the component's top is not a reliable key.
        j = int(np.searchsorted(tops, y + hh, side="right")) - 1
        if j >= 0 and y <= heads[j][1] + 1 and y + hh > heads[j][1] + 3 and ww > 2 * (heads[j][1] - heads[j][0] + 1):
            depths.append(y + hh - heads[j][0])
    xb = float(np.median(depths)) if depths else 60.0
    # A slightly skewed or wavy headline shows up as stacked runs a fraction of a line apart: join them.
    merged: list[_Line] = []
    for ln in lines:
        if merged and ln.top - merged[-1].bot < 0.5 * xb:
            merged[-1].bot = ln.bot
        else:
            merged.append(ln)
    lines = merged
    strokes: list[tuple[int, int, int]] = []  # (line index, x0, x1)
    bars: list[tuple[int, int, int]] = []
    for i in range(1, n):
        x, y, ww, hh, area = (int(v) for v in stats[i])
        if area < 0.002 * xb * xb:
            continue
        bottom, cy = y + hh, y + hh / 2
        if hh >= 1.8 * ww and 0.2 * xb <= hh <= 1.0 * xb and ww <= 0.25 * xb:
            # Svarita: the first headline starting at or just below the stroke's foot.
            # It must also clear the line above's body, or a comma hanging below that line would count.
            cand = [
                j for j, ln in enumerate(lines)
                if ln.top - 0.7 * xb <= bottom <= ln.top + 0.15 * xb and (j == 0 or y > lines[j - 1].bot + 1.0 * xb)
            ]
            if cand:
                strokes.append((cand[0], x, x + ww))
        elif ww >= 2 * hh and 0.2 * xb <= ww <= 1.3 * xb and hh <= 0.25 * xb:
            # ...and above the next line's headline, or a dash inside that line would count.
            cand = [
                j for j, ln in enumerate(lines)
                if ln.bot + 0.7 * xb <= cy <= ln.bot + 1.9 * xb and (j + 1 == len(lines) or cy < lines[j + 1].top - 0.1 * xb)
            ]
            if cand:
                bars.append((cand[-1], x, x + ww))
    for marks, gap, attr in ((strokes, 0.3 * xb, "above"), (bars, 0.08 * xb, "below")):
        # Pieces closer than `gap` are one mark: the two strokes of U+1CDA, a bar broken by the scan.
        marks.sort()
        cur: tuple[int, int, int] | None = None
        for j, x0, x1 in [*marks, (-1, 0, 0)]:
            if cur and j == cur[0] and x0 - cur[2] <= gap:
                cur = (j, cur[1], max(cur[2], x1))
                continue
            if cur:
                getattr(lines[cur[0]], attr).append((cur[1] + cur[2]) // 2)
            cur = (j, x0, x1)
    return lines, xb


def _pick_lines(lines: list[_Line], y0: float, y1: float, want: int, pad: float) -> list[_Line] | None:
    """The `want` consecutive page lines that sit inside the block's vertical extent, or None."""
    for p in (0.0, pad, 2 * pad):
        inside = [ln for ln in lines if y0 - p <= ln.centre <= y1 + p]
        if len(inside) == want:
            return inside
        if len(inside) > want:
            # A loose bbox reaches a neighbour's line: keep the run that best fits the bbox edges.
            best = min(
                range(len(inside) - want + 1),
                key=lambda s: abs(inside[s].top - y0) + abs(inside[s + want - 1].bot - y1),
            )
            return inside[best : best + want]
    return None


def table_lines(rows: list[list[str]]) -> list[str]:
    """One entry per printed line of a table: a row whose cells hold several lines ("\n") spans that many
    printed lines, and printed line k joins the k-th line of every cell."""
    out = []
    for row in rows:
        cells = [c.split("\n") for c in row]
        for k in range(max((len(c) for c in cells), default=0)):
            out.append(" ".join(c[k] for c in cells if k < len(c)))
    return out


# Block types that never carry svaras (checking them only produces noise from rules and logos).
_SKIP_TYPES = {"running_header", "page_number", "image", "separator"}


def _sa_lines(page: Page) -> list[tuple[str, int, str]]:
    """(block id, line number, normalized text) for every checkable Sanskrit line in reading order."""
    out = []
    for b in page.blocks:
        if b.lang != "sa" or b.type in _SKIP_TYPES:
            continue
        for i, line in enumerate(b.lines or table_lines(b.rows or []), 1):
            if line.strip():
                out.append((b.id, i, normalize_text(line)))
    return out


def _key(line: str) -> str:
    return "".join(line.split())


def _word_diff(a: str, b: str) -> str:
    wa, wb = a.split(), b.split()
    parts = []
    for op, i0, i1, j0, j1 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if op != "equal":
            parts.append(f"{' '.join(wa[i0:i1]) or '∅'} / {' '.join(wb[j0:j1]) or '∅'}")
    return "; ".join(parts[:6])


def cross_check(page: Page, second: Page) -> list[tuple[str, str]]:
    """Compare the page with an independent second OCR read of the same image, line by line.

    Returns (block id, note) for every line the two reads give differently. A per-line mark count cannot
    see a mark put on the wrong akshara or a remembered word; two readers rarely make the same slip.
    Lines only one read has are left to the other checks (band seams and block splits differ between reads).
    """
    a, b = _sa_lines(page), _sa_lines(second)
    ka, kb = [_key(x[2]) for x in a], [_key(x[2]) for x in b]
    out = []
    for op, i0, i1, j0, j1 in difflib.SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes():
        if op != "replace":
            continue
        # Pair each differing line with its most similar counterpart in the other read.
        for i in range(i0, i1):
            best = max(range(j0, j1), key=lambda j: difflib.SequenceMatcher(None, ka[i], kb[j], autojunk=False).ratio())
            if difflib.SequenceMatcher(None, ka[i], kb[best], autojunk=False).ratio() < 0.6:
                continue
            bid, n, text = a[i]
            out.append((bid, f"{bid} line {n}: second read differs: {_word_diff(text, b[best][2])}"))
    return out


def validate_page(page: Page, image: Path | None, second: Page | None = None) -> FlagReport:
    """Check a normalized page and return flags per block id.

    With an image: for each sa line, locate its region (block bbox split into lines), count stroke
    marks above the headline and below the baseline, and compare with counts of U+0951+U+1CDA and
    U+0952 in the text; a mismatch adds "svara_count_mismatch". Without an image (text input),
    only text-level checks run (e.g. invalid mark sequences). With `second` (an independent second OCR
    read of the same image), every line the two reads disagree on gets "reader_disagreement".
    """
    report = FlagReport(page=page.page)

    def flag(block_id: str, note: str, name: str = FLAG_SVARA_COUNT_MISMATCH) -> None:
        report.block_flags.setdefault(block_id, [])
        if name not in report.block_flags[block_id]:
            report.block_flags[block_id].append(name)
        report.notes.append(note)

    if second is not None:
        for block_id, note in cross_check(page, second):
            flag(block_id, note, FLAG_READER_DISAGREEMENT)

    lines_img: list[_Line] = []
    xb = 0.0
    img_h = img_w = 0
    if image is not None and Path(image).exists():
        import cv2

        gray = cv2.imread(str(image), cv2.IMREAD_GRAYSCALE)
        if gray is not None:
            img_h, img_w = gray.shape
            lines_img, xb = _page_lines(gray)
            del gray
    cursor = 0
    used: list[tuple[int, float, float]] = []  # (line id, x0, x1) claimed by earlier blocks
    for block in page.blocks:
        if block.lang != "sa" or block.type in _SKIP_TYPES:
            continue
        lines = list(block.lines)
        if not lines and block.rows:
            lines = table_lines(block.rows)
        if not lines:
            continue
        for i, line in enumerate(lines):
            if _orphan_marks(line):
                flag(block.id, f"{block.id} line {i + 1}: orphan combining mark (no base character)")
            for seq in invalid_sequences(line):
                flag(block.id, f"{block.id} line {i + 1}: invalid sign order {seq!r}", FLAG_INVALID_SEQUENCE)
        if not lines_img:
            continue
        if block.bbox:
            x0, x1 = (block.bbox[0] - 0.02) * img_w, (block.bbox[2] + 0.02) * img_w
            # Lines an earlier block already claimed in an overlapping column are not candidates:
            # model bboxes of consecutive blocks often overlap by a line.
            free = [ln for ln in lines_img if not any(i == id(ln) and a < x1 and x0 < b for i, a, b in used)]
            picked = _pick_lines(free, block.bbox[1] * img_h, block.bbox[3] * img_h, len(lines), xb)
            if picked:
                cursor = max(cursor, lines_img.index(picked[-1]) + 1)
        else:
            x0, x1 = 0, img_w
            picked = lines_img[cursor : cursor + len(lines)]
            cursor += len(lines)
            if len(picked) != len(lines):
                picked = None
        if picked is None:
            report.notes.append(f"{block.id}: {len(lines)} text lines could not be aligned to the image; not checked")
            continue
        used.extend((id(ln), x0, x1) for ln in picked)
        got = [(sum(x0 <= x <= x1 for x in ln.above), sum(x0 <= x <= x1 for x in ln.below)) for ln in picked]
        want = [_text_counts(line) for line in lines]
        if not any(a or b for a, b in want) and (sum(1 for g in got if g != (0, 0)) * 2 < len(lines) or sum(map(sum, got)) < 2):
            # Unaccented text: stray mark-shaped specks (broken letters, dust) on a few lines are noise.
            # Only marks on most lines suggest the reader dropped a whole block's accents.
            continue
        for i, (g, t) in enumerate(zip(got, want)):
            if g != t:
                flag(block.id, f"{block.id} line {i + 1}: text above/below {t[0]}/{t[1]} != image {g[0]}/{g[1]}")
    return report
