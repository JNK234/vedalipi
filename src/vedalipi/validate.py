# ABOUTME: Validate stage: cross-checks normalized text against the page image and flags suspect lines.
# ABOUTME: Svara marks counted per image line give "svara_count_mismatch"; impossible sign orders give "invalid_sequence".

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalize_text
from .schema import (
    FLAG_INVALID_SEQUENCE,
    FLAG_NON_VEDIC_MARK,
    FLAG_READER_DISAGREEMENT,
    FLAG_SINGLE_READ,
    FLAG_SVARA_COUNT_MISMATCH,
    FLAG_UNCONVERTED_LETTER,
    FlagReport,
    Page,
)

# Part of the validate stage fingerprint: bump when the checks change so cached flags are redone.
VALIDATE_VERSION = "6"

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


# Blocks whose characters belong in Devanagari Sanskrit: Devanagari, Vedic Extensions, Devanagari Extended.
_DEVA_BLOCKS = ((0x0900, 0x097F), (0x1CD0, 0x1CFF), (0xA8E0, 0xA8FF))
# Blocks of the other Indic scripts (Bengali to Sinhala; Brahmi, Grantha, Sharada, ... in plane 1). After
# conversion to Devanagari, a character from one of them is a letter or sign of the source script that the
# converter did not know (Telugu ౘ, Kannada ೝ) and passed through as it was.
_INDIC_BLOCKS = ((0x0980, 0x0DFF), (0x11000, 0x11FFF))


def _in(blocks: tuple[tuple[int, int], ...], ch: str) -> bool:
    return any(a <= ord(ch) <= b for a, b in blocks)


def stray_kind(ch: str) -> str | None:
    """Why `ch` does not belong in a Devanagari Sanskrit line, as a flag name, or None when it does.

    FLAG_UNCONVERTED_LETTER: any character of another Indic script. FLAG_NON_VEDIC_MARK: any other combining
    mark outside Devanagari and the Vedic blocks, e.g. U+0331 (a Latin macron below, which OCR gives for
    an anudatta bar). Everything else (ASCII, punctuation, spaces, joiners) passes.
    """
    if _in(_DEVA_BLOCKS, ch):
        return None
    if _in(_INDIC_BLOCKS, ch):
        return FLAG_UNCONVERTED_LETTER
    if _is_combining(ch):
        return FLAG_NON_VEDIC_MARK
    return None


@dataclass(frozen=True)
class Stray:
    """Every occurrence of one stray character in one Sanskrit string (a line, table cell or label)."""

    block: str
    where: str  # "line 6", "row 2 cell 1", "label 3"
    flag: str
    char: str
    count: int
    words: tuple[str, ...]  # the words it occurs in, so a proofreader can find it on the page

    @property
    def name(self) -> str:
        return f"U+{ord(self.char):04X} {unicodedata.name(self.char, 'UNNAMED')}"

    def note(self) -> str:
        return f"{self.block} {self.where}: {self.flag} {self.name} x{self.count} in {', '.join(self.words)}"


def _strays_in(block: str, where: str, text: str) -> list[Stray]:
    found: dict[str, list[str]] = {}
    for word in text.split():
        for ch in word:
            if stray_kind(ch):
                found.setdefault(ch, []).append(word)
    return [
        Stray(block, where, stray_kind(ch), ch, len(words), tuple(dict.fromkeys(words)))
        for ch, words in found.items()
    ]


def page_strays(page: Page) -> list[Stray]:
    """Stray characters in every Sanskrit string of the page: lines, table cells and labels, whatever the
    block type. Text is read as it is; nothing is changed."""
    out: list[Stray] = []
    for b in page.blocks:
        if b.is_sanskrit:
            for i, line in enumerate(b.lines, 1):
                out += _strays_in(b.id, f"line {i}", line)
            for r, row in enumerate(b.rows or [], 1):
                for c, cell in enumerate(row, 1):
                    out += _strays_in(b.id, f"row {r} cell {c}", cell)
        for k, label in enumerate(b.labels or [], 1):
            if label.is_sanskrit:
                out += _strays_in(b.id, f"label {k}", label.text)
    return out


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


# Two lines at least this similar are one printed line read twice; below it, different lines.
_SAME_LINE = 0.6


@dataclass
class ReadComparison:
    """Two reads of a page lined up: lines read differently, and lines only one read has."""

    differ: list[tuple[str, str]] = field(default_factory=list)  # (block id, note)
    # (block id, line number) of a first-read line -> the second-read line paired with it (equal or not).
    pairs: dict[tuple[str, int], str] = field(default_factory=dict)
    # (block id, note): a line of the first read with no counterpart in the second, or a line of the
    # second read with none in the first (noted on the block of the first-read line it follows).
    single: list[tuple[str, str]] = field(default_factory=list)


