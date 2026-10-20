# ABOUTME: Builds the JSON document the Typst template reads: pages, blocks, fonts, image crops and page metadata.
# ABOUTME: All text travels as JSON data (never interpolated into markup), so Typst special characters need no escaping.

from __future__ import annotations

import math
import re
from itertools import pairwise
from pathlib import Path

from ..schema import Block, BlockTranslation, Label, Master, Page
from .fonts import font_chain, lang_font, script_font
from .svarafont import derived_family, has_derived

# Runs of dots, ellipses or underscores that stand for a blank to fill in ("........ नक्षत्रे").
BLANK_RE = re.compile(r"(?:\.{2,}|…+|_{3,})(?:[ \t]*(?:\.{2,}|…+|_{3,}))*")
# A list marker the source already printed: digits in any Indic script, letters, or a bullet sign.
MARKER_RE = re.compile(r"^\s*((?:[0-9०-९೦-೯౦-౯௦-௯]+|[a-zA-Z]|[ivxIVX]+)[.)]|[•●○◦▪\-–—*])\s+")

# Typographic defaults, after prapatti.com's XeTeX stotra PDFs: US Letter, 1.1in side margins, 16.74pt body,
# baselines 1.42 em apart for plain verse and 1.81 em for svara-marked (Vedic) text, ~11pt extra between verses.
STYLE = {
    "paper": "us-letter",
    "size": 16.74,  # pt
    "line_box": [1.0, 0.42],  # em above / below the baseline that every line box spans (pitch 1.42 em)
    "leading": 0.0,  # em added between line boxes in plain text: pitch 1.42 em
    "leading_vedic": 0.55,  # em added for svara-marked text: pitch ~1.97 em, room for marks above and below
    "verse_gap": 0.68,  # em of extra space between blocks (11.35pt at 16.74pt)
    "margin_left": 79.2,  # pt
    "margin_right": 79.2,
    "margin_top": 80,
    "margin_bottom": 76,
    # Blocks are never set smaller than the body size: a printed line too wide for the page wraps with a
    # hanging indent instead, so the whole book reads at one size.
    "min_size": 16.74,  # pt
}
STYLE_SIDE_BY_SIDE = {**STYLE, "size": 11.5, "min_size": 8.25, "margin_left": 54, "margin_right": 54}
# A translated edition reads like a book in its own language: body size of a printed English manual, and
# romanized mantra lines (longer than their Indic originals) may shrink a little before they wrap.
STYLE_TRANSLATED = {**STYLE, "size": 13, "min_size": 11}

# Vedic accents and their stand-ins: a block carrying any of them gets the wider Vedic line pitch.
SVARA_RE = re.compile("[\u0951\u0952\u1CD0-\u1CFF\uA8E0-\uA8F3\u030D\u030E\u0331]")  # last three: roman svaras

BODY_EXCLUDED = ("running_header", "page_number")

# A verse number closing a line ("... word ॥ १२॥"), in any Indic or Latin digits, with the word before it.
VERSE_END_RE = re.compile(r"\S+\s+(?:[।॥]\s*)*[0-9०-९೦-೯౦-౯௦-௯]+\s*[।॥]+\s*$")
WORD_JOINER = "\u2060"


def keep_verse_number(line: str) -> str:
    """Glue a closing verse number to the word before it, so a wrapped line never leaves "॥ १२॥" alone.

    Spaces in that tail become no-break spaces and a word joiner follows each danda (UAX 14 allows a break
    after a danda even without a space).
    """
    m = VERSE_END_RE.search(line)
    if not m:
        return line
    tail = re.sub(r"\s", " ", m.group(0).rstrip())
    tail = re.sub(r"([।॥])(?=.)", r"\1" + WORD_JOINER, tail)
    return line[: m.start()] + tail

# Scan pixels painted white around a drawing's label beyond its bbox, so anti-aliased glyph edges go too.
LABEL_PAD = 3
# Gray level below which a scan pixel counts as ink, when telling the drawing's strokes from the label's glyphs.
LABEL_INK = 200

# A cell that is only a page reference: "12", "10-11", "xiii", or empty.
PAGE_REF_RE = re.compile(r"^\s*(?:\d+(?:\s*[-–]\s*\d+)?|[ivxlcIVXLC]+)?\s*$")  # \d: digits of any script


def is_contents(rows: list[list[str]]) -> bool:
    """A contents list: at least two columns and page references (or nothing) in the last one."""
    n = max((len(r) for r in rows), default=1)
    last = [r[n - 1] if n - 1 < len(r) else "" for r in rows]
    return n >= 2 and all(PAGE_REF_RE.match(c) for c in last) and any(c.strip() for c in last)


