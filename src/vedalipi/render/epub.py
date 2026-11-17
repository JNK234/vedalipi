# ABOUTME: EPUB 3 writer: the same document tree book.typ typesets, laid out as a reflowable e-book for Apple Books and others.
# ABOUTME: Embeds the vendored (svara-emphasised) fonts, splits chapters at top-level headings, keeps printed line breaks.

from __future__ import annotations

import html
import json
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from ..schema import Master
from .document import build_document, family_chain
from .fonts import SCRIPTS
from .svarafont import derived_family, has_derived

# BCP 47 tags for Sanskrit written in a target script, for the package's dc:language (iPad/iPhone use it
# for dictionary lookup and font selection; a wrong tag does no harm beyond that).
SCRIPT_TAGS = {
    "kannada": "sa-Knda", "telugu": "sa-Telu", "devanagari": "sa-Deva", "tamil": "sa-Taml",
    "malayalam": "sa-Mlym", "gujarati": "sa-Gujr", "bengali": "sa-Beng", "gurmukhi": "sa-Guru",
    "odia": "sa-Orya", "grantha": "sa-Gran", "sinhala": "sa-Sinh", "sharada": "sa-Shrd", "tibetan": "sa-Tibt",
    "thai": "sa-Thai", "burmese": "sa-Mymr", "balinese": "sa-Bali", "javanese": "sa-Java", "khmer": "sa-Khmr",
    "modi": "sa-Modi", "siddham": "sa-Sidd", "newa": "sa-Newa", "brahmi": "sa-Brah", "assamese": "as",
}

# A chapter longer than this many blocks is split at the next source page, so one XHTML file never grows
# so large that a phone reader stalls paginating it.
MAX_CHAPTER_BLOCKS = 250

# Namespaces
XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
IBOOKS_PREFIX = "ibooks: http://vocabulary.itunes.apple.com/rdf/ibooks/vocabulary-extensions-1.0/"

# Apple Books reads this EPUB 2-era file to decide whether to honour embedded fonts (the EPUB 3 meta
# property ibooks:specified-fonts in the package says the same; both are written).
APPLE_DISPLAY_OPTIONS = """<?xml version="1.0" encoding="UTF-8"?>
<display_options>
  <platform name="*">
    <option name="specified-fonts">true</option>
  </platform>
</display_options>
"""

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

# Indic runs inside Latin text (a quoted "(ಆಚಾರ್ಯ ವನ್ದನಮ್)") are scaled back up to the Indic body size, as
# book.typ does, and set in their own script's font explicitly: not every reader falls back through the
# font-family list character by character, and the embedded Latin font has no Indic glyphs.
INDIC_RUN = re.compile("[\u0900-\u0DFF\u1CD0-\u1CFF\uA8E0-\uA8FF]+")
# Unicode block of an Indic run -> the target script whose font sets it.
INDIC_BLOCKS = [(0x0900, 0x097F, "devanagari"), (0x0B80, 0x0BFF, "tamil"), (0x0C00, 0x0C7F, "telugu"), (0x0C80, 0x0CFF, "kannada")]

# Characters XML 1.0 forbids (C0 controls other than tab/newline/return, lone surrogates, U+FFFE/U+FFFF). OCR
# or an imported file can carry one; it carries no text, and left in it makes the whole package unreadable.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def E(s: str) -> str:
    return html.escape(_XML_ILLEGAL.sub("", s))


def _attr(s: str) -> str:
    return html.escape(_XML_ILLEGAL.sub("", str(s)), quote=True)


# ---------------------------------------------------------------- fonts


@dataclass
class FontSet:
    """Font files to embed, found by family name in the font directories, and the CSS classes blocks use."""

    dirs: list[Path]
    files: dict[str, Path] = field(default_factory=dict)  # EPUB-relative path -> source file
    faces: list[str] = field(default_factory=list)  # @font-face rules
    classes: dict[tuple[tuple[str, ...], float], str] = field(default_factory=dict)
    _embedded: set[str] = field(default_factory=set)

    def embed(self, family: str) -> bool:
        """Add every weight/style file of `family` (Regular, Bold, Italic, BoldItalic) found in the font dirs."""
        if family in self._embedded:
            return True
        stem = family.replace(" ", "")
        found = False
        for d in self.dirs:
            for f in sorted(Path(d).glob(f"{stem}-*.ttf")):
                style = f.stem.split("-", 1)[1]
                weight = "bold" if "Bold" in style else "normal"
                slant = "italic" if "Italic" in style else "normal"
                rel = f"fonts/{f.name}"
                self.files[rel] = f
                self.faces.append(
                    f'@font-face {{ font-family: "{family}"; src: url("{rel}"); font-weight: {weight}; font-style: {slant}; }}'
                )
                found = True
        if found:
            self._embedded.add(family)
        return found

    def cls(self, chain: list[str], scale: float) -> str:
        """CSS class for a font chain at a relative size; the chain's primary family is embedded."""
        key = (tuple(chain), round(scale, 4))
        if key not in self.classes:
            self.embed(chain[0])
            self.classes[key] = f"f{len(self.classes)}"
        return self.classes[key]

    def css(self) -> str:
        rules = list(self.faces)
        for (chain, scale), name in self.classes.items():
            size = "" if scale == 1.0 else f" font-size: {scale:.4g}em;"
            rules.append(f".{name} {{ font-family: {font_family_css(chain)};{size} }}")
        return "\n".join(rules)


