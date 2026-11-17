# ABOUTME: EPUB input: reads an e-book's spine documents into pages of blocks (the ocr.json contract), with no OCR.
# ABOUTME: Headings, paragraphs, lists, tables, rules and images map to block types; language comes from the text's script.

from __future__ import annotations

import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from .schema import Block, Master, Page

# An e-book has no printed pages. Where the publisher kept them (epub:type="pagebreak" markers) they are
# used; otherwise each spine document is a page, cut when it grows past this many blocks so one page stays
# a sensible unit for translation, corrections and `--pages`.
MAX_BLOCKS_PER_PAGE = 60
# A page this full is cut early at the next heading, so a cut falls where the book itself breaks.
SOFT_BLOCKS_PER_PAGE = 40
# A prose line longer than this (in characters) is split at dandas into printed lines.
LONG_LINE = 110
# Images stacked into one page canvas: common width and the white gap between them, in pixels.
CANVAS_WIDTH = 1400
CANVAS_GAP = 40

_NS_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_NS_OPF = "{http://www.idpf.org/2007/opf}"
_NS_DC = "{http://purl.org/dc/elements/1.1/}"

BLANK_RE = re.compile(r"(?:\.{3,}|…+|_{3,})")

# Unicode blocks -> script, and the script's own language (ISO 639-1) when its text is not Sanskrit.
_SCRIPT_RANGES = [
    (0x0900, 0x097F, "devanagari"), (0x1CD0, 0x1CFF, "devanagari"), (0xA8E0, 0xA8FF, "devanagari"),
    (0x0980, 0x09FF, "bengali"), (0x0A00, 0x0A7F, "gurmukhi"), (0x0A80, 0x0AFF, "gujarati"),
    (0x0B00, 0x0B7F, "odia"), (0x0B80, 0x0BFF, "tamil"), (0x0C00, 0x0C7F, "telugu"),
    (0x0C80, 0x0CFF, "kannada"), (0x0D00, 0x0D7F, "malayalam"), (0x0D80, 0x0DFF, "sinhala"),
]
NATIVE_LANG = {
    "devanagari": "hi", "bengali": "bn", "gurmukhi": "pa", "gujarati": "gu", "odia": "or", "tamil": "ta",
    "telugu": "te", "kannada": "kn", "malayalam": "ml", "sinhala": "si", "latin": "en",
}
# Languages a script may carry, for honouring a document's lang attribute only when it is plausible.
_SCRIPT_LANGS = {
    "devanagari": {"sa", "hi", "mr", "ne"}, "bengali": {"bn", "as", "sa"}, "gurmukhi": {"pa", "sa"},
    "gujarati": {"gu", "sa"}, "odia": {"or", "sa"}, "tamil": {"ta", "sa"}, "telugu": {"te", "sa"},
    "kannada": {"kn", "sa"}, "malayalam": {"ml", "sa"}, "sinhala": {"si", "sa"},
    "latin": {"en", "sa", "de", "fr", "es", "it", "pt", "nl", "la"},
}


# ---------------------------------------------------------------- a lenient HTML tree

_VOID = {"br", "img", "hr", "meta", "link", "input", "col", "source", "wbr", "area", "base", "embed", "param", "track"}
_SKIP = {"head", "script", "style", "nav", "title", "template", "noscript", "svg:title", "desc"}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK = {
    "p", "div", "section", "article", "main", "body", "html", "blockquote", "aside", "figure", "figcaption", "header",
    "footer", "address", "center", "details", "summary", "dl", "dt", "dd", "li", "ul", "ol", "table", "hr", "pre",
    "form", "fieldset", "tr", "td", "th", "thead", "tbody", "tfoot", "caption", "colgroup", "col", "img", "svg", "nav",
    *_HEADINGS,
}


class _Node:
    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str], parent: _Node | None):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[_Node | str] = []

    def attr(self, *names: str) -> str:
        for n in names:
            if n in self.attrs:
                return self.attrs[n]
        return ""


