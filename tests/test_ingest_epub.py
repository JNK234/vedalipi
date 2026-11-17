# ABOUTME: Tests for EPUB input: spine documents become pages of blocks, languages follow the text's script, no OCR runs.
# ABOUTME: Covers block mapping, pagination, language rules, sloppy HTML, images, vedalipi round trips and the pipeline.

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pymupdf
import pytest
from PIL import Image
from test_render import fixture_master

from vedalipi import ingest, pipeline, transliterate
from vedalipi.ingest_epub import (
    MAX_BLOCKS_PER_PAGE,
    SOFT_BLOCKS_PER_PAGE,
    LangRules,
    _split_long,
    dominant_script,
    load_epub_pages,
)
from vedalipi.render.epub import render_epub
from vedalipi.schema import Correction, Master

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">x</dc:identifier><dc:title>{title}</dc:title><dc:language>sa</dc:language></metadata>
<manifest>{items}</manifest>
<spine>{spine}</spine>
</package>"""

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>
<rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""


def xhtml(body: str, lang: str = "") -> str:
    lang_attr = f' lang="{lang}" xml:lang="{lang}"' if lang else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
            f'xmlns:epub="http://www.idpf.org/2007/ops"{lang_attr}><head><title>t</title></head><body>{body}</body></html>')


def make_epub(path: Path, docs: list[tuple[str, str]], *, title: str = "Test Book", images: dict[str, bytes] | None = None,
              extra: dict[str, bytes] | None = None) -> Path:
    """A minimal EPUB: `docs` are (name, xhtml) in spine order under OPS/; images and extra files go beside them."""
    items, spine = [], []
    for i, (name, _) in enumerate(docs):
        items.append(f'<item id="d{i}" href="{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="d{i}"/>')
    for i, name in enumerate(images or {}):
        items.append(f'<item id="i{i}" href="{name}" media-type="image/png"/>')
    for i, name in enumerate(extra or {}):
        items.append(f'<item id="x{i}" href="{name}" media-type="application/json"/>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OPS/book.opf", OPF.format(title=title, items="".join(items), spine="".join(spine)))
        for name, text in docs:
            z.writestr(f"OPS/{name}", text)
        for name, data in (images or {}).items():
            z.writestr(f"OPS/{name}", data)
        for name, data in (extra or {}).items():
            z.writestr(f"OPS/{name}", data)
    return path


def png(w: int = 60, h: int = 40, color=(200, 30, 30)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


PUBLISHER_BODY = """
<h1 class="center">शान्तिपाठः</h1>
<p class="verse">ओं भ॒द्रं कर्णे॑भिः शृणु॒याम॑ देवाः ।<br/>भ॒द्रं प॑श्येमा॒क्षभि॒र्यज॑त्राः ।<br/>
स्थि॒रैरङ्गै᳚स्तुष्टु॒वाꣳस॑स्त॒नूभिः॑ ॥ १ ॥</p>
<p>अथ संकल्पः । मम उपात्त समस्त दुरितक्षयद्वारा श्री परमेश्वर प्रीत्यर्थं करिष्ये ।</p>
<p>........ नक्षत्रे ........ राशौ जातस्य</p>
<ol><li>गणपति पूजा</li><li>कलश पूजा</li></ol>
<ul><li>प्रथमम्</li></ul>
<table><tr><th>प्रार्थना</th><th>प्रतिवचनम्</th></tr><tr><td>स्व॒स्ति न॒ इन्द्रो॑</td><td>तथास्तु</td></tr></table>
<hr/>
<p>ఈ మంత్రమును మూడు సార్లు చెప్పవలెను.</p>
<p>Note: recite with svaras</p>
<span epub:type="pagebreak" id="pg12" title="12"/>
<h2>प्रार्थना</h2>
<div><p>त्र्य॑म्बकं यजामहे सु॒गन्धिं॑ पुष्टि॒वर्ध॑नम् ।</p><p>उ॒र्वा॒रु॒कमि॑व॒ बन्ध॑नान्मृ॒त्योर्मु॑क्षीय॒ मा॒ऽमृता॑त् ॥</p></div>
<img src="img/fig.png" alt="yantra"/>
"""


@pytest.fixture
def publisher_epub(tmp_path) -> Path:
    return make_epub(tmp_path / "pub.epub", [("ch1.xhtml", xhtml(PUBLISHER_BODY, lang="en"))], images={"img/fig.png": png()})


def test_input_kind_and_title(publisher_epub):
    assert ingest.input_kind(publisher_epub) == "epub"
    assert ingest.document_title(publisher_epub) == "Test Book"
    assert ingest.document_title(Path("/x/scan.pdf")) == "scan"


def test_blocks_map_from_html(publisher_epub, tmp_path):
    pages = ingest.load_text_pages(publisher_epub, assets_dir=tmp_path / "assets")
    assert [p.page for p in pages] == [1, 2]
    b = pages[0].blocks
    kinds = [(x.type, x.lang) for x in b]
    assert kinds == [
        ("heading", "sa"), ("verse", "sa"), ("prose", "sa"), ("fill_blank", "sa"), ("list_item", "sa"),
        ("list_item", "sa"), ("list_item", "sa"), ("table", "sa"), ("separator", "en"), ("prose", "te"), ("prose", "en"),
    ]
    assert b[0].level == 1 and b[0].align == "center"
    # <br> breaks are printed lines; whitespace around them is collapsed away.
    assert b[1].lines == ["ओं भ॒द्रं कर्णे॑भिः शृणु॒याम॑ देवाः ।", "भ॒द्रं प॑श्येमा॒क्षभि॒र्यज॑त्राः ।",
                          "स्थि॒रैरङ्गै᳚स्तुष्टु॒वाꣳस॑स्त॒नूभिः॑ ॥ १ ॥"]
    assert b[2].lines == ["अथ संकल्पः । मम उपात्त समस्त दुरितक्षयद्वारा श्री परमेश्वर प्रीत्यर्थं करिष्ये ।"]
    # Ordered lists get their printed numbers; unordered items are left for the renderer's bullet.
    assert b[4].lines == ["1. गणपति पूजा"] and b[5].lines == ["2. कलश पूजा"] and b[6].lines == ["प्रथमम्"]
    assert b[7].rows == [["प्रार्थना", "प्रतिवचनम्"], ["स्व॒स्ति न॒ इन्द्रो॑", "तथास्तु"]]
    assert [x.id for x in b] == [f"b{i}" for i in range(1, len(b) + 1)]
    # The second page: opened by the page-break marker, which also gives its printed number.
    p2 = pages[1].blocks
    assert [(x.type, x.lang) for x in p2] == [("heading", "sa"), ("verse", "sa"), ("image", "en"), ("page_number", "en")]
    assert p2[0].level == 2
    # A wrapper div of <p> lines is one verse.
    assert p2[1].lines == ["त्र्य॑म्बकं यजामहे सु॒गन्धिं॑ पुष्टि॒वर्ध॑नम् ।", "उ॒र्वा॒रु॒कमि॑व॒ बन्ध॑नान्मृ॒त्योर्मु॑क्षीय॒ मा॒ऽमृता॑त् ॥"]
    assert p2[3].lines == ["12"]
    # The image sits on the page canvas written under assets; its bbox is its strip of that canvas.
    assert pages[1].source_image.endswith("assets/p0002.png") and Path(pages[1].source_image).is_file()
    x0, y0, x1, y1 = p2[2].bbox
    assert x0 == 0.0 and x1 == 1.0 and 0 < y0 < y1 < 1
    assert not pages[0].source_image


def test_images_are_dropped_without_an_assets_dir(publisher_epub):
    pages = ingest.load_text_pages(publisher_epub)
    assert all(b.type != "image" for p in pages for b in p.blocks)


def test_several_images_share_one_canvas(tmp_path):
    body = '<p>नमः</p><img src="a.png"/><p>शिवाय</p><img src="b.png"/>'
    epub = make_epub(tmp_path / "i.epub", [("c.xhtml", xhtml(body))], images={"a.png": png(60, 40), "b.png": png(120, 30, (0, 0, 200))})
    pages = load_epub_pages(epub, assets_dir=tmp_path / "assets")
    imgs = [b for b in pages[0].blocks if b.type == "image"]
    assert len(imgs) == 2 and imgs[0].bbox[3] <= imgs[1].bbox[1]
    with Image.open(pages[0].source_image) as im:
        w, h = im.size
        # The first image (red) fills the upper strip, the second (blue) the lower one.
        assert im.getpixel((w // 2, int((imgs[0].bbox[1] + imgs[0].bbox[3]) / 2 * h)))[0] > 150
        assert im.getpixel((w // 2, int((imgs[1].bbox[1] + imgs[1].bbox[3]) / 2 * h)))[2] > 150


def test_pages_follow_spine_documents_when_the_book_has_no_markers(tmp_path):
    docs = [("a.xhtml", xhtml("<p>एकम्</p>")), ("b.xhtml", xhtml("<p>द्वे</p><p>त्रीणि</p>")), ("nav.xhtml", xhtml("<nav><ol><li>x</li></ol></nav>"))]
    pages = load_epub_pages(make_epub(tmp_path / "d.epub", docs))
    assert [[b.lines[0] for b in p.blocks] for p in pages] == [["एकम्"], ["द्वे", "त्रीणि"]]


def test_long_document_is_cut_into_pages(tmp_path):
    paras = "".join(f"<p>पदम् {i}</p>" for i in range(SOFT_BLOCKS_PER_PAGE + 5))
    paras += "<h2>शीर्षकम्</h2>" + "".join(f"<p>पदम् {i}</p>" for i in range(MAX_BLOCKS_PER_PAGE + 3))
    pages = load_epub_pages(make_epub(tmp_path / "l.epub", [("a.xhtml", xhtml(paras))]))
    assert len(pages) == 3
    assert len(pages[0].blocks) == SOFT_BLOCKS_PER_PAGE + 5  # cut early, at the heading
    assert pages[1].blocks[0].type == "heading" and len(pages[1].blocks) == MAX_BLOCKS_PER_PAGE
    assert len(pages[2].blocks) == 4
    assert sum(len(p.blocks) for p in pages) == SOFT_BLOCKS_PER_PAGE + 5 + 1 + MAX_BLOCKS_PER_PAGE + 3


def test_sloppy_html_is_read(tmp_path):
    sloppy = ("<html><body><p>अग्निः&nbsp;स्वस्ति<p>यज्ञः &amp; सोमः<br>द्वितीया<div>Hello &mdash; world</div>"
              "<p style='text-align:center'>ॐ</p></body></html>")
    pages = load_epub_pages(make_epub(tmp_path / "s.epub", [("a.html", sloppy)]))
    b = pages[0].blocks
    assert [x.lines for x in b] == [["अग्निः स्वस्ति"], ["यज्ञः & सोमः", "द्वितीया"], ["Hello — world"], ["ॐ"]]
    assert b[3].align == "center" and b[2].lang == "en"


def test_language_rules():
    assert dominant_script("ओं भद्रं 12 ।") == "devanagari" and dominant_script("Hello") == "latin"
    assert dominant_script("ఈ మంత్రమును") == "telugu" and dominant_script("12 ॥") is None
    deva = LangRules()
    assert deva.lang_for("ओं भद्रं", "en") == "sa"  # a document-wide lang="en" does not make Devanagari English
    assert deva.lang_for("ओं भद्रं", "hi") == "sa"  # no --languages: the book is Sanskrit only, whatever it declares
    assert LangRules(languages=("hi", "sa")).lang_for("ओं भद्रं", "hi") == "hi"  # declared and listed: honoured
    assert LangRules(languages=("hi", "sa")).lang_for("ओं भद्रं", "") == "sa"  # undeclared Devanagari is Sanskrit
    assert LangRules(languages=("hi",)).lang_for("ओं भद्रं", "") == "hi"
    assert deva.lang_for("ఈ మంత్రమును", "") == "te" and deva.lang_for("Note", "") == "en" and deva.lang_for("12", "") == "en"
    # A book printed in Kannada script: Sanskrit only, or Sanskrit with Kannada instructions.
    kn = LangRules(source_script="kannada")
    assert kn.lang_for("ಅಚ್ಯುತಾಯ ನಮಃ", "kn") == "sa"
    assert LangRules(source_script="kannada", languages=("te", "sa")).lang_for("ಅಚ್ಯುತಾಯ ನಮಃ", "kn") == "sa"
    both = LangRules(source_script="kannada", languages=("kn", "sa"))
    assert both.lang_for("ಅಚ್ಯುತಾಯ ನಮಃ", "") == "sa"  # visarga-final words read as a mantra
    assert both.lang_for("ಈ ಮಂತ್ರವನ್ನು ಮೂರು ಬಾರಿ ಹೇಳಬೇಕು", "") == "kn"
    assert both.lang_for("ಈ ಮಂತ್ರವನ್ನು", "kn") == "kn" and both.lang_for("ಅಚ್ಯುತಾಯ", "sa") == "sa"
    assert LangRules(source_script="kannada", languages=("kn",)).lang_for("ಅಚ್ಯುತಾಯ ನಮಃ", "") == "kn"
    # Roman Sanskrit (an IAST book) is Sanskrit when no Latin-script language is listed.
    assert LangRules(source_script="iast").lang_for("oṃ bhūr bhuvaḥ", "") == "sa"
    assert LangRules(source_script="iast", languages=("en", "sa")).lang_for("Recite three times.", "") == "en"
    assert LangRules(source_script="iast", languages=("en", "sa")).lang_for("oṃ bhūr bhuvaḥ svāhā", "") == "sa"
    # A book-wide lang="sa" (e.g. Pandoc's YAML "lang: sa" front matter, inherited by every paragraph) must
    # not make unrelated scripts Sanskrit: "sa" is only plausible where the book's Sanskrit is actually written.
    assert deva.lang_for("Nitya Sandhyavandanam", "sa") == "en"
    assert deva.lang_for("ఈ మంత్రమును మూడు సార్లు", "sa") == "te"
    assert LangRules(source_script="kannada").lang_for("Hello world", "sa") == "en"


def test_long_prose_splits_at_dandas():
    line = " ".join(f"पदम् {i} ।" for i in range(30))
    parts = _split_long(line)
    assert len(parts) > 1 and " ".join(parts) == line and all(len(p) <= 110 for p in parts)
    assert _split_long("short ।") == ["short ।"]
    # Never before a verse number.
    assert _split_long("x" * 100 + " । १ ॥ " + "y" * 50) == ["x" * 100 + " । १ ॥", "y" * 50]


def test_vedalipi_epub_round_trips_its_master(tmp_path):
    master = fixture_master()
    target = transliterate.transliterate_master(master, "kannada")
    epub = render_epub(master, target, tmp_path / "f.epub", script="kannada", work_dir=tmp_path / "work")
    pages = ingest.load_text_pages(epub)
    assert pages == master.pages
    assert ingest.document_title(epub) == master.title


def test_vedalipi_epub_without_its_master_is_read_back_from_the_script(tmp_path):
    master = fixture_master()
    target = transliterate.transliterate_master(master, "telugu")
    epub = render_epub(master, target, tmp_path / "f.epub", script="telugu", work_dir=tmp_path / "work")
    # Strip the embedded master so the text itself has to be read.
    stripped = tmp_path / "s.epub"
    with zipfile.ZipFile(epub) as src, zipfile.ZipFile(stripped, "w") as dst:
        for info in src.infolist():
            if info.filename.endswith("master.json"):
                continue
            data = src.read(info.filename)
            if info.filename.endswith("content.opf"):
                data = b"\n".join(l for l in data.splitlines() if b"master.json" not in l)
            dst.writestr(info, data)
    pages = load_epub_pages(stripped, source_script="telugu", languages=("ta", "en", "sa"))
    # Two source pages, found through the page-break markers; the title page is a page of its own.
    assert len(pages) == 3 and pages[1].blocks[-1].type == "page_number" and pages[1].blocks[-1].lines == ["7"]
    # The Telugu text reads back as Telugu-script Sanskrit; normalize will turn it into Devanagari.
    v1 = next(b for b in pages[1].blocks if b.type == "verse" and len(b.lines) == 4)
    assert v1.lang == "sa" and v1.lines[0] == target.page(7).block("v1").lines[0]
    assert transliterate.transliterate_text(master.page(7).block("v1").lines[1], "telugu") == v1.lines[1]
    # A fill-in line keeps its blanks; the Tamil note and the English note keep their languages.
    assert any(b.type == "fill_blank" and "........" in b.lines[0] for b in pages[1].blocks)
    assert {b.lang for b in pages[1].blocks + pages[2].blocks} >= {"sa", "ta", "en"}


def test_pipeline_runs_an_epub_without_ocr_and_renders_it(publisher_epub, tmp_path):
    cfg = pipeline.RunConfig(input_path=publisher_epub, out_dir=tmp_path / "out", script="kannada", jobs=1, formats=("pdf", "epub"))
    runner, outputs = pipeline.run(cfg)
    assert not runner.failed and {s for _, s in runner.ran} == {"ingest", "normalize", "validate"}
    master = Master.load(cfg.master_path)
    assert master.title == "Test Book" and len(master.pages) == 2
    assert outputs["pdf"].name == "pub.kannada.pdf" and outputs["epub"].name == "pub.kannada.epub"
    text = "".join(pg.get_text() for pg in pymupdf.open(outputs["pdf"]))
    assert transliterate.transliterate_text("शान्तिपाठः", "kannada") in text
    assert "ఈ మంత్రమును" in text  # Telugu instructions pass through untouched
    # The image canvas is cropped into the PDF (no "image not available" placeholder).
    assert "image not available" not in text and any(pg.get_images() for pg in pymupdf.open(outputs["pdf"]))
    # A second run is fully cached; changing --languages re-tags the blocks, so ingest runs again.
    runner2, _ = pipeline.run(cfg)
    assert not runner2.ran
    cfg.languages = ("hi",)
    runner3, _ = pipeline.run(cfg)
    assert ("ingest" in {s for _, s in runner3.ran})
    assert Master.load(cfg.master_path).page(1).block("b1").lang == "hi"


def test_source_script_book_becomes_a_devanagari_master(tmp_path):
    body = "<h1>ಸಂಧ್ಯಾ</h1><p>ಅಚ್ಯುತಾಯ ನಮಃ<br/>ಅನಂತಾಯ ನಮಃ</p>"
    epub = make_epub(tmp_path / "kn.epub", [("a.xhtml", xhtml(body))])
    cfg = pipeline.RunConfig(input_path=epub, out_dir=tmp_path / "out", script="telugu", jobs=1, source_script="kannada")
    runner, outputs = pipeline.run(cfg)
    assert not runner.failed
    page = Master.load(cfg.master_path).page(1)
    assert page.block("b2").lines == ["अच्युताय नमः", "अनंताय नमः"] and page.block("b1").lines == ["संध्या"]


def test_corrections_can_fix_a_block_language(tmp_path):
    master = Master(title="t", pages=ingest.load_text_pages(make_epub(tmp_path / "a.epub", [("a.xhtml", xhtml("<p>ಈ ಮಂತ್ರ</p>"))])))
    assert master.page(1).block("b1").lang == "kn"
    fixed = pipeline.apply_corrections(master, [Correction(page=1, block_id="b1", lang="sa")])
    assert fixed.page(1).block("b1").lang == "sa"


def test_empty_or_broken_epubs_raise(tmp_path):
    with pytest.raises(ValueError, match="no text"):
        load_epub_pages(make_epub(tmp_path / "e.epub", [("a.xhtml", xhtml("<nav><p>only nav</p></nav>"))]))
    with pytest.raises(ValueError, match="no XHTML"):
        load_epub_pages(make_epub(tmp_path / "n.epub", []))
    with pytest.raises(zipfile.BadZipFile):
        (tmp_path / "bad.epub").write_bytes(b"not a zip")
        load_epub_pages(tmp_path / "bad.epub")


PANDOC_MD = """---
title: "Nitya Sandhyavandanam"
lang: sa
---

