# ABOUTME: Judge: a read-only, deterministic check that a rendered PDF holds every line, cell, label and image of master.json.
# ABOUTME: Reads the PDF text layer honouring /ActualText, searches each source page in reading order, writes work/judge-<script>.json.

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import pipeline, transliterate
from .render.document import (
    BLANK_RE,
    BODY_EXCLUDED,
    STYLE,
    header_logo,
    is_edge_artifact,
)
from .render.textlayer import _glyphs, _mul, _page_fonts, _text_string, _tokenize
from .schema import Manifest, Master, Page, atomic_write_text

# Whitespace, ZWNJ, ZWJ and soft hyphens carry no content: a wrapped or re-spaced line still matches.
_DROP = re.compile("[\\s\u200c\u200d\u00ad\u2060]+")
# The template's source page labels: a mid-page divider "src p. 4", or a footer "src p. 4" / "src pp. 3–4".
_MARKER = re.compile(r"srcp(p?)\.(\d+)(?:[–-](\d+))?")
_PLACEHOLDER = "imagenotavailable"  # book.typ's box for an image block whose crop is missing


def norm(text: str) -> str:
    """Comparison form of a string: NFC with all whitespace, ZWJ/ZWNJ, word joiners and soft hyphens removed.

    book.typ never hyphenates (`hyphenate: false` for all text, Latin included) and never justifies, so a
    long line only wraps: the break is whitespace and drops out here.
    """
    return _DROP.sub("", unicodedata.normalize("NFC", text))


# ---------------------------------------------------------------- reading the PDF


@dataclass
class Run:
    """One text object (BT..ET) or one placed image of an output page, in content-stream order."""

    kind: str  # "text" or "image"
    y: float  # distance from the top of the page: the text origin, or the image's vertical centre
    text: str = ""