class _Tree(HTMLParser):
    """Builds a tree from XHTML or sloppy HTML alike: unclosed tags and undefined entities do not matter."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.cur.children.append(_Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag: str) -> None:
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data: str) -> None:
        self.cur.children.append(data)


def parse_html(text: str) -> _Node:
    t = _Tree()
    t.feed(text)
    t.close()
    return t.root


# ---------------------------------------------------------------- text and language


def _clean(text: str) -> str:
    """Collapse HTML whitespace to single spaces; joiners that only steer line breaking go, ZWJ/ZWNJ (which
    shape Indic conjuncts) stay."""
    text = text.replace("\u00a0", " ").replace("\u2060", "").replace("\u00ad", "").replace("\ufeff", "")
    return re.sub(r"[ \t\r\n\f\v]+", " ", text)


def dominant_script(text: str) -> str | None:
    """The script most of the text's letters are in: an Indic script name, "latin", or None for no letters."""
    counts: dict[str, int] = {}
    for ch in text:
        if not ch.isalpha() and unicodedata.category(ch) not in ("Mn", "Mc"):
            continue
        o = ord(ch)
        script = next((s for lo, hi, s in _SCRIPT_RANGES if lo <= o <= hi), None)
        if script is None and (o < 0x0250 or 0x1E00 <= o <= 0x1EFF) and ch.isalpha():
            script = "latin"
        if script is not None:
            counts[script] = counts.get(script, 0) + 1
    return max(counts, key=counts.get) if counts else None


@dataclass
class LangRules:
    """How a block's language is decided. `source_script` is the script the book's Sanskrit is written in;
    `languages` are the languages the book prints (empty: Sanskrit only)."""

    source_script: str = "devanagari"
    languages: tuple[str, ...] = ()

    def lang_for(self, text: str, declared: str) -> str:
        """The language of a block from its script, using the document's lang attribute only to choose
        among the languages that script can plausibly carry (EPUBs often declare lang="en" for everything)."""
        script = dominant_script(text)
        if script is None:
            return "en"
        listed = set(self.languages)
        base = declared.split("-", 1)[0].lower() if declared else ""
        is_source = script == self.source_script or (script == "latin" and self._roman_source())
        # "sa" is a plausible declared language only for the script the book's Sanskrit is actually printed
        # in: an EPUB's lang="sa" on the <html> element (from Pandoc's "lang: sa" front matter, say) is
        # inherited by every paragraph, including a Telugu instruction or an English heading, and must not
        # make those Sanskrit. A declared language otherwise counts only when the script can carry it and it
        # is one of the book's languages (none given means Sanskrit only): a publisher tagging
        # Sanskrit-in-Kannada as "kn", or a Devanagari book as "hi", loses unless --languages names it.
        candidates = set(_SCRIPT_LANGS.get(script, set()))
        if not is_source and script != "devanagari":
            candidates.discard("sa")
        if base and base in candidates and base in (listed or {"sa"}):
            return base
        native = NATIVE_LANG.get(script, "en")
        sanskrit_ok = not listed or "sa" in listed
        if script == "devanagari":
            # Devanagari is the master script: Sanskrit unless the book is declared to hold only Hindi/Marathi.
            return "sa" if sanskrit_ok else next((l for l in ("hi", "mr", "ne") if l in listed), "hi")
        if script == self.source_script or (script == "latin" and self._roman_source()):
            # The book's Sanskrit is written in this script; so is any other text in the script's own language.
            if sanskrit_ok and native not in listed:
                return "sa"
            if sanskrit_ok and native in listed:
                # Both Sanskrit and the script's language are printed: an undeclared block is Sanskrit when it
                # reads like a mantra (dandas, svaras, visarga-final words), else the native language.
                return "sa" if _looks_sanskrit(text) else native
            return native
        return native

    def _roman_source(self) -> bool:
        from . import transliterate

        try:
            return transliterate.is_roman(self.source_script)
        except Exception:
            return False


_SANSKRIT_CUES = re.compile(
    "[।॥\u0951\u0952\u1CD0-\u1CFF\uA8E0-\uA8F3]|[ःఃಃഃଃঃ](?=\\s|$|[,.])|\\b(?:namaḥ|svāhā|oṃ|om)\\b", re.IGNORECASE
)


def _looks_sanskrit(text: str) -> bool:
    return len(_SANSKRIT_CUES.findall(text)) >= 1


# ---------------------------------------------------------------- items out of one document