def font_family_css(chain: list[str] | tuple[str, ...]) -> str:
    """A CSS font-family list for a Typst font chain. The scripts with svara-emphasised copies are named by
    that copy (the file that is embedded), so an Indic run quoted inside Latin text finds the same font the
    Indic body uses instead of falling through to the reader's system font."""
    names: list[str] = []
    for f in chain:
        for n in ([derived_family(f), f] if has_derived(f) else [f]):
            if n not in names:
                names.append(n)
    return ", ".join(f'"{n}"' for n in names) + ", serif"


# ---------------------------------------------------------------- stylesheet

# Sizes follow document.STYLE: 1.42 em line pitch for plain text, ~1.97 em for svara-marked text (room
# for marks above and below), headings 1.43/1.2/1.08 em regular weight. Everything is in em so the
# reader's own font-size setting scales the whole book. No text colour is set and rules use currentColor,
# so the reader's night and sepia themes apply (Apple Books inverts nothing it does not own).
BASE_CSS = """
html { -webkit-hyphens: none; hyphens: none; }
body { margin: 0; line-height: 1.42; text-align: left; word-wrap: break-word; }
h1, h2, h3, h4, h5, h6 { font-weight: normal; line-height: 1.35; margin: 1.6em 0 1em; page-break-after: avoid; break-after: avoid; }
h1.title { font-size: 2em; text-align: center; margin-top: 30%; }
p.subtitle { text-align: center; opacity: 0.7; margin-top: 2em; }
.block { margin: 1.1em 0; }
.vedic { line-height: 1.97; }
.vedic.block { margin: 1.25em 0; }
.tight-above { margin-top: 0.12em; }
.tight-below { margin-bottom: 0.12em; }
.center { text-align: center; }
.right { text-align: right; }
.left { text-align: left; }
.line { margin: 0; padding-left: 2em; text-indent: -2em; }
.center .line, .right .line { padding-left: 0; text-indent: 0; }
p.prose { margin: 0; text-indent: 0; }
p.prose.justify { text-align: justify; }
.srcpage { text-align: right; font-size: 0.6em; color: #999; margin: 1.4em 0 0.4em; line-height: 1.2; }
.item { display: table; width: 100%; margin: 0.3em 0; }
.item .marker { display: table-cell; width: 1.6em; padding-right: 0.2em; white-space: nowrap; }
.item .itext { display: table-cell; }
.blank { display: inline-block; border-bottom: 1px dotted currentColor; height: 0.85em; margin: 0 0.15em; vertical-align: baseline; }
table { border-collapse: collapse; margin: 0.6em 0; }
table.ruled { border: 1px solid currentColor; }
table.ruled th, table.ruled td { border-left: 1px solid currentColor; border-right: 1px solid currentColor; padding: 0.45em 0.55em; vertical-align: top; }
table.ruled thead th { border-bottom: 1px solid currentColor; text-align: center; font-weight: bold; }
table.plain td { padding: 0.1em 0.4em; vertical-align: top; border: 0; }
table.plain td.last { text-align: right; }
hr.sep { width: 28%; border: 0; border-top: 1px solid #999; margin: 1em auto; }
.figure { text-align: center; margin: 1.2em 0; }
.figure img, .figure svg { max-width: 100%; }
.placeholder { border: 1px solid #999; opacity: 0.6; font-size: 0.75em; padding: 1em; text-align: center; }
.mantra { margin: 1.5em 0; }
.cue { font-weight: bold; margin: 0 0 0.5em; page-break-after: avoid; break-after: avoid; }
.mtext { border-left: 2px solid #a0a0a0; padding: 0.15em 0 0.15em 0.9em; margin: 0; }
.meaning { font-style: italic; margin: 0.55em 0 0 1.2em; line-height: 1.4; }
.meaning p { margin: 0; }
.gloss { margin: 0.5em 0 0 1.2em; line-height: 1.4; }
.pair { margin: 0.25em 0; }
.pair .orig { opacity: 0.8; }
@media (min-width: 600px) {
  .pair { display: table; width: 100%; table-layout: fixed; }
  .pair > div { display: table-cell; vertical-align: top; }
  .pair > .orig { padding-right: 0.8em; }
  .pair > .tgt { padding-left: 0.8em; }
}
"""


