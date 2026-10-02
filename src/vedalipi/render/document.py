# ABOUTME: Builds the JSON document the Typst template reads: pages, blocks, fonts, image crops and page metadata.
# ABOUTME: All text travels as JSON data (never interpolated into markup), so Typst special characters need no escaping.

from __future__ import annotations

import re
import unicodedata
from itertools import pairwise
from pathlib import Path

from ..schema import Block, Master, Page
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
}
STYLE_SIDE_BY_SIDE = {**STYLE, "size": 11.5, "margin_left": 54, "margin_right": 54}

# Vedic accents and their stand-ins: a block carrying any of them gets the wider Vedic line pitch.
SVARA_RE = re.compile("[\u0951\u0952\u1CD0-\u1CFF\uA8E0-\uA8F3]")

BODY_EXCLUDED = ("running_header", "page_number")

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


# A line that ends a printed sentence or bracketed aside ends a paragraph in prose.
SENTENCE_END_RE = re.compile(r"[।॥)\]]\s*$")


def text_measure(page: Page) -> int:
    """Characters in the page's longest prose or verse line: roughly what a full printed line holds."""
    return max((len(ln.strip()) for b in page.blocks if b.type in ("prose", "verse") for ln in b.lines), default=0)


def prose_paragraphs(lines: list[str], measure: int = 0) -> list[list[str]]:
    """Group a prose block's printed lines into paragraphs. A line ending in a danda or bracket, or
    clearly shorter than a full printed line (`measure` characters, else the block's longest line),
    ends its paragraph; only true wrapped continuation lines are joined and reflowed."""
    longest = max(measure, max((len(ln.strip()) for ln in lines), default=0))
    paras: list[list[str]] = [[]]
    for ln in lines:
        paras[-1].append(ln)
        if SENTENCE_END_RE.search(ln) or len(ln.strip()) < 0.75 * longest:
            paras.append([])
    return [p for p in paras if p]


# A line ending in a danda (or "॥ १ ॥") closes a verse or sentence; any other line break in Vedic text is
# only where the printed column ran out.
VERSE_END_RE = re.compile(r"[।॥]\s*$")
# Vedic accent marks, ignored when looking at the letter a line ends on.
TRAILING_SVARAS_RE = re.compile("[\u0951\u0952\u1CD0-\u1CFF\uA8E0-\uA8F3]+$")
# A sibilant + virama opening a line (स्त, श्च, ಸ್ತ, స్త...): the sandhi form of a visarga ending the
# previous word, so the break fell inside one written word.
SANDHI_START_RE = re.compile("^[\u0936-\u0938\u0CB6-\u0CB8\u0C36-\u0C38][\u094D\u0CCD\u0C4D]")


def _joins_mid_word(prev: str, nxt: str) -> bool:
    """Whether a printed line break between `prev` and `nxt` splits a written word: the next line opens
    on a combining sign, or on a sandhi'd visarga after a line ending in a bare vowel or letter."""
    if not nxt:
        return False
    if unicodedata.category(nxt[0]).startswith("M"):
        return True
    tail = TRAILING_SVARAS_RE.sub("", prev.rstrip())
    if not tail or not SANDHI_START_RE.match(nxt):
        return False
    name = unicodedata.name(tail[-1], "")
    return ("VOWEL SIGN" in name or "LETTER" in name) and "VIRAMA" not in name


def vedic_paragraphs(lines: list[str]) -> list[str]:
    """Reflow svara-marked text: consecutive printed lines are joined until one ends in । or ॥, so only
    real verse and sentence ends break the line. A break inside a word (a trailing hyphen, or one of
    the cases `_joins_mid_word` detects) joins with no space."""
    paras: list[str] = []
    cur = ""
    for raw in lines:
        ln = raw.strip()
        if not ln:
            continue
        if not cur:
            cur = ln
        elif cur.endswith("-"):
            cur = cur[:-1] + ln
        else:
            cur += ("" if _joins_mid_word(cur, ln) else " ") + ln
        if VERSE_END_RE.search(cur):
            paras.append(cur)
            cur = ""
    if cur:
        paras.append(cur)
    return paras


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
    return {"font": family_chain(sf.family), "lang": sf.lang, "scale": sf.scale}


def family_chain(family: str) -> list[str]:
    """Font fallback list for a family, led by its svara-emphasised copy when one is derived (see svarafont)."""
    chain = font_chain(family)
    return [derived_family(family), *chain] if has_derived(family) else chain


def _block_data(
    block: Block, orig: Block | None, page: Page, script: str, side_by_side: bool, work_dir: Path, build_dir: Path,
    index: int, measure: int = 0,
) -> dict:
    d: dict = {"id": block.id, "type": block.type, "align": block.align, "level": block.level or 1, **_font(block, script)}
    d["tight_above"] = d["tight_below"] = False
    d["lines"] = block.lines
    d["rows"] = block.rows or []
    d["vedic"] = any(SVARA_RE.search(t) for t in [*block.lines, *(c for r in d["rows"] for c in r)])
    if block.type == "table":
        d["table"] = table_layout(d["rows"])
    # The Devanagari original is shown only for Sanskrit text; foreign text is identical in both.
    show_orig = side_by_side and orig is not None and block.is_sanskrit and block.type not in ("image", "separator")
    d["orig"] = None
    if show_orig:
        d["orig"] = {"lines": orig.lines, "rows": orig.rows or [], **_font(orig, "devanagari")}

    if block.type == "prose":
        d["paras"] = prose_paragraphs(block.lines, measure)
        if d["orig"]:
            d["orig"]["paras"] = prose_paragraphs(orig.lines, measure)
    if block.type == "verse" and d["vedic"]:
        # Vedic text printed as run-on lines flows as paragraphs; plain verse keeps its printed lines.
        d["flow"] = vedic_paragraphs(block.lines)
        if d["orig"]:
            d["orig"]["flow"] = vedic_paragraphs(orig.lines)
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
            # Named by position, not id: ids like "a/b" and "a?b" would sanitize to the same file.
            rel = Path("img") / f"p{page.page:04d}-{index:03d}.png"
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
            _block_data(b, orig_blocks.get(b.id), page, script, side_by_side, work_dir, build_dir, i, text_measure(page))
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
        "style": STYLE_SIDE_BY_SIDE if side_by_side else STYLE,
        "attachment": "master.json",
        "pages": pages,
    }