def table_layout(rows: list[list[str]]) -> dict:
    """Column widths and style for a table: a contents list (page refs in the last column) is unruled;
    any other table is boxed, with its first row set as a header."""
    n = max((len(r) for r in rows), default=1)
    cols = [[r[i] if i < len(r) else "" for r in rows] for i in range(n)]
    contents = is_contents(rows)
    widths = ["auto" if max((len(c) for c in col), default=0) <= 8 else "1fr" for col in cols]
    if "1fr" not in widths:
        widths = ["1fr"] * n
    return {"ruled": not contents, "widths": widths, "last_right": contents, "header": not contents and len(rows) > 1}


def merge_contents(blocks: list[Block]) -> list[Block]:
    """Rejoin one printed contents list that OCR split up (per band, or English and Sanskrit parts, or
    sub-entries read as list items): adjacent contents tables with the same column count become one
    table, and list items next to a contents table become its bullet sub-entry rows. Text is unchanged.
    Only the layout is unified, so the target and the Devanagari original merge the same way."""
    out: list[Block] = []
    pending: list[Block] = []  # list items waiting for a contents table to join

    def ncols(b: Block) -> int:
        return max((len(r) for r in b.rows or []), default=0)

    def bullet_rows(items: list[Block], n: int) -> list[list[str]]:
        return [["", "• " + " ".join(i.lines).strip(), *[""] * (n - 2)] for i in items]

    for b in blocks:
        is_toc = b.type == "table" and b.rows and ncols(b) >= 3 and is_contents(b.rows)
        prev = out[-1] if out else None
        prev_toc = prev is not None and prev.type == "table" and ncols(prev) >= 3 and is_contents(prev.rows or [])
        if b.type == "list_item" and prev_toc and not pending:
            prev.rows = [*prev.rows, *bullet_rows([b], ncols(prev))]
            continue
        if b.type == "list_item":
            pending.append(b)
            continue
        if is_toc and pending:
            b = b.model_copy(update={"rows": [*bullet_rows(pending, ncols(b)), *b.rows]}, deep=True)
            pending = []
        out.extend(pending)
        pending = []
        if is_toc and prev_toc and ncols(prev) == ncols(b):
            prev.rows = [*prev.rows, *b.rows]
            # Mixed English/Sanskrit contents: Sanskrit wins so the target script is the primary font.
            if b.is_sanskrit:
                prev.lang = b.lang
            continue
        out.append(b.model_copy(deep=True) if is_toc else b)
    out.extend(pending)
    return out


def is_edge_artifact(block: Block) -> bool:
    """A thin "image" hugging the page edge is the scan's dark border or binding shadow, not content."""
    if block.type != "image" or not block.bbox:
        return False
    x0, y0, x1, y1 = block.bbox
    return x1 - x0 < 0.1 and (x0 < 0.03 or x1 > 0.97) and y1 - y0 > 3 * (x1 - x0)


def header_logo(page: Page) -> Block | None:
    """An image beside the running header (the book's logo) belongs in the page header, not the body."""
    hdr = [b for b in page.blocks if b.type == "running_header" and b.bbox]
    if not hdr:
        return None
    limit = max(b.bbox[3] for b in hdr) + 0.04
    for b in page.blocks:
        if b.type == "image" and b.bbox and b.bbox[1] < 0.15 and b.bbox[3] <= limit and not is_edge_artifact(b):
            return b
    return None


def blank_segments(line: str) -> list[dict]:
    """Split a fill-in line into text segments and blanks; a blank's width follows the dot count."""
    segs: list[dict] = []
    pos = 0
    for m in BLANK_RE.finditer(line):
        if m.start() > pos:
            segs.append({"t": line[pos : m.start()]})
        n = len(m.group(0).replace(" ", "")) * (3 if "…" in m.group(0) else 1)
        segs.append({"blank": max(3.0, min(12.0, n * 0.45))})
        pos = m.end()
    if pos < len(line):
        segs.append({"t": line[pos:]})
    return segs


def split_marker(lines: list[str]) -> tuple[str, list[str]]:
    """Separate a printed list marker from the first line; default to a bullet when the source had none."""
    if not lines:
        return "•", lines
    m = MARKER_RE.match(lines[0])
    if not m:
        return "•", lines
    return m.group(1), [lines[0][m.end() :], *lines[1:]]