# ---------------------------------------------------------------- XHTML building


class Chapter:
    def __init__(self, index: int, title: str):
        self.index = index
        self.title = title
        self.parts: list[str] = []
        self.blocks = 0
        self.pages: list[int] = []  # source pages whose start marker lies in this chapter
        self.headings: list[tuple[int, str, str]] = []  # (level, anchor, text)
        self.has_svg = False

    @property
    def href(self) -> str:
        return f"ch{self.index:03d}.xhtml"


class EpubBuilder:
    """Turns build_document()'s tree into chapter XHTML files, CSS and navigation."""

    def __init__(self, doc: dict, fonts: FontSet, build_dir: Path):
        self.doc = doc
        self.S = doc["style"]
        self.fonts = fonts
        self.build_dir = build_dir
        self.sa_tag = language_tags(doc["script"], None)[0]  # "sa-Knda": Sanskrit in the book's script
        self.images: dict[str, Path] = {}  # EPUB-relative -> file in build_dir
        self.chapters: list[Chapter] = []
        # Chapters split at the shallowest heading level the book uses (level 1 when it has any).
        levels = [b["level"] for p in doc["pages"] for b in p["blocks"] if b["type"] == "heading"]
        self.top_level = min(levels) if levels else None

    # ---- fonts & text

    def _cls(self, f: dict) -> str:
        scale = 1.0 if self.S.get("uniform_scale") else f.get("scale", 1.0)
        return self.fonts.cls(f["font"], scale)

    def _indic_chain(self, run: str) -> list[str]:
        """Font chain for an Indic run quoted inside Latin text: its own script's font when vendored, else the book's."""
        for ch in run:
            for lo, hi, script in INDIC_BLOCKS:
                if lo <= ord(ch) <= hi and script in SCRIPTS:
                    return family_chain(SCRIPTS[script].family)
        return list(self.doc["main"]["font"])

    def _text(self, s: str, f: dict) -> str:
        """Escaped text; Indic runs inside Latin text get their script's font and, when the Latin text is set
        smaller, are scaled back to the body size."""
        out = E(s)
        if f.get("lang") != "en":
            return out
        scale = 1.0 if self.S.get("uniform_scale") else f.get("scale", 1.0)
        return INDIC_RUN.sub(lambda m: f'<span class="{self.fonts.cls(self._indic_chain(m.group(0)), 1 / scale)}">{m.group(0)}</span>', out)

    def _svg_text(self, s: str, f: dict) -> str:
        """Escaped SVG text; Indic runs inside a Latin label name their own font (SVG text cannot take a class
        from the chapter stylesheet in every reader, so the font-family is inline)."""
        out = E(s)
        if f.get("lang") != "en":
            return out
        return INDIC_RUN.sub(
            lambda m: f'<tspan style="font-family: {_attr(font_family_css(self._indic_chain(m.group(0))))}">{m.group(0)}</tspan>', out
        )

    def _lang(self, f: dict) -> str:
        """lang attributes for a text: Sanskrit is tagged with its script ("sa-Knda"), other text by language."""
        lang = f.get("lang", "")
        if f.get("text_lang") == "sa":
            lang = self.sa_tag
        return f' lang="{_attr(lang)}" xml:lang="{_attr(lang)}"' if lang else ""

    def _lines(self, lines: list[str], f: dict) -> str:
        return "".join(f'<p class="line">{self._text(l, f)}</p>' for l in lines) or '<p class="line"> </p>'

    # ---- one block

    def _align(self, b: dict) -> str:
        ha, ba = self.S.get("heading_align"), self.S.get("body_align")
        if b.get("reflow") and b["type"] == "prose":
            return "left"
        if b["type"] == "heading" and ha:
            return ha[min(max(b["level"], 1), len(ha)) - 1]
        if b["type"] != "heading" and ba:
            return ba
        return b.get("align", "left")

    def _heading_size(self, level: int) -> float:
        sizes = self.S.get("heading_sizes", [1.43, 1.2, 1.08])
        return sizes[min(max(level, 1), len(sizes)) - 1]

    def _body(self, b: dict, src: dict) -> str:
        """Content of one block for one text source (the target, or the Devanagari original)."""
        f = {"font": src["font"], "lang": src["lang"], "scale": src.get("scale", 1.0)}
        t = b["type"]
        if t == "heading":
            lvl = min(max(b["level"], 1), 6)
            size = self._heading_size(b["level"])
            inner = "<br/>".join(self._text(l, f) for l in src["lines"])
            return f'<h{lvl} style="font-size: {size:.4g}em">{inner}</h{lvl}>'
        if t == "prose" and b.get("reflow"):
            just = " justify" if self.S.get("justify", True) else ""
            return f'<p class="prose{just}">{self._text(" ".join(src["lines"]), f)}</p>'
        if t in ("verse", "prose"):
            return self._lines(src["lines"], f)
        if t == "list_item":
            marker = src.get("marker", "•")
            return (f'<div class="item"><span class="marker">{self._text(marker, f)}</span>'
                    f'<span class="itext">{self._text(" ".join(src["lines"]), f)}</span></div>')
        if t == "fill_blank":
            rows = []
            for segs in src.get("segs", []):
                rows.append("".join(
                    f'<span class="blank" style="width: {s["blank"]:.3g}em"></span>' if "blank" in s else self._text(s["t"], f)
                    for s in segs
                ))
            return "".join(f'<p class="line">{r}</p>' for r in rows)
        if t == "table":
            return self._table(b, src, f)
        return self._lines(src["lines"], f)

    def _table(self, b: dict, src: dict, f: dict) -> str:
        lay = b.get("table") or {"ruled": True, "widths": [], "last_right": False, "header": False}
        rows = src["rows"]
        n = max((len(r) for r in rows), default=1)
        cell = lambda c: "<br/>".join(self._text(x, f) for x in c.split("\n"))  # noqa: E731
        out = [f'<table class="{"ruled" if lay["ruled"] else "plain"}">']
        body_rows = rows
        if lay.get("header") and rows:
            out.append("<thead><tr>" + "".join(f"<th>{cell(c)}</th>" for c in rows[0] + [""] * (n - len(rows[0]))) + "</tr></thead>")
            body_rows = rows[1:]
        out.append("<tbody>")
        for r in body_rows:
            cells = list(r) + [""] * (n - len(r))
            out.append("<tr>" + "".join(
                f'<td{" class=\"last\"" if lay.get("last_right") and i == n - 1 else ""}>{cell(c)}</td>' for i, c in enumerate(cells)
            ) + "</tr>")
        out.append("</tbody></table>")
        return "".join(out)

    def _image(self, b: dict) -> str:
        width = b.get("width", 1.0) * 100
        if b.get("image") is None:
            return f'<div class="figure"><div class="placeholder" style="width: {width:.3g}%; margin: 0 auto">image not available</div></div>'
        rel = b["image"]
        self.images[rel] = self.build_dir / rel
        labels = b.get("labels") or []
        if not labels:
            return f'<div class="figure"><img src="{_attr(rel)}" alt="" style="width: {width:.3g}%"/></div>'
        # Labels are set over the drawing inside an SVG, so their size and place scale with the image.
        W = 1000.0
        H = W * b["aspect"]
        parts = [
            f'<svg xmlns="{SVG_NS}" xmlns:xlink="{XLINK_NS}" viewBox="0 0 {W:.0f} {H:.0f}" style="width: {width:.3g}%" role="img">',
            f'<image xlink:href="{_attr(rel)}" x="0" y="0" width="{W:.0f}" height="{H:.0f}" preserveAspectRatio="none"/>',
        ]
        fits = []
        for lab in labels:
            bw, bh = lab["w"] * W, lab["h"] * H
            fits.append(min(bh * 0.8, bw * 0.94 / _est_width(lab["text"])))
        typical = sorted(fits)[len(fits) // 2]
        for lab, fit in zip(labels, fits):
            size = min(fit, typical)
            cx, cy = (lab["x"] + lab["w"] / 2) * W, (lab["y"] + lab["h"] / 2) * H
            parts.append(
                f'<text x="{cx:.1f}" y="{cy:.1f}" font-size="{size:.1f}" text-anchor="middle" dominant-baseline="central" '
                f'style="font-family: {_attr(font_family_css(lab["font"]))}"{self._lang(lab)}>{self._svg_text(lab["text"], lab)}</text>'
            )
            self.fonts.embed(lab["font"][0])
            for m in INDIC_RUN.finditer(lab["text"]):
                self.fonts.embed(self._indic_chain(m.group(0))[0])
        parts.append("</svg>")
        return f'<div class="figure">{"".join(parts)}</div>'

    def _meaning(self, m: dict, cls: str = "meaning") -> str:
        mf = {"font": m["font"], "lang": m["lang"], "scale": m.get("scale", 1.0)}
        size = self.S.get("meaning_size", 0.86)
        inner = "".join(f"<p>{self._text(l, mf)}</p>" for l in m["lines"])
        return f'<div class="{cls} {self._cls(mf)}"{self._lang(mf)} style="font-size: {size:.4g}em">{inner}</div>'

    def _gloss(self, m: dict) -> str:
        mf = {"font": m["font"], "lang": m["lang"], "scale": m.get("scale", 1.0)}
        size = self.S.get("gloss_size", 0.78)
        items = "; ".join(f"<i>{self._text(g[0], mf)}</i> — {self._text(g[1], mf)}" for g in m["gloss"] if len(g) >= 2)
        return f'<p class="gloss {self._cls(mf)}"{self._lang(mf)} style="font-size: {size:.4g}em">{items}</p>'

    def render_block(self, b: dict, page: int) -> str:
        bid = f"p{page:04d}-{_safe_id(b['id'])}"
        if b["type"] == "image":
            return f'<div id="{bid}">{self._image(b)}</div>'
        if b["type"] == "separator":
            return f'<hr class="sep" id="{bid}"/>'
        target = {"font": b["font"], "lang": b["lang"], "text_lang": b.get("text_lang"), "scale": b.get("scale", 1.0),
                  "lines": b["lines"], "rows": b["rows"], "segs": b.get("segs", []), "marker": b.get("marker", "•")}
        classes = ["block", self._align(b), self._cls(target)]
        if b.get("vedic"):
            classes.append("vedic")
        if b.get("tight_above"):
            classes.append("tight-above")
        if b.get("tight_below"):
            classes.append("tight-below")
        lang = self._lang(target)
        m = b.get("meaning")

        if b.get("mantra"):
            # A translated edition's mantra: cue line, the mantra beside a rule, its meaning and gloss.
            nf = b["note_font"]
            mf = {"font": nf["font"], "lang": nf["lang"], "scale": nf.get("scale", 1.0)}
            parts = [f'<div class="mantra" id="{bid}">']
            if b.get("cue"):
                parts.append(f'<p class="cue {self._cls(mf)}"{self._lang(mf)} style="font-size: {self.S.get("cue_size", 1.0):.4g}em">{self._text(b["cue"], mf)}</p>')
            ml = 1.42 + self.S.get("mantra_leading", self.S.get("leading_vedic", 0.55))
            parts.append(
                f'<div class="mtext {self._cls(target)}"{lang} style="font-size: {self.S.get("mantra_size", 1.0):.4g}em; line-height: {ml:.3g}">'
                f'{self._body(b, target)}</div>'
            )
            if m and m["lines"]:
                parts.append(self._meaning(m))
            if m and m["gloss"]:
                parts.append(self._gloss(m))
            parts.append("</div>")
            return "".join(parts)

        if m is not None:
            # A Sanskrit heading of a translated edition, with its meaning under it.
            inner = self._body(b, target) + (self._meaning(m) if m["lines"] else "")
            return f'<div class="{" ".join(classes)}" id="{bid}"{lang}>{inner}</div>'

        if b.get("orig") is None:
            return f'<div class="{" ".join(classes)}" id="{bid}"{lang}>{self._body(b, target)}</div>'

        # Side by side: the Devanagari original with the target. Stacked on a phone, two columns on a tablet.
        o = b["orig"]
        orig = {"font": o["font"], "lang": "sa-Deva", "scale": o.get("scale", 1.0), "lines": o["lines"], "rows": o["rows"],
                "segs": o.get("segs", []), "marker": o.get("marker", "•")}
        ocls, olang = self._cls(orig), self._lang(orig)
        classes.remove(self._cls(target))
        if b["type"] in ("verse", "prose") and len(o["lines"]) == len(b["lines"]):
            pairs = "".join(
                f'<div class="pair"><div class="orig {ocls}"{olang}>{self._lines([ol], orig)}</div>'
                f'<div class="tgt {self._cls(target)}"{lang}>{self._lines([tl], target)}</div></div>'
                for ol, tl in zip(o["lines"], b["lines"])
            )
            return f'<div class="{" ".join(classes)}" id="{bid}">{pairs}</div>'
        return (f'<div class="{" ".join(classes)}" id="{bid}"><div class="pair">'
                f'<div class="orig {ocls}"{olang}>{self._body(b, orig)}</div>'
                f'<div class="tgt {self._cls(target)}"{lang}>{self._body(b, target)}</div></div></div>')

    # ---- chapters

    def build_chapters(self) -> list[Chapter]:
        title = self.doc["title"] or "Book"
        chapters: list[Chapter] = []
        cur: Chapter | None = None
        pending_page: str | None = None  # a source page marker not yet written (moves into a new chapter)
        pending_num: int | None = None

        def new_chapter(name: str) -> Chapter:
            nonlocal cur
            cur = Chapter(len(chapters) + 1, name)
            chapters.append(cur)
            return cur

        first_page = True
        for p in self.doc["pages"]:
            num = p["page"]
            marker = f'<span epub:type="pagebreak" role="doc-pagebreak" id="srcp{num:04d}" aria-label="{num}" title="{num}"></span>'
            label = "" if first_page else f'<div class="srcpage">src p. {num}</div>'
            pending_page, pending_num = marker + label, num
            # A long chapter is cut at a page boundary.
            if cur is not None and cur.blocks >= MAX_CHAPTER_BLOCKS:
                new_chapter(f"{title} (contd.)")
            for b in p["blocks"]:
                is_split = (b["type"] == "heading" and b["level"] == self.top_level) or cur is None
                if is_split and (cur is None or cur.blocks > 0):
                    name = " ".join(b["lines"]).strip() if b["type"] == "heading" else title
                    new_chapter(name or title)
                elif is_split:
                    cur.title = " ".join(b["lines"]).strip() or cur.title
                if pending_page is not None:
                    cur.parts.append(pending_page)
                    cur.pages.append(pending_num)
                    pending_page = None
                if b["type"] == "heading":
                    cur.headings.append((b["level"], f"p{num:04d}-{_safe_id(b['id'])}", " ".join(b["lines"]).strip()))
                cur.parts.append(self.render_block(b, num))
                cur.blocks += 1
                if b["type"] == "image" and b.get("labels") and b.get("image") is not None:
                    cur.has_svg = True
            if pending_page is not None:
                # A source page with no body blocks (a bare page number) still gets its marker.
                if cur is None:
                    new_chapter(title)
                cur.parts.append(pending_page)
                cur.pages.append(pending_num)
                pending_page = None
            first_page = False
        if not chapters:
            new_chapter(title)
            cur.parts.append('<p class="line"> </p>')
        self.chapters = chapters
        return chapters

    def chapter_xhtml(self, ch: Chapter) -> str:
        main = self.doc["main"]
        lang = _attr(self.sa_tag)
        body_cls = self.fonts.cls(main["font"], 1.0)
        body = "\n".join(ch.parts)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<html xmlns="{XHTML_NS}" xmlns:epub="{EPUB_NS}" lang="{lang}" xml:lang="{lang}">\n'
            f"<head><meta charset=\"utf-8\"/><title>{E(ch.title)}</title>"
            '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
            f'<body class="{body_cls}" epub:type="bodymatter">\n<section>\n{body}\n</section>\n</body>\n</html>\n'
        )

    def title_xhtml(self, subtitle: str) -> str:
        main = self.doc["main"]
        lang = _attr(main.get("lang", ""))
        title = self.doc["title"] or "Book"
        latin = self.fonts.cls(self.doc["latin"]["font"], 1.0)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<html xmlns="{XHTML_NS}" xmlns:epub="{EPUB_NS}" lang="{lang}" xml:lang="{lang}">\n'
            f'<head><meta charset="utf-8"/><title>{E(title)}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
            f'<body class="{self.fonts.cls(main["font"], 1.0)}" epub:type="frontmatter titlepage">\n'
            f'<section><h1 class="title">{E(title)}</h1><p class="subtitle {latin}" lang="en" xml:lang="en">{E(subtitle)}</p></section>\n'
            "</body>\n</html>\n"
        )

    def nav_xhtml(self) -> str:
        latin = self.fonts.cls(self.doc["latin"]["font"], 1.0)
        main_cls = self.fonts.cls(self.doc["main"]["font"], 1.0)
        # Table of contents: top-level headings, with the next level nested under them. A heading before
        # the first top-level one, or a book without headings, is listed by chapter.
        toc: list[tuple[str, str, list[tuple[str, str]]]] = []  # (href, text, [(href, text)])
        for ch in self.chapters:
            if not ch.headings:
                if not toc or self.top_level is None:
                    toc.append((ch.href, ch.title, []))
                continue
            for level, anchor, text in ch.headings:
                href = f"{ch.href}#{anchor}"
                if level == self.top_level or not toc:
                    toc.append((href, text or ch.title, []))
                elif level == self.top_level + 1:
                    toc[-1][2].append((href, text))
        if not toc:
            toc.append((self.chapters[0].href, self.chapters[0].title, []))

        def li(href: str, text: str, children: list[tuple[str, str]] = ()) -> str:
            sub = f"<ol>{''.join(li(h, t) for h, t in children)}</ol>" if children else ""
            return f'<li><a href="{_attr(href)}">{E(text) or "…"}</a>{sub}</li>'

        pages = "".join(
            f'<li><a href="{ch.href}#srcp{n:04d}">{n}</a></li>' for ch in self.chapters for n in ch.pages
        )
        page_list = f'<nav epub:type="page-list" hidden=""><h1>Pages</h1><ol>{pages}</ol></nav>' if pages else ""
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<html xmlns="{XHTML_NS}" xmlns:epub="{EPUB_NS}" lang="en" xml:lang="en">\n'
            '<head><meta charset="utf-8"/><title>Contents</title><link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
            f'<body class="{main_cls}">\n'
            f'<nav epub:type="toc" id="toc"><h1 class="{latin}">Contents</h1><ol>{"".join(li(*t) for t in toc)}</ol></nav>\n'
            f"{page_list}\n"
            '<nav epub:type="landmarks" hidden=""><h1>Landmarks</h1><ol>'
            '<li><a epub:type="titlepage" href="title.xhtml">Title page</a></li>'
            '<li><a epub:type="toc" href="nav.xhtml">Contents</a></li>'
            f'<li><a epub:type="bodymatter" href="{self.chapters[0].href}">Start</a></li>'
            "</ol></nav>\n</body>\n</html>\n"
        )