def _page_runs(doc, page) -> list[Run]:
    """Text objects and images of one page, in content-stream (reading) order.

    MuPDF's own extraction repeats characters of an /ActualText span whose glyphs are split over several
    strings (a Typst cluster with a kerning adjustment comes out as "ವಾಃಃ" or "ಸ್ವ್ವ"), so the text layer
    is read here directly: an /ActualText span contributes its text once and its glyphs nothing; any other
    glyph contributes its ToUnicode text. Typst writes Identity-H fonts and puts all text in page streams.

    Typst can also split one cluster over two text objects (Devanagari "ई" as the glyph of "इ" in one, its
    top stroke in the next), each piece carrying the cluster's whole text, as /ActualText or as the glyph's
    ToUnicode. A piece that opens a text object right after a piece with the same text on the same baseline,
    and draws none of its glyphs, is the rest of that cluster and contributes nothing. A genuinely repeated
    cluster draws the same glyphs again, so it still counts.
    """
    fonts = _page_fonts(doc, page)
    images = {img[7] for img in page.get_images(full=True)}
    height = page.rect.height
    runs: list[Run] = []
    ctm, ctm_stack, tm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), [], (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    cmap: dict[int, str] = {}
    spans: list[list] = []  # marked-content stack: [actual text or None, already emitted]
    last: list | None = None  # the cluster just emitted: [text, glyph codes, baseline]
    fresh = False  # no glyph shown yet in this text object
    current: Run | None = None

    def emit(text: str, codes: set, y: float) -> None:
        nonlocal last
        if (fresh and last is not None and text and last[0] == text and abs(last[2] - y) < 1.0
                and codes and not codes & last[1]):
            last[1] |= codes  # the rest of a cluster split over two text objects
        else:
            current.text += text
            last = [text, codes, y]

    for xref in page.get_contents():
        operands: list[tuple[str, bytes]] = []
        for kind, raw in _tokenize(doc.xref_stream(xref)):
            if kind != "op":
                operands.append((kind, raw))
                continue
            nums = [float(r) for k, r in operands if k == "num"]
            if raw == b"q":
                ctm_stack.append(ctm)
            elif raw == b"Q":
                ctm = ctm_stack.pop() if ctm_stack else ctm
            elif raw == b"cm" and len(nums) == 6:
                ctm = _mul(tuple(nums), ctm)
            elif raw == b"BT":
                tm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
                current, fresh = None, True
            elif raw == b"ET":
                current = None
            elif raw == b"Tm" and len(nums) == 6:
                tm = tuple(nums)
            elif raw in (b"Td", b"TD") and len(nums) == 2:
                tm = _mul((1.0, 0.0, 0.0, 1.0, nums[0], nums[1]), tm)
            elif raw == b"Tf":
                names = [r for k, r in operands if k == "name"]
                cmap = fonts.get(names[-1][1:].decode(), {}) if names else {}
            elif raw == b"BDC":
                vals = [r for _, r in operands]
                actual = None
                if b"/ActualText" in vals and vals.index(b"/ActualText") + 1 < len(operands):
                    k, v = operands[vals.index(b"/ActualText") + 1]
                    actual = _text_string(k, v) if k in ("hex", "str") else ""
                spans.append([actual, False])
            elif raw == b"BMC":
                spans.append([None, False])
            elif raw == b"EMC":
                if spans:
                    spans.pop()
            elif raw in (b"Tj", b"TJ", b"'", b'"'):
                if current is None:
                    current = Run("text", height - _mul(tm, ctm)[5])
                    runs.append(current)
                outer = next((s for s in reversed(spans) if s[0] is not None), None)
                shown = [c for k, r in operands if k in ("str", "hex") for c in _glyphs(r)]
                y = _mul(tm, ctm)[5]
                if outer is not None:
                    if not outer[1]:
                        emit(outer[0], set(shown), y)
                        outer[1] = True
                    elif last is not None:
                        last[1].update(shown)
                else:
                    for c in shown:
                        emit(cmap.get(int.from_bytes(c, "big"), ""), {c}, y)
                        fresh = False
                fresh = fresh and not shown
            elif raw == b"Do":
                names = [r[1:].decode() for k, r in operands if k == "name"]
                if names and names[-1] in images:
                    # An image is drawn in the unit square scaled by the CTM: d is its height, f its bottom.
                    runs.append(Run("image", height - (ctm[5] + ctm[3] / 2)))
            operands = []
    return runs


@dataclass
class OutputPage:
    number: int  # 1-based output page
    header: str = ""  # normalized text above the top margin
    header_images: int = 0
    footer: str = ""  # normalized text below the bottom margin
    body: list[Run] = field(default_factory=list)


def read_pdf(pdf: Path) -> list[OutputPage]:
    """Every output page split into header, body and footer by the template's margins."""
    import pymupdf

    out: list[OutputPage] = []
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc, 1):
            op = OutputPage(i)
            top, bottom = STYLE["margin_top"], page.rect.height - STYLE["margin_bottom"]
            for r in _page_runs(doc, page):
                if r.y < top:
                    op.header += norm(r.text)
                    op.header_images += r.kind == "image"
                elif r.y > bottom:
                    op.footer += norm(r.text)
                else:
                    op.body.append(r)
            out.append(op)
    return out


# ---------------------------------------------------------------- the check


@dataclass
class Book:
    """The PDF body cut into source pages at the "src p. N" dividers."""

    text: dict[int, str] = field(default_factory=dict)  # source page -> normalized body text, in order
    events: dict[int, list[str]] = field(default_factory=dict)  # source page -> "image"/"placeholder" in order
    outputs: dict[int, set[int]] = field(default_factory=dict)  # source page -> output pages with its body
    header_source: dict[int, int] = field(default_factory=dict)  # output page -> source page in its header
    marked: set[int] = field(default_factory=set)  # source pages named by any divider or footer label


def segment(pages: list[OutputPage], first: int | None) -> Book:
    """Attribute body text and images to source pages. The first source page carries no divider (book.typ
    omits it), so the body starts in `first`; each divider switches to its page."""
    book = Book()
    cur = first
    for op in pages:
        for m in _MARKER.finditer(op.footer):
            # "src pp. A–B" names only the first and last source page on this output page (book.typ
            # page-footer), not every page between: the ones between are proven by their own dividers.
            a = int(m.group(2))
            book.marked.add(a)
            if m.group(3):
                book.marked.add(int(m.group(3)))
            book.header_source.setdefault(op.number, a)
        for r in op.body:
            t = norm(r.text)
            m = _MARKER.fullmatch(t) if r.kind == "text" else None
            if m and not m.group(1) and not m.group(3):
                cur = int(m.group(2))
                book.marked.add(cur)
                continue
            if cur is None:
                continue
            book.outputs.setdefault(cur, set()).add(op.number)
            if r.kind == "image":
                book.events.setdefault(cur, []).append("image")
            else:
                if t == _PLACEHOLDER:
                    book.events.setdefault(cur, []).append("placeholder")
                book.text[cur] = book.text.get(cur, "") + t
    return book


