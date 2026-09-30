# ABOUTME: Splits a page image into overlapping horizontal bands (cut at the emptiest rows) and stitches the
# ABOUTME: per-band OCR blocks back into one page: page-space bboxes, seam dedup of repeated lines, block merging.

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from ..schema import FLAG_LOW_CONFIDENCE, Block

# Seam lines whose readings agree at least this much are the same printed line.
MATCH_RATIO = 0.8
# How many lines on each side of a seam are compared.
SEAM_WINDOW = 8
# Block types whose content may run across a band seam and is rejoined into one block.
MERGEABLE = {"prose", "verse", "table"}
# Slack (fraction of page height) on model bboxes when deciding whether a block can reach the band overlap.
GEO_MARGIN = 0.03
# An unmatched seam line counts as a clipped reading of its neighbour's line only if at least this share
# of its characters appear, in order, in that full line.
CLIP_EVIDENCE = 0.5


@dataclass
class Band:
    index: int  # 1-based
    y0: int  # pixel rows [y0, y1) of the page image
    y1: int
    height: int  # page height in pixels

    @property
    def top(self) -> float:
        return self.y0 / self.height

    @property
    def bottom(self) -> float:
        return self.y1 / self.height


def ink_profile(gray: np.ndarray) -> np.ndarray:
    """Dark-pixel count per row, ignoring a 3% margin on each side where scan edges and shadows live."""
    h, w = gray.shape
    m = max(1, int(w * 0.03))
    return (gray[:, m : w - m] < 150).sum(axis=1).astype(np.int64)