def _est_width(text: str) -> float:
    """Rough advance width of a label in em, without shaping: Indic aksharas are about twice as wide as Latin
    letters, a virama fuses the next consonant into a conjunct, and marks add nothing."""
    import unicodedata

    w = 0.0
    for ch in text:
        o = ord(ch)
        if ch == " ":
            w += 0.3
        elif unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            w += 0.0
        elif unicodedata.category(ch) == "Mc":
            w += 0.35  # a spacing vowel sign (ಾ, ೀ) is narrow
        elif 0x0900 <= o <= 0x0DFF:
            w += 0.95
        else:
            w += 0.55
    w -= 0.45 * text.count("\u094d") + 0.45 * sum(text.count(chr(v)) for v in (0x0BCD, 0x0C4D, 0x0CCD))  # viramas
    return max(w, 0.55)


def _safe_id(s: str) -> str:
    """An XML id fragment: anything but letters, digits, - and _ is spelled out so "a/b" and "a?b" differ."""
    return re.sub(r"[^A-Za-z0-9_-]", lambda m: f"_{ord(m.group(0)):x}_", s)


# ---------------------------------------------------------------- package


def book_identifier(master: Master, script: str, translation: str | None, side_by_side: bool) -> str:
    """A stable urn:uuid for one edition: re-rendering the same book gives the same id, so a reader
    (Apple Books) treats the new file as an update of the book it already has."""
    key = f"vedalipi:{master.source}:{master.title}:{script}:{translation or ''}:{int(side_by_side)}"
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, key)}"