def _item(page: int, block: str, kind: str, text: str, **extra) -> dict:
    return {"page": page, "block": block, "kind": kind, "text": text, **extra}


def _text_items(page: Page, replaced: list[dict], meanings: dict | None = None) -> list[tuple[str, str, str]]:
    """(block id, kind, text) of everything book.typ typesets in the body of this page, in source order;
    what the template deliberately leaves out is appended to `replaced` with the reason. `meanings` (a
    translated edition) adds each Sanskrit block's meaning lines and gloss right after the block."""
    items: list[tuple[str, str, str]] = []
    for b in page.blocks:
        _block_items(page, b, items, replaced)
        bt = (meanings or {}).get((page.page, b.id))
        if bt is not None and bt.cue:
            # The cue is printed before the mantra: it goes ahead of the block's own lines.
            at = next((i for i, it in enumerate(items) if it[0] == b.id), len(items))
            items.insert(at, (b.id, "meaning", bt.cue))
        if bt is not None:
            items.extend((b.id, "meaning", line) for line in bt.lines)
            for word, meaning in bt.gloss or []:
                items.extend([(b.id, "gloss", word), (b.id, "gloss", meaning)])
    return items


def _block_items(page: Page, b, items: list[tuple[str, str, str]], replaced: list[dict]) -> None:
    """Append one block's typeset text to `items` (see _text_items)."""
    if b.type == "page_number":
        replaced.append(_item(page.page, b.id, "page_number", " ".join(b.lines),
                              reason="printed source numbers are left out; the footer numbers output pages "
                                     "and names the source page as \"src p. N\""))
    elif b.type == "running_header":
        return  # checked against the page header (see _check_header)
    elif b.type in ("image", "separator"):
        if is_edge_artifact(b):
            replaced.append(_item(page.page, b.id, "image", "", reason="thin image at the page edge: the scan's "
                                  "border or binding shadow, not content (render.document.is_edge_artifact)"))
        if any(line.strip() for line in b.lines):
            replaced.append(_item(page.page, b.id, b.type, " ".join(b.lines),
                                  reason=f"a {b.type} block is drawn, its text is not typeset"))
        # A drawing's labels are typeset over it in the target script (book.typ label-box).
        if not is_edge_artifact(b):
            for label in b.labels or []:
                items.append((b.id, "label", label.text))
    elif b.type == "fill_blank":
        for line in b.lines:
            items.append((b.id, "line", BLANK_RE.sub("", line)))
        if any(BLANK_RE.search(line) for line in b.lines):
            replaced.append(_item(page.page, b.id, "fill_blank", " / ".join(b.lines),
                                  reason="dot and underscore leaders are drawn as dotted rules, not text"))
    elif b.type == "table":
        for row in b.rows or []:
            for cell in row:
                items.append((b.id, "cell", cell))
    else:
        # Headings, verse, prose, foreign text and list items (book.typ splits off a printed list marker
        # into its own column and adds "•" when there is none; the text itself is unchanged).
        for line in b.lines:
            items.append((b.id, "line", line))


def _align(seg: str, needles: list[str]) -> dict[int, tuple[int, int]]:
    """The most needles found in `seg` in their given order on non-overlapping spans (ties: the most text);
    returns needle index -> (start, end). A plain left-to-right search would let a short line latch on to
    a later copy of itself and push every line after it out of order."""
    size = len(seg) + 1
    tree: list[tuple[tuple[int, int], int]] = [((0, 0), -1)] * (size + 1)  # Fenwick prefix max over span ends

    def best_before(pos: int) -> tuple[tuple[int, int], int]:
        out, i = ((0, 0), -1), pos + 1
        while i > 0:
            out, i = max(out, tree[i]), i - (i & -i)
        return out

    def put(pos: int, value: tuple[tuple[int, int], int]) -> None:
        i = pos + 1
        while i <= size:
            tree[i], i = max(tree[i], value), i + (i & -i)

    chosen: list[tuple[int, int, int, int]] = []  # (needle, start, end, previous choice)
    top: tuple[tuple[int, int], int] = ((0, 0), -1)
    for k, needle in enumerate(needles):
        found, at = [], seg.find(needle)
        while at >= 0:
            (count, chars), prev = best_before(at)
            found.append(((count + 1, chars + len(needle)), at, prev))
            at = seg.find(needle, at + 1)
        for score, at, prev in found:  # all of one needle's spans are scored before any is usable
            chosen.append((k, at, at + len(needle), prev))
            put(at + len(needle), (score, len(chosen) - 1))
            top = max(top, (score, len(chosen) - 1))
    matched, c = {}, top[1]
    while c >= 0:
        k, start, end, c = chosen[c]
        matched[k] = (start, end)
    return matched