def compare_reads(page: Page, second: Page) -> ReadComparison:
    """Line up the page's Sanskrit lines with an independent second read of the same page.

    A line both reads have but give differently is a disagreement: a per-line mark count cannot see a
    mark put on the wrong akshara or a remembered word, and two readers rarely make the same slip. A line
    with no counterpart at least _SAME_LINE similar in the other read was read only once.
    """
    a, b = _sa_lines(page), _sa_lines(second)
    ka, kb = [_key(x[2]) for x in a], [_key(x[2]) for x in b]
    out = ReadComparison()

    def ratio(i: int, j: int) -> float:
        return difflib.SequenceMatcher(None, ka[i], kb[j], autojunk=False).ratio()

    def only_second(after: int, j: int) -> None:
        # Noted on the first-read line it follows (or precedes, at the top of the page).
        if not a:
            return
        bid, n, _ = a[max(after, 0)]
        where = f"after line {n}" if after >= 0 else f"before line {n}"
        out.single.append((bid, f"{bid} {where}: single_read: only the second read has: {b[j][2]}"))

    def only_first(i: int) -> None:
        bid, n, _ = a[i]
        out.single.append((bid, f"{bid} line {n}: single_read: only the first read has this line"))

    for op, i0, i1, j0, j1 in difflib.SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes():
        if op == "equal":
            for i, j in zip(range(i0, i1), range(j0, j1)):
                out.pairs[(a[i][0], a[i][1])] = b[j][2]
        elif op == "delete":
            for i in range(i0, i1):
                only_first(i)
        elif op == "insert":
            for j in range(j0, j1):
                only_second(i0 - 1, j)
        elif op == "replace":
            pairs = _pair_lines(i0, i1, j0, j1, ratio)
            paired_j = {j for _, j in pairs}
            last_i = i0 - 1  # the first-read line a second-read-only line follows
            by_i = dict(pairs)
            for i in range(i0, i1):
                if i not in by_i:
                    only_first(i)
                    continue
                bid, n, text = a[i]
                out.pairs[(bid, n)] = b[by_i[i]][2]
                out.differ.append((bid, f"{bid} line {n}: second read differs: {_word_diff(text, b[by_i[i]][2])}"))
            for j in range(j0, j1):
                if j in paired_j:
                    last_i = next(i for i, jj in pairs if jj == j)
                else:
                    only_second(last_i, j)
    return out


def _pair_lines(i0: int, i1: int, j0: int, j1: int, ratio) -> list[tuple[int, int]]:
    """Pair lines i0..i1 of one read with lines j0..j1 of the other, one to one and in reading order,
    maximizing total similarity; only pairs at least _SAME_LINE similar count. Liturgical text repeats
    whole phrases line after line, so pairing each line with its most similar line on its own would
    pair two lines with the same counterpart and hide the line the other read lacks."""
    n, m = i1 - i0, j1 - j0
    r = [[ratio(i0 + x, j0 + y) for y in range(m)] for x in range(n)]
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for x in range(1, n + 1):
        for y in range(1, m + 1):
            take = best[x - 1][y - 1] + r[x - 1][y - 1] if r[x - 1][y - 1] >= _SAME_LINE else -1.0
            best[x][y] = max(best[x - 1][y], best[x][y - 1], take)
    pairs = []
    x, y = n, m
    while x and y:
        if r[x - 1][y - 1] >= _SAME_LINE and best[x][y] == best[x - 1][y - 1] + r[x - 1][y - 1]:
            pairs.append((i0 + x - 1, j0 + y - 1))
            x, y = x - 1, y - 1
        elif best[x][y] == best[x - 1][y]:
            x -= 1
        else:
            y -= 1
    return pairs[::-1]


def cross_check(page: Page, second: Page) -> list[tuple[str, str]]:
    """(block id, note) for every line the page and its second read give differently."""
    return compare_reads(page, second).differ


def validate_page(page: Page, image: Path | None, second: Page | None = None) -> FlagReport:
    """Check a normalized page and return flags per block id.

    With an image: for each sa line, locate its region (block bbox split into lines), count stroke
    marks above the headline and below the baseline, and compare with counts of U+0951+U+1CDA and
    U+0952 in the text; a mismatch adds "svara_count_mismatch". Without an image (text input),
    only text-level checks run (e.g. invalid mark sequences). With `second` (an independent second OCR
    read of the same image), every line the two reads disagree on gets "reader_disagreement"; when the page
    has no image svara count, a line only one read has gets "single_read".
    Every Sanskrit line, cell and label is also checked for characters that do not belong in Devanagari:
    "unconverted_letter" for a source-script letter the conversion left as it was, "non_vedic_mark" for
    a combining mark such as U+0331. The text itself is never changed.
    """
    report = FlagReport(page=page.page)

    def flag(block_id: str, note: str, name: str = FLAG_SVARA_COUNT_MISMATCH) -> None:
        report.block_flags.setdefault(block_id, [])
        if name not in report.block_flags[block_id]:
            report.block_flags[block_id].append(name)
        report.notes.append(note)

    compared = compare_reads(page, second) if second is not None else None
    if compared is not None:
        for block_id, note in compared.differ:
            flag(block_id, note, FLAG_READER_DISAGREEMENT)

    # A source letter the conversion did not know, or a non-Vedic mark, stays in the text exactly as read:
    # flagged for proofreading, never dropped or swapped for a guess.
    for stray in page_strays(page):
        flag(stray.block, stray.note(), stray.flag)

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
    if compared is not None and not lines_img:
        # No image svara count on this page (a book in another script, or no readable image): a line only
        # one reader saw has had no check at all.
        for block_id, note in compared.single:
            flag(block_id, note, FLAG_SINGLE_READ)
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