def language_tags(script: str, translation: str | None) -> list[str]:
    from .. import transliterate

    tag = SCRIPT_TAGS.get(script) or ("sa-Latn" if transliterate.is_roman(script) else "sa")
    return [translation, tag] if translation else [tag]


def package_opf(
    *, title: str, identifier: str, languages: list[str], modified: str, source: str, description: str,
    chapters: list[Chapter], fonts: FontSet, images: list[str], extra: list[tuple[str, str]],
) -> str:
    items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="titlepage" href="title.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    for ch in chapters:
        props = ' properties="svg"' if ch.has_svg else ""
        items.append(f'<item id="ch{ch.index:03d}" href="{ch.href}" media-type="application/xhtml+xml"{props}/>')
    for i, rel in enumerate(sorted(fonts.files)):
        items.append(f'<item id="font{i}" href="{_attr(rel)}" media-type="font/ttf"/>')
    for i, rel in enumerate(sorted(images)):
        items.append(f'<item id="img{i}" href="{_attr(rel)}" media-type="image/png"/>')
    for i, (rel, mime) in enumerate(extra):
        items.append(f'<item id="extra{i}" href="{_attr(rel)}" media-type="{_attr(mime)}"/>')
    # Title page, the contents page (the nav document doubles as a visible contents page), then the chapters.
    spine = '<itemref idref="titlepage"/><itemref idref="nav"/>' + "".join(f'<itemref idref="ch{ch.index:03d}"/>' for ch in chapters)
    langs = "".join(f"<dc:language>{E(l)}</dc:language>" for l in languages)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<package xmlns="{OPF_NS}" version="3.0" unique-identifier="bookid" xml:lang="{E(languages[0])}" prefix="{IBOOKS_PREFIX}">\n'
        f'<metadata xmlns:dc="{DC_NS}">\n'
        f'<dc:identifier id="bookid">{E(identifier)}</dc:identifier>\n'
        f"<dc:title>{E(title)}</dc:title>\n{langs}\n"
        f"<dc:source>{E(source)}</dc:source>\n<dc:description>{E(description)}</dc:description>\n"
        f'<meta property="dcterms:modified">{E(modified)}</meta>\n'
        '<meta property="rendition:layout">reflowable</meta>\n'
        '<meta property="ibooks:specified-fonts">true</meta>\n'
        "</metadata>\n"
        f'<manifest>\n{chr(10).join(items)}\n</manifest>\n'
        f"<spine>{spine}</spine>\n</package>\n"
    )