@dataclass
class Item:
    """One thing found in reading order: a block (with its text), a page break marker, or an image."""

    kind: str  # "block", "pagebreak", "image"
    type: str = "prose"
    lines: list[str] = field(default_factory=list)
    rows: list[list[str]] | None = None
    level: int | None = None
    align: str = "left"
    lang: str = ""  # declared language (attribute), resolved later
    label: str = ""  # pagebreak: printed page label
    data: bytes = b""  # image bytes


def _align_of(node: _Node, inherited: str) -> str:
    style = node.attr("style").lower()
    m = re.search(r"text-align\s*:\s*(left|center|right|justify)", style)
    if m:
        return "left" if m.group(1) == "justify" else m.group(1)
    classes = node.attr("class").lower().split()
    if node.attr("align").lower() in ("center", "right", "left"):
        return node.attr("align").lower()
    if node.tag == "center" or any(c in ("center", "centre", "centered", "tcenter", "text-center") for c in classes):
        return "center"
    if any(c in ("right", "tright", "text-right") for c in classes):
        return "right"
    return inherited


def _is_pagebreak(node: _Node) -> bool:
    return "pagebreak" in node.attr("epub:type").split() or node.attr("role") == "doc-pagebreak"


def _inline_text(node: _Node, pre: bool = False) -> str:
    """Text of an inline subtree, with <br> as a newline; block children inside are flattened too."""
    out: list[str] = []
    for c in node.children:
        if isinstance(c, str):
            out.append(c if pre else _clean(c))
        elif c.tag == "br":
            out.append("\n")
        elif c.tag in _SKIP or "hidden" in c.attrs:
            continue
        elif c.tag == "span" and "blank" in c.attr("class").split() and not c.children:
            out.append(" ........ ")  # a fill-in blank drawn as a rule (vedalipi's own EPUBs) reads back as dots
        elif _is_pagebreak(c):
            continue
        elif c.tag in ("img", "svg", "table", "hr", "ul", "ol", "dl"):
            continue
        else:
            inner = _inline_text(c, pre or c.tag == "pre")
            if c.tag in _BLOCK:
                inner = "\n" + inner + "\n"
            out.append(inner)
    return "".join(out)


def _lines_of(text: str) -> list[str]:
    return [l for l in (_clean(l).strip() for l in text.split("\n")) if l]


def _has_block_child(node: _Node) -> bool:
    return any(isinstance(c, _Node) and (c.tag in _BLOCK or _is_pagebreak(c)) for c in node.children)