def find_page_image(work_dir: Path, page: Page) -> Path | None:
    """The page scan to crop images from: pre.png, then source.png, then the page's recorded source_image."""
    d = Path(work_dir) / "pages" / f"p{page.page:04d}"
    candidates = [d / "pre.png", d / "source.png"]
    if page.source_image:
        candidates.append(Path(page.source_image))
    return next((c for c in candidates if c.is_file()), None)


def crop_image(src: Path, bbox: list[float], dest: Path, blank: list[list[float]] = ()) -> tuple[int, int]:
    """Crop normalized bbox [x0, y0, x1, y1] out of src into dest (PNG); returns the crop size.

    Each normalized page bbox in `blank` (a label's printed text) is painted white first, grown by
    LABEL_PAD pixels so no edge of the old glyphs remains, and never more than that; drawing strokes that
    pass through that padding are kept (see _whiten_label).
    """
    from PIL import Image

    with Image.open(src) as im:
        w, h = im.size
        x0, y0, x1, y1 = bbox
        box = (int(x0 * w), int(y0 * h), max(int(x1 * w), int(x0 * w) + 1), max(int(y1 * h), int(y0 * h) + 1))
        crop = im.crop(box)
        if crop.mode not in ("RGB", "L", "RGBA", "LA"):
            crop = crop.convert("RGB")
        if blank:
            for lx0, ly0, lx1, ly1 in blank:
                # Pixel edges of the label's ink, outward-rounded, in crop pixels.
                ink = (math.floor(lx0 * w) - box[0], math.floor(ly0 * h) - box[1],
                       math.ceil(lx1 * w) - box[0], math.ceil(ly1 * h) - box[1])
                _whiten_label(crop, ink)
        dest.parent.mkdir(parents=True, exist_ok=True)
        crop.save(dest)
        return crop.size


def _whiten_label(crop, ink: tuple[int, int, int, int]) -> None:
    """Paint a label's ink box white, plus a LABEL_PAD ring around it for the glyphs' anti-aliased edges.

    Ink in the ring that runs on beyond the ring (a stroke or grid line of the drawing passing close to the
    label) is part of the drawing, not the label, and is kept.
    """
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    cw, ch = crop.size
    ix0, iy0, ix1, iy1 = ink
    px0, py0, px1, py1 = ix0 - LABEL_PAD, iy0 - LABEL_PAD, ix1 + LABEL_PAD, iy1 + LABEL_PAD
    # The padded box and a one-pixel frame outside it, clipped to the crop.
    ex0, ey0, ex1, ey1 = max(px0 - 1, 0), max(py0 - 1, 0), min(px1 + 1, cw), min(py1 + 1, ch)
    if ex0 >= ex1 or ey0 >= ey1:
        return
    region = np.asarray(crop.crop((ex0, ey0, ex1, ey1)).convert("L"))
    dark = (region < LABEL_INK).astype(np.uint8)
    dark[max(iy0 - ey0, 0):max(iy1 - ey0, 0), max(ix0 - ex0, 0):max(ix1 - ex0, 0)] = 0  # the label itself goes
    ys, xs = np.mgrid[ey0:ey1, ex0:ex1]
    outside = (xs < px0) | (xs >= px1) | (ys < py0) | (ys >= py1)
    _, comp = cv2.connectedComponents(dark, connectivity=8)
    drawing = np.isin(comp, np.unique(comp[outside & (dark > 0)])) & (dark > 0)
    keep = Image.fromarray((drawing * 255).astype(np.uint8))
    original = crop.crop((ex0, ey0, ex1, ey1))
    ImageDraw.Draw(crop).rectangle((px0, py0, px1 - 1, py1 - 1), fill="white")
    crop.paste(original, (ex0, ey0), keep)