def _check_xml(name: str, text: str) -> None:
    """Every XHTML/XML file must parse: a reader that meets a broken file shows nothing or refuses the book."""
    try:
        ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as e:
        raise ValueError(f"epub: {name} is not well-formed XML: {e}") from e


def write_epub(
    master: Master, doc: dict, out_epub: Path, *, script: str, build_dir: Path, font_dirs: list[str],
    translation: str | None = None, side_by_side: bool = False, modified: datetime | None = None,
) -> Path:
    """Write the EPUB for a built document tree. `font_dirs` are searched for the font files to embed."""
    fonts = FontSet([Path(d) for d in font_dirs])
    fonts.embed("Noto Serif")  # digits, "src p." labels and the contents page
    builder = EpubBuilder(doc, fonts, build_dir)
    chapters = builder.build_chapters()

    edition = script.capitalize() + (" · translated" if translation else "") + (" · with Devanagari" if side_by_side else "")
    subtitle = f"{edition} edition, transliterated by vedalipi"
    files: dict[str, bytes] = {}
    for ch in chapters:
        files[f"OEBPS/{ch.href}"] = builder.chapter_xhtml(ch).encode("utf-8")
    files["OEBPS/title.xhtml"] = builder.title_xhtml(subtitle).encode("utf-8")
    files["OEBPS/nav.xhtml"] = builder.nav_xhtml().encode("utf-8")
    files["OEBPS/style.css"] = (fonts.css() + "\n" + BASE_CSS).encode("utf-8")
    files["OEBPS/master.json"] = (master.to_json() + "\n").encode("utf-8")
    for name, data in list(files.items()):
        if name.endswith(".xhtml"):
            _check_xml(name, data.decode("utf-8"))

    title = doc["title"] or Path(master.source).stem or "Book"
    stamp = (modified or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    opf = package_opf(
        title=title, identifier=book_identifier(master, script, translation, side_by_side),
        languages=language_tags(script, translation), modified=stamp, source=master.source or title,
        description=f"{subtitle}. The Devanagari master (master.json) is included in the package.",
        chapters=chapters, fonts=fonts, images=list(builder.images),
        extra=[("master.json", "application/json")] + ([("fonts/OFL.txt", "text/plain")] if _ofl(font_dirs) else []),
    )
    _check_xml("content.opf", opf)

    out_epub = Path(out_epub)
    out_epub.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_epub.with_name(f".{out_epub.name}.tmp")
    stamp_zip = (1980, 1, 1, 0, 0, 0)  # fixed entry times: the same book gives byte-identical files
    with zipfile.ZipFile(tmp, "w") as z:
        info = zipfile.ZipInfo("mimetype", date_time=stamp_zip)
        z.writestr(info, b"application/epub+zip", compress_type=zipfile.ZIP_STORED)  # first, uncompressed
        def put(name: str, data: bytes) -> None:
            zi = zipfile.ZipInfo(name, date_time=stamp_zip)
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
        put("META-INF/container.xml", CONTAINER_XML.encode("utf-8"))
        put("META-INF/com.apple.ibooks.display-options.xml", APPLE_DISPLAY_OPTIONS.encode("utf-8"))
        put("OEBPS/content.opf", opf.encode("utf-8"))
        for name, data in files.items():
            put(name, data)
        for rel, src in sorted(fonts.files.items()):
            put(f"OEBPS/{rel}", Path(src).read_bytes())
        if (ofl := _ofl(font_dirs)) is not None:
            put("OEBPS/fonts/OFL.txt", ofl.read_bytes())
        for rel, src in sorted(builder.images.items()):
            put(f"OEBPS/{rel}", Path(src).read_bytes())
    tmp.replace(out_epub)
    return out_epub


def _ofl(font_dirs: list[str]) -> Path | None:
    for d in font_dirs:
        p = Path(d) / "OFL.txt"
        if p.exists():
            return p
    return None


def render_epub(
    master: Master,
    target: Master,
    out_epub: Path,
    *,
    script: str,
    work_dir: Path,
    side_by_side: bool = False,
    fonts_dir: Path | None = None,
    meanings: dict | None = None,
    meaning_lang: str = "en",
    translated: bool = False,
    build_name: str | None = None,
    translation: str | None = None,
) -> Path:
    """Render `target` (the transliterated master) as a reflowable EPUB 3 next to, or instead of, the PDF.

    Takes the same arguments as render_pdf and lays out the same document tree (build_document), so the
    EPUB holds exactly the text of the PDF: printed line breaks kept, one output line per printed line,
    wider line pitch for svara-marked text, image crops from the scan, the Devanagari master inside the
    package. The vendored fonts (with the svara-emphasised Vedalipi copies) are embedded and Apple Books is
    told to use them, because the system fonts of an iPad or iPhone cannot place Vedic marks on Kannada or
    Telugu letters.
    """
    from . import FONTS_DIR, build_dir_for, font_dirs

    fonts_dir = Path(fonts_dir) if fonts_dir is not None else FONTS_DIR
    build = build_dir_for(work_dir, f"{build_name or script}-epub", side_by_side)
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)
    doc = build_document(
        master, target, script=script, side_by_side=side_by_side, work_dir=Path(work_dir), build_dir=build,
        meanings=meanings, meaning_lang=meaning_lang, translated=translated,
    )
    (build / "data.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return write_epub(
        master, doc, out_epub, script=script, build_dir=build, font_dirs=font_dirs(fonts_dir),
        translation=translation if translated else None, side_by_side=side_by_side,
    )


__all__ = ["MAX_CHAPTER_BLOCKS", "SCRIPT_TAGS", "render_epub", "write_epub"]