class _Walker:
    def __init__(self, doc_path: str, zf: zipfile.ZipFile):
        self.doc_path = doc_path
        self.zf = zf
        self.items: list[Item] = []

    def walk(self, node: _Node, lang: str = "", align: str = "left") -> None:
        for c in node.children:
            if isinstance(c, str):
                continue
            self._node(c, lang, align)

    def _node(self, n: _Node, lang: str, align: str) -> None:
        if n.tag in _SKIP or "hidden" in n.attrs or "srcpage" in n.attr("class").split():
            return
        # A lang on <html> or <body> is one declaration for the whole document (Pandoc writes the book's
        # main language there), so it would decide every block alike; only a lang below them counts.
        if n.tag not in ("html", "body"):
            lang = n.attr("xml:lang", "lang") or lang
        align = _align_of(n, align)
        if _is_pagebreak(n):
            label = n.attr("title", "aria-label") or _clean(_inline_text(n)).strip()
            self.items.append(Item("pagebreak", label=label.strip()))
            return
        if n.tag in _HEADINGS:
            lines = _lines_of(_inline_text(n))
            if lines:
                self.items.append(Item("block", "heading", lines, level=_HEADINGS[n.tag], align=align, lang=lang))
            return
        if n.tag == "hr":
            self.items.append(Item("block", "separator"))
            return
        if n.tag == "img":
            self._image(n.attr("src"))
            return
        if n.tag == "svg":
            for d in _descendants(n):
                if d.tag == "image":
                    self._image(d.attr("href", "xlink:href"))
            return
        if n.tag == "table":
            self._table(n, lang, align)
            return
        if n.tag in ("ul", "ol"):
            self._list(n, lang, align)
            return
        if n.tag == "pre":
            lines = _lines_of(_inline_text(n, pre=True))
            if lines:
                self.items.append(Item("block", "verse", lines, align=align, lang=lang))
            return
        if n.tag == "div" and _is_line_group(n):
            # A wrapper <div> holding nothing but <p> lines is one verse: its paragraphs are its printed lines.
            lines = [l for c in n.children if isinstance(c, _Node) for l in _lines_of(_inline_text(c))]
            kind = "fill_blank" if any(BLANK_RE.search(l) for l in lines) else "verse"
            self.items.append(Item("block", kind, lines, align=_align_of(_first_p(n), align), lang=lang))
            return
        if n.tag in _BLOCK and _has_block_child(n):
            # Mixed content: runs of inline children between block children are paragraphs of their own.
            run: list[_Node | str] = []
            for c in n.children:
                if isinstance(c, _Node) and (c.tag in _BLOCK or _is_pagebreak(c)):
                    self._paragraph(run, lang, align)
                    run = []
                    self._node(c, lang, align)
                else:
                    run.append(c)
            self._paragraph(run, lang, align)
            return
        if n.tag in _BLOCK:
            self._paragraph(n.children, lang, align, node=n)
            return
        # An inline element at block level (a stray <span>): its text is a paragraph.
        self._paragraph([n], lang, align)

    def _paragraph(self, children: list[_Node | str], lang: str, align: str, node: _Node | None = None) -> None:
        holder = _Node("p", {}, None)
        holder.children = list(children)
        text = _inline_text(holder)
        lines = _lines_of(text)
        if not lines:
            return
        # Printed breaks (<br>) make a verse; a single paragraph is prose.
        kind = "verse" if len(lines) > 1 else "prose"
        if any(BLANK_RE.search(l) for l in lines):
            kind = "fill_blank"
        self.items.append(Item("block", kind, lines, align=align, lang=lang))

    def _table(self, n: _Node, lang: str, align: str) -> None:
        rows: list[list[str]] = []
        for tr in _descendants(n, stop={"table"}):
            if tr.tag != "tr":
                continue
            cells = []
            for td in tr.children:
                if isinstance(td, _Node) and td.tag in ("td", "th"):
                    cells.append("\n".join(_lines_of(_inline_text(td))))
            if any(c.strip() for c in cells):
                rows.append(cells)
        if rows:
            self.items.append(Item("block", "table", rows=rows, align=align, lang=lang))

    def _list(self, n: _Node, lang: str, align: str) -> None:
        ordered = n.tag == "ol"
        raw = n.attr("start") or "1"
        start = int(raw) if raw.isascii() and raw.isdigit() else 1
        k = start
        for c in n.children:
            if not isinstance(c, _Node) or c.tag != "li":
                continue
            nested = [x for x in c.children if isinstance(x, _Node) and x.tag in ("ul", "ol")]
            own = _Node("li", {}, None)
            own.children = [x for x in c.children if not (isinstance(x, _Node) and x.tag in ("ul", "ol"))]
            lines = _lines_of(_inline_text(own))
            if lines:
                if ordered and not re.match(r"^\s*(?:\d+|[a-zA-Z]|[ivxIVX]+)[.)]\s", lines[0]):
                    lines[0] = f"{k}. {lines[0]}"
                self.items.append(Item("block", "list_item", lines, align=align, lang=c.attr("xml:lang", "lang") or lang))
            k += 1
            for sub in nested:
                self._list(sub, lang, align)

    def _image(self, src: str) -> None:
        if not src or src.startswith(("data:", "http:", "https:")):
            return
        path = posixpath.normpath(posixpath.join(posixpath.dirname(self.doc_path), unquote(src)))
        try:
            data = self.zf.read(path)
        except KeyError:
            return
        self.items.append(Item("image", data=data))


def _is_line_group(n: _Node) -> bool:
    """A <div> whose children are two or more <p> elements (and whitespace) with no block or break inside."""
    ps = [c for c in n.children if isinstance(c, _Node)]
    if len(ps) < 2 or any(isinstance(c, str) and c.strip() for c in n.children):
        return False
    return all(c.tag == "p" and not _has_block_child(c) and not _is_pagebreak(c) and "hidden" not in c.attrs for c in ps)


def _first_p(n: _Node) -> _Node:
    return next(c for c in n.children if isinstance(c, _Node))


def _descendants(node: _Node, stop: set[str] = frozenset()):
    for c in node.children:
        if isinstance(c, _Node):
            yield c
            if c.tag not in stop:
                yield from _descendants(c, stop)


# ---------------------------------------------------------------- the package