def _quietest(smooth: np.ndarray, lo: int, hi: int, target: int) -> int:
    """Middle of the lowest-ink run of rows in [lo, hi) whose middle is nearest target."""
    seg = smooth[lo:hi]
    quiet = seg <= seg.min() + 0.5
    runs, start = [], None
    for i, q in enumerate(np.append(quiet, False)):
        if q and start is None:
            start = i
        elif not q and start is not None:
            runs.append((start + i - 1) // 2)
            start = None
    return lo + min(runs, key=lambda m: abs(lo + m - target))


def plan_bands(gray: np.ndarray, n: int, overlap: float = 0.03, search: float = 0.05) -> list[Band]:
    """n bands covering the page; each seam sits on the emptiest row near the even split, and every band
    reaches `overlap` (fraction of page height) past the seam so a line near it is whole in at least one band."""
    h = gray.shape[0]
    if n <= 1:
        return [Band(1, 0, h, h)]
    prof = ink_profile(gray)
    # Smooth over ~0.4% of the page so a thin gap inside a glyph never wins over a real line gap.
    k = max(1, int(h * 0.004))
    smooth = np.convolve(prof, np.ones(k) / k, mode="same")
    cuts = [0]
    for i in range(1, n):
        target = int(h * i / n)
        lo, hi = max(cuts[-1] + 1, target - int(h * search)), min(h - 1, target + int(h * search))
        cuts.append(_quietest(smooth, lo, hi, target) if hi > lo else target)
    cuts.append(h)
    ov = int(h * overlap)
    return [
        Band(i + 1, max(0, cuts[i] - (ov if i else 0)), min(h, cuts[i + 1] + (ov if i < n - 1 else 0)), h)
        for i in range(n)
    ]


def load_gray(image: Path) -> np.ndarray:
    with Image.open(image) as im:
        return np.asarray(im.convert("L"))


def crop_band(image: Path, band: Band, dst: Path, *, max_width: int | None = None) -> Path:
    """Write the band as a PNG (optionally downscaled to max_width) and return its path."""
    with Image.open(image) as im:
        crop = im.convert("L").crop((0, band.y0, im.width, band.y1))
    if max_width and crop.width > max_width:
        crop = crop.resize((max_width, round(crop.height * max_width / crop.width)), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dst, optimize=True)
    return dst


def to_page_bbox(bbox: list[float] | None, band: Band) -> list[float] | None:
    """Map a bbox normalized to the band image into page-normalized coordinates."""
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    span = band.bottom - band.top
    return [x0, band.top + y0 * span, x1, band.top + y1 * span]


def _key(text: str) -> str:
    """Comparison key: NFC, no whitespace, no dots (blank runs vary in length between readings)."""
    t = unicodedata.normalize("NFC", text)
    return "".join(ch for ch in t if not ch.isspace() and ch != ".")


def _has_units(b: Block) -> bool:
    return b.type not in ("image", "separator")


def _units(blocks: list[Block]) -> list[tuple[int, int, str]]:
    """Flatten text blocks into (block index, line/row index, key) units in reading order."""
    out = []
    for bi, b in enumerate(blocks):
        if not _has_units(b):
            continue
        items = [" | ".join(r) for r in b.rows] if b.rows is not None else b.lines
        for li, t in enumerate(items):
            out.append((bi, li, _key(t)))
    return out


def _same(a: str, b: str) -> bool:
    if not a and not b:
        return True
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= MATCH_RATIO


@dataclass
class Seam:
    drop_next: int = 0  # units to remove from the head of the lower band
    drop_prev: int = 0  # units to remove from the tail of what is stitched so far (a clipped fragment)
    pairs: list[tuple[str, str]] = field(default_factory=list)  # keys of the lines read twice
    # An unmatched line next to the seam that could not be shown to be a clipped fragment: kept, but suspect.
    suspect_prev: bool = False
    suspect_next: bool = False


def clipped(fragment: str, full: str | None) -> bool:
    """Evidence that `fragment` is a partial reading of the printed line read in full as `full`:
    it is empty, or most of its characters occur in order in `full`."""
    if not fragment:
        return True
    if not full:
        return False
    sm = difflib.SequenceMatcher(None, fragment, full, autojunk=False)
    return sum(m.size for m in sm.get_matching_blocks()) >= CLIP_EVIDENCE * len(fragment)


def _near_seam(units, blocks: list[Block], ok) -> list:
    """Longest run of units (from the seam side) whose block geometry allows them to lie in the overlap."""
    run = []
    for u in units:
        if not ok(blocks[u[0]].bbox):
            break
        run.append(u)
    return run


def seam_overlap(prev: list[Block], nxt: list[Block], upper: Band | None = None, lower: Band | None = None) -> Seam:
    """Find the lines of `nxt` that repeat the tail of `prev` across the seam between bands `upper` and `lower`.

    Only lines whose (page-space) block bbox can reach the shared overlap are compared, so a refrain
    repeated far from the seam is never mistaken for a duplicate. Allows one clipped/unmatched unit at
    the head of nxt and one at the tail of prev; both are dropped when the lines around them match.
    """
    pu = list(reversed(_units(prev)))
    nu = _units(nxt)
    if upper is not None and lower is not None:
        pu = _near_seam(pu, prev, lambda bb: bb is None or bb[3] >= lower.top - GEO_MARGIN)
        nu = _near_seam(nu, nxt, lambda bb: bb is None or bb[1] <= upper.bottom + GEO_MARGIN)
    pu = list(reversed(pu[:SEAM_WINDOW]))
    nu = nu[:SEAM_WINDOW]
    all_next = _units(nxt)
    found = []  # (matched, skip_head_of_next, skip_tail_of_prev), the longest match per skip choice
    for s in (0, 1):
        for t in (0, 1):
            tail = pu[: len(pu) - t]
            for k in range(min(len(tail), len(nu) - s), 0, -1):
                if all(_same(tail[len(tail) - k + i][2], nu[s + i][2]) for i in range(k)):
                    found.append((k, s, t))
                    break
    # Longest match first; among equals, the one that skips fewer lines.
    found.sort(key=lambda c: (-c[0], c[1] + c[2]))
    seam = Seam()
    for k, s, t in found:
        tail = pu[: len(pu) - t]
        # A skipped line is only dropped as a clipped fragment when there is evidence for it: its full
        # reading is the line on the other side of the matched run. Without evidence it may be real text.
        ok_prev = not t or clipped(pu[-1][2], all_next[s + k][2] if s + k < len(all_next) else None)
        ok_next = not s or clipped(nu[0][2], tail[len(tail) - k - 1][2] if len(tail) > k else None)
        if ok_prev and ok_next:
            pairs = [(tail[len(tail) - k + i][2], nu[s + i][2]) for i in range(k)]
            return Seam(drop_next=s + k, drop_prev=t, pairs=pairs,
                        suspect_prev=seam.suspect_prev, suspect_next=seam.suspect_next)
        seam.suspect_prev = seam.suspect_prev or not ok_prev
        seam.suspect_next = seam.suspect_next or not ok_next
    return seam


def _drop_units(blocks: list[Block], n: int, *, from_end: bool = False) -> list[Block]:
    """Remove n line/row units from the head (or tail) of blocks; text blocks left empty vanish."""
    out = []
    for b in (reversed(blocks) if from_end else blocks):
        b = b.model_copy(deep=True)
        if n > 0 and _has_units(b):
            items = b.rows if b.rows is not None else b.lines
            take = min(n, len(items))
            kept = items[: len(items) - take] if from_end else items[take:]
            n -= take
            if b.rows is not None:
                b.rows = kept
            else:
                b.lines = kept
            if not kept:
                continue
        out.append(b)
    return list(reversed(out)) if from_end else out


def _mergeable(a: Block, b: Block) -> bool:
    if a.type != b.type or a.type not in MERGEABLE or a.lang != b.lang or a.align != b.align:
        return False
    if a.type == "table":
        return bool(a.rows and b.rows) and len(a.rows[-1]) == len(b.rows[0])
    # A verse ending in a double danda is complete; the next band starts a new one.
    return not (a.type == "verse" and a.lines and a.lines[-1].rstrip().endswith("॥"))


def _union(a: list[float] | None, b: list[float] | None) -> list[float] | None:
    if a is None or b is None:
        return a or b
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def _merge_images(page: list[Block], blocks: list[Block]) -> list[Block]:
    """An image straddling a seam is seen by both bands: grow the upper copy instead of adding a second one."""
    out = []
    for b in blocks:
        if b.type == "image" and b.bbox:
            twin = next(
                (p for p in page if p.type == "image" and p.bbox
                 and min(p.bbox[2], b.bbox[2]) > max(p.bbox[0], b.bbox[0])
                 and b.bbox[1] <= p.bbox[3] + 0.005 and p.bbox[1] <= b.bbox[3] + 0.005),
                None,
            )
            if twin is not None:
                twin.bbox = _union(twin.bbox, b.bbox)
                continue
        out.append(b)
    return out


def stitch(bands: list[tuple[Band, list[Block]]]) -> list[Block]:
    """Join per-band blocks (bbox still band-relative) into one page-ordered list with ids b1..bN.

    Lines read twice in an overlap are kept once (from the upper band); if the two readings differ the
    block is flagged low_confidence. A block cut by a seam is merged back when its type allows.
    """
    page: list[Block] = []
    upper: Band | None = None
    for band, blocks in bands:
        blocks = [b.model_copy(update={"bbox": to_page_bbox(b.bbox, band)}, deep=True) for b in blocks]
        if page:
            seam = seam_overlap(page, blocks, upper, band)
            disagreeing = {a for a, b in seam.pairs if a != b}
            if disagreeing:
                # Two readings of the same printed line disagree: flag the kept copy.
                for bi, _, key in _units(page)[-SEAM_WINDOW:]:
                    if key in disagreeing:
                        page[bi].add_flag(FLAG_LOW_CONFIDENCE)
            if seam.suspect_prev and page:
                page[_units(page)[-1][0]].add_flag(FLAG_LOW_CONFIDENCE)
            if seam.suspect_next and _units(blocks):
                blocks[_units(blocks)[0][0]].add_flag(FLAG_LOW_CONFIDENCE)
            page = _drop_units(page, seam.drop_prev, from_end=True)
            blocks = _merge_images(page, _drop_units(blocks, seam.drop_next))
            if page and blocks and _mergeable(page[-1], blocks[0]):
                head, first = page[-1], blocks.pop(0)
                if head.rows is not None:
                    head.rows.extend(first.rows or [])
                else:
                    head.lines.extend(first.lines)
                head.bbox = _union(head.bbox, first.bbox)
                for f in first.flags:
                    head.add_flag(f)
        page.extend(blocks)
        upper = band
    for i, b in enumerate(page, 1):
        b.id = f"b{i}"
    return page