def label_rooms(crop_png: Path, bbox: list[float], label_bboxes: list[list[float]]) -> list[tuple[float, float, float, float]]:
    """How far each label may grow beyond its printed box: (left, top, right, bottom) as fractions of the image.

    Each label's box is grown a pixel at a time on every side while the grown box stays clear of the
    drawing's ink in the whitened crop and of the boxes already given to other labels, by at most the
    label's own width sideways and its own height up or down; a small margin is then kept from that ink.
    A script wider or taller than the printed one is then not shrunk to fit a box sized for its glyphs.
    """
    import numpy as np
    from PIL import Image

    with Image.open(crop_png) as im:
        cw, ch = im.size
        blocked = np.asarray(im.convert("L")) < LABEL_INK
    x0, y0, x1, y1 = bbox
    w, h = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
    boxes = []
    for lx0, ly0, lx1, ly1 in label_bboxes:
        ix0, ix1 = max(int((lx0 - x0) / w * cw), 0), min(int(math.ceil((lx1 - x0) / w * cw)), cw)
        iy0, iy1 = max(int((ly0 - y0) / h * ch), 0), min(int(math.ceil((ly1 - y0) / h * ch)), ch)
        boxes.append((ix0, iy0, max(ix1, ix0 + 1), max(iy1, iy0 + 1)))
    for bx in boxes:  # every printed label is reserved for its own label
        blocked[bx[1]:bx[3], bx[0]:bx[2]] = True
    out = []
    for i, (ix0, iy0, ix1, iy1) in enumerate(boxes):
        blocked[iy0:iy1, ix0:ix1] = False
        lw, lh = ix1 - ix0, iy1 - iy0
        margin = max(LABEL_PAD * 2, lh // 4)
        caps = (lw + margin, lh + margin, lw + margin, lh + margin)
        g = [0, 0, 0, 0]  # left, top, right, bottom
        grown = True
        while grown:
            grown = False
            for side in range(4):
                if g[side] >= caps[side]:
                    continue
                a0, b0, a1, b1 = ix0 - g[0], iy0 - g[1], ix1 + g[2], iy1 + g[3]
                strip = {0: (a0 - 1, b0, a0, b1), 1: (a0, b0 - 1, a1, b0), 2: (a1, b0, a1 + 1, b1), 3: (a0, b1, a1, b1 + 1)}[side]
                sx0, sy0, sx1, sy1 = strip
                if sx0 < 0 or sy0 < 0 or sx1 > cw or sy1 > ch or blocked[sy0:sy1, sx0:sx1].any():
                    continue
                g[side] += 1
                grown = True
        g = [max(0, v - margin) for v in g]
        # The grown box is now taken: later labels may not grow into it.
        blocked[iy0 - g[1]:iy1 + g[3], ix0 - g[0]:ix1 + g[2]] = True
        out.append((g[0] / cw, g[1] / ch, g[2] / cw, g[3] / ch))
    return out

def _font(block: Block | Label, script: str) -> dict:
    sf = script_font(script) if block.is_sanskrit else lang_font(block.lang)
    return {"font": family_chain(sf.family), "lang": sf.lang, "scale": sf.scale}


def family_chain(family: str) -> list[str]:
    """Font fallback list for a family, led by its svara-emphasised copy when one is derived (see svarafont)."""
    chain = font_chain(family)
    return [derived_family(family), *chain] if has_derived(family) else chain


def _label_data(
    label: Label, bbox: list[float], script: str, grow: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
) -> dict:
    """A label's text and font, and its box as fractions of the image block it sits in.

    `grow` (left, top, right, bottom fractions of the image, see label_rooms) widens the box into free space.
    """
    gl, gt, gr, gb = grow
    x0, y0, x1, y1 = bbox
    w, h = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
    lx0, ly0, lx1, ly1 = label.bbox
    box = {"x": (lx0 - x0) / w - gl, "y": (ly0 - y0) / h - gt, "w": (lx1 - lx0) / w + gl + gr, "h": (ly1 - ly0) / h + gt + gb}
    return {"text": label.text, **{k: round(v, 5) for k, v in box.items()}, **_font(label, script)}


def _block_data(
    block: Block, orig: Block | None, page: Page, script: str, side_by_side: bool, work_dir: Path, build_dir: Path,
    index: int, meaning: BlockTranslation | None = None, meaning_lang: str = "en", translated: bool = False,
) -> dict:
    d: dict = {"id": block.id, "type": block.type, "align": block.align, "level": block.level or 1, **_font(block, script)}
    d["tight_above"] = d["tight_below"] = False
    d["lines"] = [keep_verse_number(line) for line in block.lines]
    d["rows"] = block.rows or []
    d["vedic"] = any(SVARA_RE.search(t) for t in [*block.lines, *(c for r in d["rows"] for c in r)])
    if block.type == "table":
        d["table"] = table_layout(d["rows"])
    # The Devanagari original is shown only for Sanskrit text; foreign text is identical in both.
    show_orig = side_by_side and orig is not None and block.is_sanskrit and block.type not in ("image", "separator")
    d["orig"] = None
    if show_orig:
        d["orig"] = {"lines": orig.lines, "rows": orig.rows or [], **_font(orig, "devanagari")}

    # In a translated edition, text in the translation's language is new prose: it reflows freely.
    d["reflow"] = translated and not block.is_sanskrit
    # A Sanskrit block of a translated edition carries its meaning (and word gloss), set under the text.
    d["meaning"] = None
    if meaning is not None and (meaning.lines or meaning.gloss):
        lf = lang_font(meaning_lang)
        d["meaning"] = {"lines": meaning.lines, "gloss": meaning.gloss or [],
                        "font": family_chain(lf.family), "lang": lf.lang, "scale": lf.scale}

    if block.type == "fill_blank":
        d["segs"] = [blank_segments(line) for line in block.lines]
        if d["orig"]:
            d["orig"]["segs"] = [blank_segments(line) for line in orig.lines]
    elif block.type == "list_item":
        d["marker"], d["lines"] = split_marker(d["lines"])
        if d["orig"]:
            d["orig"]["marker"], d["orig"]["lines"] = split_marker(orig.lines)
    elif block.type == "image":
        d["image"] = None
        d["labels"] = []
        src = find_page_image(work_dir, page)
        labels = block.labels or []
        if src is not None and block.bbox is not None:
            # Named by position, not id: ids like "a/b" and "a?b" would sanitize to the same file.
            rel = Path("img") / f"p{page.page:04d}-{index:03d}.png"
            cw, ch = crop_image(src, block.bbox, build_dir / rel, blank=[lab.bbox for lab in labels])
            d["image"] = rel.as_posix()
            d["aspect"] = ch / cw
            rooms = label_rooms(build_dir / rel, block.bbox, [lab.bbox for lab in labels])
            d["labels"] = [_label_data(lab, block.bbox, script, room) for lab, room in zip(labels, rooms)]
        x0, _, x1, _ = block.bbox or (0.0, 0.0, 1.0, 1.0)
        # The text block of a scan is ~75% of the page width; scale crops to the same share of ours.
        d["width"] = round(min(1.0, max(0.15, (x1 - x0) / 0.75)), 3)
    return d


def _printed_number(page: Page) -> str | None:
    for b in page.blocks:
        if b.type == "page_number":
            text = " ".join(line.strip() for line in b.lines).strip()
            if text:
                return text
    return None


def build_document(
    master: Master, target: Master, *, script: str, side_by_side: bool, work_dir: Path, build_dir: Path,
    meanings: dict[tuple[int, str], BlockTranslation] | None = None, meaning_lang: str = "en", translated: bool = False,
) -> dict:
    """The full data tree handed to book.typ as data.json.

    `translated` marks a translated edition; `meanings` maps (page, block id) of its Sanskrit blocks to
    their translation."""
    meanings = meanings or {}
    main = script_font(script)
    pages = []
    for page in target.pages:
        header = [
            {"text": " ".join(b.lines), "align": b.align, **_font(b, script)}
            for b in page.blocks
            if b.type == "running_header" and any(line.strip() for line in b.lines)
        ]
        logo = header_logo(page)
        logo_path = None
        if logo is not None and (src := find_page_image(work_dir, page)) is not None:
            rel = Path("img") / f"p{page.page:04d}-logo.png"
            crop_image(src, logo.bbox, build_dir / rel)
            logo_path = rel.as_posix()
        body = [b for b in page.blocks if b.type not in BODY_EXCLUDED and b is not logo and not is_edge_artifact(b)]
        ids = {b.id for b in body}
        try:
            # The original is merged over the same blocks so merged ids line up.
            orig_blocks = {b.id: b for b in merge_contents([b for b in master.page(page.page).blocks if b.id in ids])}
        except KeyError:
            orig_blocks = {}
        blocks = [
            _block_data(b, orig_blocks.get(b.id), page, script, side_by_side, work_dir, build_dir, i,
                        meanings.get((page.page, b.id)), meaning_lang, translated)
            for i, b in enumerate(merge_contents(body), 1)
        ]
        # Consecutive list items sit closer together, like a printed list.
        for prev, cur in pairwise(blocks):
            if prev["type"] == cur["type"] == "list_item":
                prev["tight_below"] = cur["tight_above"] = True
        pages.append({"page": page.page, "printed": _printed_number(page), "header": header, "logo": logo_path, "blocks": blocks})
    return {
        "title": target.title or master.title or "",
        "script": script,
        "side_by_side": side_by_side,
        "main": {"font": family_chain(main.family), "lang": main.lang},
        "latin": {"font": font_chain("Noto Serif"), "lang": "en"},
        "style": STYLE_SIDE_BY_SIDE if side_by_side else STYLE_TRANSLATED if translated else STYLE,
        "attachment": "master.json",
        "pages": pages,
    }