# Sandhyavandanam

## Introduction

This manual explains the daily ritual of Sandhyavandanam, to be recited facing the sun. Sip water
three times before beginning.

## Pratah Sandhya

### Achamanam

मम उपात्त समस्त दुरितक्षय द्वारा श्री परमेश्वर\\
प्रीत्यर्थं प्रातः सन्ध्यावन्दनं करिष्ये ।

### Gayatri Mantra

> ॐ भूर्भुवः॒ स्वः॑ ।\\
> तत्स॑वि॒तुर्वरे॑ण्यं॒ भर्गो॑ दे॒वस्य॑ धीमहि ।\\
> धियो॒ यो नः॑ प्रचो॒दया॑त् ॥

Recite this verse three times, touching the sacred thread.[^gayatri]

[^gayatri]: The Gayatri mantra is addressed to Savitr, the solar deity.

### Names to be recited

1. केशवाय नमः
2. नारायणाय नमः

### Correspondence table

| Sanskrit | Meaning |
|---|---|
| सूर्यः | the sun |
| अग्निः | fire |

## Instructions in Telugu

ఈ మంత్రమును మూడు సార్లు చెప్పవలెను.

## Fill in your lineage

अभिवादये ........ गोत्रः ........ नाम्नाहं अस्मि भोः ।
"""


@pytest.fixture(scope="module")
def pandoc_epub(tmp_path_factory):
    """A real EPUB built by Pandoc (not vedalipi's own writer or a hand-rolled fixture), so the parser is
    tested against an independent tool's structure: <!DOCTYPE html>, <section> chapters, footnotes as
    <aside epub:type="footnote">, a book-wide lang="sa" inherited onto every paragraph, a smart-quoted
    ellipsis fill-in blank, and Pandoc's own nav/OPF shape."""
    import shutil
    import subprocess

    if shutil.which("pandoc") is None:
        pytest.skip("pandoc is not installed")
    d = tmp_path_factory.mktemp("pandoc")
    (d / "book.md").write_text(PANDOC_MD, encoding="utf-8")
    out = d / "book.epub"
    subprocess.run(["pandoc", str(d / "book.md"), "-o", str(out), "--split-level=2"], check=True, capture_output=True)
    return out


def test_reads_a_real_pandoc_epub(pandoc_epub, tmp_path):
    pages = load_epub_pages(pandoc_epub, assets_dir=tmp_path / "assets")
    all_blocks = [b for p in pages for b in p.blocks]
    by_text = {" ".join(b.lines): b for b in all_blocks if b.lines}
    # English headings and prose stay English, despite the book-wide lang="sa".
    assert by_text["Sandhyavandanam"].lang == "en" and by_text["Introduction"].lang == "en"
    assert by_text["Instructions in Telugu"].lang == "en"
    # Devanagari mantras are Sanskrit; Telugu instructions are Telugu; the footnote text is English.
    gayatri = next(b for b in all_blocks if b.lines and "भूर्भुवः" in b.lines[0])
    assert gayatri.lang == "sa" and gayatri.type == "verse" and len(gayatri.lines) == 3 and "॒" in gayatri.lines[0]
    telugu = next(b for b in all_blocks if b.lines and "మంత్రమును" in b.lines[0])
    assert telugu.lang == "te"
    footnote = next(b for b in all_blocks if b.lines and "Savitr" in b.lines[0])
    assert footnote.lang == "en"
    # Ordered list numbers are kept; the fill-in blank (Pandoc turns "........" into an ellipsis) is detected.
    assert any(b.type == "list_item" and b.lines[0].startswith("1.") for b in all_blocks)
    blank = next(b for b in all_blocks if b.type == "fill_blank")
    assert "गोत्रः" in blank.lines[0]
    # The mixed-language table survives with both Sanskrit and English cells intact.
    table = next(b for b in all_blocks if b.type == "table")
    assert ["Sanskrit", "Meaning"] in table.rows and ["सूर्यः", "the sun"] in table.rows


def test_pandoc_epub_renders_end_to_end(pandoc_epub, tmp_path):
    cfg = pipeline.RunConfig(input_path=pandoc_epub, out_dir=tmp_path / "out", script="kannada", jobs=1, formats=("pdf", "epub"))
    runner, outputs = pipeline.run(cfg)
    assert not runner.failed
    text = "".join(pg.get_text() for pg in pymupdf.open(outputs["pdf"]))
    assert "Instructions in Telugu" in text and "ఈ మంత్రమును" in text
    assert transliterate.transliterate_text("केशवाय नमः", "kannada") in text
    assert "Savitr" in text  # the footnote, printed as trailing text
    jar = __import__("os").environ.get("VEDALIPI_EPUBCHECK")
    if jar:
        import subprocess

        res = subprocess.run(["java", "-jar", jar, str(outputs["epub"])], capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, res.stdout + res.stderr


def test_cli_accepts_an_epub_input(publisher_epub, tmp_path, capsys):
    from vedalipi.cli import main

    assert main(["run", str(publisher_epub), "--out", str(tmp_path / "out"), "--script", "telugu", "--format", "epub"]) == 0
    out = capsys.readouterr().out
    assert "epub: " in out and (tmp_path / "out" / "pub.telugu.epub").exists()
    assert json.loads((tmp_path / "out" / "master.json").read_text())["title"] == "Test Book"
