# ABOUTME: Builds the JSON document the Typst template reads: pages, blocks, fonts, image crops and page metadata.
# ABOUTME: All text travels as JSON data (never interpolated into markup), so Typst special characters need no escaping.

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

from ..schema import Block, Master, Page
from .fonts import font_chain, lang_font, script_font

# Runs of dots, ellipses or underscores that stand for a blank to fill in ("........ नक्षत्रे").
BLANK_RE = re.compile(r"(?:\.{2,}|…+|_{3,})(?:[ \t]*(?:\.{2,}|…+|_{3,}))*")
# A list marker the source already printed: digits in any Indic script, letters, or a bullet sign.
MARKER_RE = re.compile(r"^\s*((?:[0-9०-९೦-೯౦-౯௦-௯]+|[a-zA-Z]|[ivxIVX]+)[.)]|[•●○◦▪\-–—*])\s+")

# Typographic defaults; paper switches to A4 for side-by-side so two columns have room.
STYLE = {
    "paper": "a4",  # Vedic lines are long; A4 keeps each printed line on one output line
    "size": 12.5,  # pt
    "leading": 0.25,  # em gap between ascender/descender boxes; line pitch comes to ~1.9 em
    "spacing": 1.1,  # em between paragraphs/blocks
    "margin_inside": 18,  # mm
    "margin_outside": 14,
    "margin_top": 22,
    "margin_bottom": 20,
}
STYLE_SIDE_BY_SIDE = {**STYLE, "paper": "a4", "size": 10.5, "margin_inside": 16, "margin_outside": 12}

BODY_EXCLUDED = ("running_header", "page_number")

# A cell that is only a page reference: "12", "10-11", "xiii", or empty.
PAGE_REF_RE = re.compile(r"^\s*(?:\d+(?:\s*[-–]\s*\d+)?|[ivxlcIVXLC]+)?\s*$")  # \d: digits of any script


def table_layout(rows: list[list[str]]) -> dict:
    """Column widths and style for a table: a contents list (page refs in the last column) is unruled."""
    n = max((len(r) for r in rows), default=1)
    cols = [[r[i] if i < len(r) else "" for r in rows] for i in range(n)]
    contents = n >= 2 and all(PAGE_REF_RE.match(c) for c in cols[-1]) and any(c.strip() for c in cols[-1])
    widths = ["auto" if max((len(c) for c in col), default=0) <= 8 else "1fr" for col in cols]
    if "1fr" not in widths:
        widths = ["1fr"] * n
    return {"ruled": not contents, "widths": widths, "last_right": contents}


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


def crop_image(src: Path, bbox: list[float], dest: Path) -> tuple[int, int]:
    """Crop normalized bbox [x0, y0, x1, y1] out of src into dest (PNG); returns the crop size."""
    from PIL import Image

    with Image.open(src) as im:
        w, h = im.size
        x0, y0, x1, y1 = bbox
        box = (int(x0 * w), int(y0 * h), max(int(x1 * w), int(x0 * w) + 1), max(int(y1 * h), int(y0 * h) + 1))
        crop = im.crop(box)
        if crop.mode not in ("RGB", "L", "RGBA", "LA"):
            crop = crop.convert("RGB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        crop.save(dest)
        return crop.size


def _font(block: Block, script: str) -> dict:
    sf = script_font(script) if block.is_sanskrit else lang_font(block.lang)
    return {"font": font_chain(sf.family), "lang": sf.lang, "scale": sf.scale}


def _block_data(
    block: Block, orig: Block | None, page: Page, script: str, side_by_side: bool, work_dir: Path, build_dir: Path
) -> dict:
    d: dict = {"id": block.id, "type": block.type, "align": block.align, "level": block.level or 1, **_font(block, script)}
    d["tight_above"] = d["tight_below"] = False
    d["lines"] = block.lines
    d["rows"] = block.rows or []
    if block.type == "table":
        d["table"] = table_layout(d["rows"])
    # The Devanagari original is shown only for Sanskrit text; foreign text is identical in both.
    show_orig = side_by_side and orig is not None and block.is_sanskrit and block.type not in ("image", "separator")
    d["orig"] = None
    if show_orig:
        d["orig"] = {"lines": orig.lines, "rows": orig.rows or [], **_font(orig, "devanagari")}

    if block.type == "fill_blank":
        d["segs"] = [blank_segments(line) for line in block.lines]
        if d["orig"]:
            d["orig"]["segs"] = [blank_segments(line) for line in orig.lines]
    elif block.type == "list_item":
        d["marker"], d["lines"] = split_marker(block.lines)
        if d["orig"]:
            d["orig"]["marker"], d["orig"]["lines"] = split_marker(orig.lines)
    elif block.type == "image":
        d["image"] = None
        src = find_page_image(work_dir, page)
        if src is not None and block.bbox is not None:
            rel = Path("img") / f"p{page.page:04d}-{re.sub(r'[^A-Za-z0-9_-]', '_', block.id)}.png"
            crop_image(src, block.bbox, build_dir / rel)
            d["image"] = rel.as_posix()
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
    master: Master, target: Master, *, script: str, side_by_side: bool, work_dir: Path, build_dir: Path
) -> dict:
    """The full data tree handed to book.typ as data.json."""
    main = script_font(script)
    pages = []
    for page in target.pages:
        try:
            orig_page = master.page(page.page)
            orig_blocks = {b.id: b for b in orig_page.blocks}
        except KeyError:
            orig_blocks = {}
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
        blocks = [
            _block_data(b, orig_blocks.get(b.id), page, script, side_by_side, work_dir, build_dir)
            for b in page.blocks
            if b.type not in BODY_EXCLUDED and b is not logo and not is_edge_artifact(b)
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
        "main": {"font": font_chain(main.family), "lang": main.lang},
        "latin": {"font": font_chain("Noto Serif"), "lang": "en"},
        "style": STYLE_SIDE_BY_SIDE if side_by_side else STYLE,
        "attachment": "master.json",
        "pages": pages,
    }