@dataclass
class EpubBook:
    title: str
    docs: list[tuple[str, str]]  # (path in zip, XHTML text) in spine order
    master: Master | None  # a vedalipi EPUB carries its Devanagari master


def open_epub(path: Path) -> tuple[zipfile.ZipFile, EpubBook]:
    zf = zipfile.ZipFile(path)
    names = set(zf.namelist())
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(f".//{_NS_CONTAINER}rootfile")
    if rootfile is None:
        raise ValueError(f"{path}: container.xml names no rootfile")
    opf_path = rootfile.get("full-path")
    opf = ET.fromstring(zf.read(opf_path))
    opf_dir = posixpath.dirname(opf_path)
    resolve = lambda href: posixpath.normpath(posixpath.join(opf_dir, unquote(href.split("#", 1)[0])))  # noqa: E731
    items = {it.get("id"): (resolve(it.get("href", "")), it.get("media-type", "")) for it in opf.iter(f"{_NS_OPF}item")}
    title_el = opf.find(f".//{_NS_DC}title")
    title = (title_el.text or "").strip() if title_el is not None else ""

    master = None
    for href, mime in items.values():
        if posixpath.basename(href) == "master.json" and href in names:
            try:
                master = Master.model_validate_json(zf.read(href))
            except ValueError:
                master = None
    docs: list[tuple[str, str]] = []
    for ref in opf.iter(f"{_NS_OPF}itemref"):
        href, mime = items.get(ref.get("idref"), ("", ""))
        if href in names and (mime in ("application/xhtml+xml", "text/html") or href.endswith((".xhtml", ".html", ".htm"))):
            docs.append((href, zf.read(href).decode("utf-8", errors="replace")))
    if not docs and master is None:
        raise ValueError(f"{path}: the spine has no XHTML documents")
    return zf, EpubBook(title=title, docs=docs, master=master)


def epub_title(path: Path) -> str:
    zf, book = open_epub(Path(path))
    zf.close()
    return book.title


# ---------------------------------------------------------------- pages


def _split_long(line: str) -> list[str]:
    """A long prose line breaks into printed lines at dandas (never inside a verse number)."""
    if len(line) <= LONG_LINE or not re.search("[।॥]", line):
        return [line]
    pieces = re.split(r"(?<=[।॥])\s+(?![०-९0-9]+\s*[।॥])", line)
    out: list[str] = []
    for piece in pieces:
        if out and len(out[-1]) + 1 + len(piece) <= LONG_LINE:
            out[-1] = out[-1] + " " + piece
        else:
            out.append(piece)
    return out


def _to_block(item: Item, bid: str, rules: LangRules) -> Block:
    text = " ".join(item.lines) if item.rows is None else " ".join(c for r in item.rows for c in r)
    lang = "en" if item.type == "separator" else rules.lang_for(text, item.lang)
    lines = item.lines
    if item.type == "prose" and lang == "sa":
        lines = [l for line in lines for l in _split_long(line)]
    return Block(id=bid, type=item.type, lang=lang, lines=lines, rows=item.rows, level=item.level, align=item.align)


def _compose_images(images: list[bytes], dest: Path) -> list[list[float] | None] | None:
    """Stack a page's images into one canvas at dest (the page's "scan"). Returns one entry per image, in
    order: its bbox on the canvas, or None when it could not be read; None when no image could be read."""
    from PIL import Image

    opened: list = []
    for data in images:
        try:
            im = Image.open(BytesIO(data))
            im.load()
            opened.append(im.convert("RGB"))
        except Exception:  # any decoder failure: the image is left out, never the page
            opened.append(None)
    if not any(opened):
        return None
    width = min(CANVAS_WIDTH, max(im.width for im in opened if im))
    scaled = [im.resize((width, max(1, round(im.height * width / im.width)))) if im else None for im in opened]
    read = [im for im in scaled if im]
    height = sum(im.height for im in read) + CANVAS_GAP * (len(read) + 1)
    canvas = Image.new("RGB", (width, height), "white")
    boxes: list[list[float] | None] = []
    y = CANVAS_GAP
    for im in scaled:
        if im is None:
            boxes.append(None)
            continue
        canvas.paste(im, (0, y))
        boxes.append([0.0, y / height, 1.0, (y + im.height) / height])
        y += im.height + CANVAS_GAP
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp.png")
    canvas.save(tmp, format="PNG")
    tmp.replace(dest)
    return boxes