def _claim(text: dict[int, str], used: dict[int, list[tuple[int, int]]], needle: str) -> bool:
    """Mark and report an occurrence of `needle` in any source page's text that overlaps no used span."""
    for n, seg in text.items():
        spans = used.setdefault(n, [])
        at = seg.find(needle)
        while at >= 0:
            end = at + len(needle)
            if all(end <= a or at >= b for a, b in spans):
                spans.append((at, end))
                return True
            at = seg.find(needle, at + 1)
    return False


def judge(out_dir: Path, script: str, translation: str | None = None, all_meanings: bool = False) -> dict:
    """Check OUT/<book>.<script>[.<lang>].pdf against OUT/master.json (and, for a translated edition,
    OUT/translation.<lang>.json); returns the report (also written to work/)."""
    out_dir = Path(out_dir)
    master = Master.load(out_dir / "master.json")
    # Exactly what the render command typesets (the U+A8F3 stand-in included).
    target = transliterate.transliterate_master(master, script)
    meanings: dict = {}
    untranslated: list[tuple[int, str]] = []
    if translation:
        from . import translate
        from .schema import Translation

        tpath = translate.translation_path(out_dir, translation)
        if not tpath.is_file():
            raise RuntimeError(f"no {tpath}; run `vedalipi translate {out_dir} --to {translation}` first")
        target, meanings, untranslated, _ = translate.apply_translation(master, target, Translation.load(tpath))
        if not all_meanings:
            meanings = translate.first_meanings(master, meanings)  # exactly what render prints
    pdf = pipeline.pdf_path(out_dir, master, script, translation=translation)
    if not pdf.is_file():
        how = f" --translation {translation}" if translation else ""
        raise RuntimeError(f"no rendered PDF at {pdf}; run `vedalipi render {out_dir} --script {script}{how}` first")
    manifest_path = out_dir / "work" / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"no {manifest_path}: without the source page count, coverage of pages 1..N "
                           "cannot be checked")
    num_pages = Manifest.load(manifest_path).num_pages

    pages = read_pdf(pdf)
    book = segment(pages, target.pages[0].page if target.pages else None)
    by_number = {p.number: p for p in pages}
    report = {"script": script, "translation": translation, "pdf": str(pdf), "pages_checked": 0, "lines_checked": 0,
              "cells_checked": 0, "labels_checked": 0, "meanings_checked": 0, "images_checked": 0, "missing": [],
              "out_of_order": [], "empty_pages": [], "missing_markers": [], "replaced_by_template": [],
              "untranslated": [_item(p, b, "block", "") for p, b in untranslated]}
    missing, out_of_order, replaced = report["missing"], report["out_of_order"], report["replaced_by_template"]
    used: dict[int, list[tuple[int, int]]] = {}  # source page -> spans of its PDF text backing a master line
    unmatched: list[tuple[int, str, str, str, str]] = []  # (page, block, kind, text, needle) not found in order

    present = {p.page for p in target.pages}
    for n in range(1, num_pages + 1):
        if n not in present:
            missing.append(_item(n, "", "page", "source page not in master.json"))
    for n in sorted(present | set(range(1, num_pages + 1))):
        if n not in book.marked:
            report["missing_markers"].append(n)

    for page in target.pages:
        report["pages_checked"] += 1
        logo = header_logo(page)
        body = [b for b in page.blocks if b.type not in BODY_EXCLUDED and b is not logo and not is_edge_artifact(b)]
        if not [b for b in body if b.type != "separator"]:
            report["empty_pages"].append(page.page)

        # Text, in reading order within this source page's stretch of the PDF.
        items = [(page.page, b, k, t, norm(t)) for b, k, t in _text_items(page, replaced, meanings)]
        items = [it for it in items if it[4]]
        counters = {"cell": "cells_checked", "label": "labels_checked", "meaning": "meanings_checked",
                    "gloss": "meanings_checked"}
        for it in items:
            report[counters.get(it[2], "lines_checked")] += 1
        seg = book.text.get(page.page, "")
        matched = _align(seg, [it[4] for it in items])
        used.setdefault(page.page, []).extend(matched.values())
        unmatched.extend(it for i, it in enumerate(items) if i not in matched)

        # Body images, matched in order with the images and placeholders found on this source page.
        events = book.events.get(page.page, [])
        for k, b in enumerate(x for x in body if x.type == "image"):
            report["images_checked"] += 1
            if k >= len(events) or events[k] != "image":
                why = "placeholder 'image not available'" if k < len(events) else "no image in the PDF"
                missing.append(_item(page.page, b.id, "image", why))

        _check_header(page, logo, book, by_number, report)

    # A line not found in order on its own page is out of order only if some other stretch of the PDF still
    # holds a copy no master line has used; otherwise the PDF has one copy fewer than master.json.
    for n, block_id, kind, text, needle in unmatched:
        if _claim(book.text, used, needle):
            out_of_order.append(_item(n, block_id, kind, text))
        else:
            missing.append(_item(n, block_id, kind, text))
    missing.sort(key=lambda m: m["page"])

    # An untranslated block is printed, but a translated edition that still holds one is not finished.
    report["passed"] = not (missing or report["missing_markers"] or report["untranslated"])
    name = f"judge-{script}{f'-{translation}' if translation else ''}.json"
    atomic_write_text(out_dir / "work" / name, json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    return report


def _check_header(page: Page, logo, book: Book, by_number: dict[int, OutputPage], report: dict) -> None:
    """The running header (and the logo beside it) is drawn on every output page whose header belongs to this
    source page: the one running at the top of that output page (book.typ `covering`)."""
    headed = [n for n, src in book.header_source.items() if src == page.page]
    hdr = [b for b in page.blocks if b.type == "running_header" and any(line.strip() for line in b.lines)]
    for b in hdr:
        report["lines_checked"] += 1
        text = " ".join(b.lines)
        needle = norm(text)
        if headed:
            if not all(needle in by_number[n].header for n in headed):
                report["missing"].append(_item(page.page, b.id, "running_header", text))
        elif any(needle in by_number[n].header for n in book.outputs.get(page.page, ())):
            pass  # the same header text is shown on the output page this source page sits on
        else:
            report["replaced_by_template"].append(_item(
                page.page, b.id, "running_header", text,
                reason="this source page never runs at the top of an output page; the template shows only the "
                       "header of the source page running at the top"))
    if logo is not None:
        report["images_checked"] += 1
        if headed:
            if not all(by_number[n].header_images for n in headed):
                report["missing"].append(_item(page.page, logo.id, "logo", "no logo image in the page header"))
        else:
            report["replaced_by_template"].append(_item(
                page.page, logo.id, "logo", "",
                reason="the logo beside the running header is drawn in the page header, which this source page "
                       "never owns (it never runs at the top of an output page)"))


def summary(report: dict) -> str:
    """A few lines for the terminal."""
    lines = [
        f"judge {report['script']}: {report['pdf']}",
        (f"checked {report['pages_checked']} pages, {report['lines_checked']} lines, "
         f"{report['cells_checked']} cells, {report['labels_checked']} labels, "
         f"{report.get('meanings_checked', 0)} meaning/gloss lines, {report['images_checked']} images"),
        (f"missing: {len(report['missing'])}, out of order: {len(report['out_of_order'])}, "
         f"missing markers: {len(report['missing_markers'])}, empty pages: {len(report['empty_pages'])}, "
         f"replaced by template: {len(report['replaced_by_template'])}"),
    ]
    for key in ("missing", "out_of_order"):
        for m in report[key][:20]:
            lines.append(f"  {key} p{m['page']} {m['block']} {m['kind']}: {m['text'][:60]}")
        if len(report[key]) > 20:
            lines.append(f"  ...and {len(report[key]) - 20} more {key}")
    if report.get("untranslated"):
        pages = sorted({m["page"] for m in report["untranslated"]})
        lines.append(f"  untranslated: {len(report['untranslated'])} blocks on pages {pages}")
    if report["missing_markers"]:
        lines.append(f"  missing markers: {report['missing_markers']}")
    if report["empty_pages"]:
        lines.append(f"  empty pages (warning): {report['empty_pages']}")
    lines.append("PASS" if report["passed"] else "FAIL")
    return "\n".join(lines)