def _paginate(all_items: list[list[Item]]) -> list[tuple[list[Item], str]]:
    """Cut the items of the spine documents into pages: at the book's own page-break markers when it has
    them, else one page per document; a page past MAX_BLOCKS_PER_PAGE is cut (at a heading once it is past
    SOFT_BLOCKS_PER_PAGE). Returns (items, printed page label) per page."""
    has_markers = any(it.kind == "pagebreak" for items in all_items for it in items)
    pages: list[tuple[list[Item], str]] = []
    cur: list[Item] = []
    label = ""

    def close(next_label: str) -> None:
        nonlocal cur, label
        if any(it.kind != "pagebreak" for it in cur):
            pages.append((cur, label))
        cur, label = [], next_label

    for items in all_items:
        if not has_markers:
            close("")
        for it in items:
            if it.kind == "pagebreak":
                close(it.label)
                continue
            n = sum(1 for x in cur if x.kind != "pagebreak")
            if n >= MAX_BLOCKS_PER_PAGE or (n >= SOFT_BLOCKS_PER_PAGE and it.kind == "block" and it.type == "heading"):
                close("")
            cur.append(it)
    close("")
    return pages


def load_epub_pages(
    path: Path, *, assets_dir: Path | None = None, source_script: str = "devanagari", languages: tuple[str, ...] = ()
) -> list[Page]:
    """Read an EPUB into Pages (the ocr.json contract), skipping OCR entirely.

    A vedalipi EPUB carries its Devanagari master.json: that is returned as is. Any other EPUB is read from
    its spine: headings, paragraphs (<br> breaks make verse lines), lists, tables, rules and images become
    blocks; each block's language follows its script (Devanagari is Sanskrit; the source script is Sanskrit
    unless `languages` says the script's own language is printed too, then mantra-like text is Sanskrit).
    Images are stacked into one canvas per page under `assets_dir` (dropped when it is None) so image blocks
    crop from it like blocks of a scan. Printed page-break markers, when present, decide the pages.
    """
    zf, book = open_epub(Path(path))
    try:
        if book.master is not None:
            # The master came inside the file, from wherever the file came from: its source_image paths
            # name files on another machine, or any file on this one, so they are never followed.
            pages = [p.model_copy(update={"source_image": ""}) for p in sorted(book.master.pages, key=lambda p: p.page)]
            if pages:
                return pages
        rules = LangRules(source_script=source_script, languages=languages)
        per_doc: list[list[Item]] = []
        for doc_path, text in book.docs:
            w = _Walker(doc_path, zf)
            w.walk(parse_html(text))
            per_doc.append(w.items)
        pages: list[Page] = []
        for items, label in _paginate(per_doc):
            number = len(pages) + 1
            blocks: list[Block] = []
            images: list[bytes] = []
            image_slots: list[int] = []
            for it in items:
                if it.kind == "image":
                    if assets_dir is not None:
                        images.append(it.data)
                        image_slots.append(len(blocks))
                        blocks.append(Block(id=f"b{len(blocks) + 1}", type="image", lang="en", bbox=[0.0, 0.0, 1.0, 1.0]))
                elif it.kind == "block":
                    blocks.append(_to_block(it, f"b{len(blocks) + 1}", rules))
            source_image = ""
            if images:
                dest = Path(assets_dir) / f"p{number:04d}.png"
                boxes = _compose_images(images, dest)
                if boxes is None:
                    blocks = [b for i, b in enumerate(blocks) if i not in image_slots]
                else:
                    source_image = str(dest)
                    for slot, box in zip(image_slots, boxes):
                        if box is not None:
                            blocks[slot].bbox = box
                    unreadable = {slot for slot, box in zip(image_slots, boxes) if box is None}
                    blocks = [b for i, b in enumerate(blocks) if i not in unreadable]
                blocks = [b.model_copy(update={"id": f"b{i + 1}"}) for i, b in enumerate(blocks)]
            if label:
                blocks.append(Block(id=f"b{len(blocks) + 1}", type="page_number", lang="en", lines=[label], align="center"))
            if blocks:
                pages.append(Page(page=number, source_image=source_image, blocks=blocks))
        if not pages:
            raise ValueError(f"{path}: no text found in the EPUB")
        return pages
    finally:
        zf.close()
