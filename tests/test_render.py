# ABOUTME: Tests for the Typst renderer: a realistic Devanagari fixture master rendered to Kannada/Telugu PDFs.
# ABOUTME: Checks compile, extractable text, the master.json attachment, image crops, escaping and HarfBuzz mark coverage.

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw

from vedalipi import transliterate
from vedalipi.render import FONTS_DIR, build_dir_for, font_dirs, render_pdf
from vedalipi.render.document import blank_segments, split_marker
from vedalipi.render.fonts import SCRIPTS, font_chain
from vedalipi.schema import Block, Master, Page

SVARA_LINE = "ओं भ॒द्रं कर्णे॑भिः शृणु॒याम॑ देवाः ।"
# Every Vedic mark on conjuncts, matras, anusvara, visarga and the U+A8F3 stand-in.
MARK_TEST = "ओं भ॒द्रं कर्णे॑भिः शृणु॒याम॑ देवाः । स्थि॒रैरङ्गै᳚स्तुष्टु॒वाꣳस॑स्त॒नूभिः॑ क्ष्मा॒ त्र्य॑म्बकं॒ य॒जा॑महे"


def make_scan(path: Path) -> None:
    """A small fake page scan with a drawn 'figure' in its middle for image-block cropping."""
    im = Image.new("RGB", (600, 900), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((180, 380, 420, 560), outline="black", width=6)
    d.ellipse((230, 410, 370, 530), outline=(150, 30, 30), width=5)
    d.line((180, 380, 420, 560), fill="black", width=3)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def fixture_master() -> Master:
    p1 = Page(
        page=7,
        blocks=[
            Block(id="h", type="running_header", lines=["तैत्तिरीयोपनिषत्"], align="center"),
            Block(id="t", type="heading", level=1, lines=["शान्तिपाठः"], align="center"),
            Block(
                id="v1",
                type="verse",
                lines=[
                    SVARA_LINE,
                    "भ॒द्रं प॑श्येमा॒क्षभि॒र्यज॑त्राः ।",
                    "स्थि॒रैरङ्गै᳚स्तुष्टु॒वाꣳस॑स्त॒नूभिः॑ ।",
                    "व्यशे॑म दे॒वहि॑तं॒ यदायुः॑ ॥ १ ॥",
                ],
            ),
            Block(id="p1", type="prose", lines=["अथ संकल्पः । मम उपात्त समस्त दुरितक्षयद्वारा", "श्री परमेश्वर प्रीत्यर्थं [विशेषेण] *पूजां* करिष्ये ।"]),
            Block(id="fb", type="fill_blank", lines=["........ नक्षत्रे ........ राशौ जातस्य", "........ नाम्नः ........ गोत्रस्य"]),
            Block(id="l1", type="list_item", lines=["१. गणपति पूजा"]),
            Block(id="l2", type="list_item", lines=["कलश पूजा"]),
            Block(id="en", type="foreign", lang="en", lines=["Note: recite with svaras #1 $5 <b> `x` @y \\z [a] _u_ *s*"]),
            Block(id="pn", type="page_number", lines=["७"], align="center"),
        ],
    )
    p2 = Page(
        page=8,
        blocks=[
            Block(id="h", type="running_header", lines=["शान्तिपाठः"], align="center"),
            Block(id="t2", type="heading", level=2, lines=["प्रार्थना — प्रतिवचनम्"]),
            Block(
                id="tb",
                type="table",
                rows=[
                    ["प्रार्थना", "प्रतिवचनम्"],
                    ["स्व॒स्ति न॒ इन्द्रो॑ वृ॒द्धश्र॑वाः ।", "स्व॒स्ति नः॑ पू॒षा वि॒श्ववे॑दाः ।"],
                    ["ओं शान्तिः॒ शान्तिः॒ शान्तिः॑ ॥", "तथास्तु"],
                ],
            ),
            Block(id="img", type="image", bbox=[0.28, 0.4, 0.72, 0.64]),
            Block(id="ta", type="foreign", lang="ta", lines=["தமிழ் குறிப்பு: ஓம் சாந்தி"]),
            Block(id="sep", type="separator"),
            Block(id="v2", type="verse", align="center", lines=["त्र्य॑म्बकं यजामहे सु॒गन्धिं॑ पुष्टि॒वर्ध॑नम् ।", "ꣳ इति"]),
            Block(id="pn", type="page_number", lines=["८"], align="center"),
        ],
    )
    return Master(title="Taittiriya fixture", source="fixture.pdf", pages=[p1, p2])


def render(tmp_path: Path, script: str = "kannada", side_by_side: bool = False) -> Path:
    master = fixture_master()
    work = tmp_path / "work"
    make_scan(work / "pages" / "p0008" / "pre.png")
    target = transliterate.transliterate_master(master, script)
    out = tmp_path / f"book.{script}{'.sbs' if side_by_side else ''}.pdf"
    render_pdf(master, target, out, script=script, work_dir=work, side_by_side=side_by_side)
    return out


@pytest.fixture(scope="module")
def kannada_pdf(tmp_path_factory) -> Path:
    return render(tmp_path_factory.mktemp("kn"))


def pdf_text(path: Path) -> str:
    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


def test_compiles_and_contains_kannada(kannada_pdf):
    assert kannada_pdf.stat().st_size > 5000
    text = pdf_text(kannada_pdf)
    for word in ["ಶಾನ್ತಿಪಾಠಃ", "ನಕ್ಷತ್ರೇ", "ತೈತ್ತಿರೀಯೋಪನಿಷತ್", "ಪ್ರತಿವಚನಮ್", "ಗಣಪತಿ"]:
        assert word in text, word
    # Foreign text is not transliterated and Typst specials survive literally.
    assert "#1 $5 <b> `x` @y \\z [a] _u_ *s*" in text
    assert "தமிழ் குறிப்பு" in text
    assert "[ವಿಶೇಷೇಣ] *ಪೂಜಾಂ*" in text


def test_svaras_are_in_text_layer(kannada_pdf):
    text = pdf_text(kannada_pdf)
    assert "॑" in text and "॒" in text and "᳚" in text
    assert "◌" not in text


def test_master_attached(kannada_pdf):
    with pymupdf.open(kannada_pdf) as doc:
        assert doc.embfile_count() == 1
        info = doc.embfile_info(0)
        assert info["filename"] == "master.json"
        data = json.loads(doc.embfile_get(0))
    assert Master.model_validate(data) == fixture_master()


def test_only_vendored_fonts_used(kannada_pdf):
    with pymupdf.open(kannada_pdf) as doc:
        fonts = {f[3].split("+")[-1] for page in doc for f in page.get_fonts()}
    assert fonts, "no fonts found"
    # Vendored Noto fonts, or the svara-emphasised Vedalipi copies derived from them.
    assert all(f.startswith(("Noto", "Vedalipi")) for f in fonts), fonts
    assert any(f.startswith("VedalipiSerifKannada") for f in fonts), fonts


def test_image_crop_embedded(kannada_pdf):
    with pymupdf.open(kannada_pdf) as doc:
        assert sum(len(p.get_images()) for p in doc) == 1


def test_footer_shows_source_page(kannada_pdf):
    text = pdf_text(kannada_pdf)
    assert "src p. 7" in text or "src pp. 7" in text
    assert "೭" in text


def test_side_by_side_telugu(tmp_path):
    out = render(tmp_path, "telugu", side_by_side=True)
    text = pdf_text(out)
    assert "భ" in text  # Telugu
    assert "भ॒द्रं" in text or "भ" in text  # Devanagari original alongside
    assert (build_dir_for(tmp_path / "work", "telugu", True) / "data.json").exists()


def test_missing_scan_renders_placeholder(tmp_path):
    master = fixture_master()
    target = transliterate.transliterate_master(master, "kannada")
    out = tmp_path / "noimg.pdf"
    render_pdf(master, target, out, script="kannada", work_dir=tmp_path / "work")
    assert "image not available" in pdf_text(out)


def test_blank_segments_and_markers():
    segs = blank_segments("........ नक्षत्रे ........ राशौ")
    assert [("blank" in s) for s in segs] == [True, False, True, False]
    assert segs[1]["t"] == " नक्षत्रे "
    assert split_marker(["१. गणपति"]) == ("१.", ["गणपति"])
    assert split_marker(["कलश"]) == ("•", ["कलश"])


@pytest.mark.parametrize("script", ["kannada", "telugu", "devanagari"])
def test_harfbuzz_marks_attach(script):
    """No .notdef and no dotted circle when shaping all three Vedic marks in the script's primary font."""
    hb = pytest.importorskip("uharfbuzz")
    text = MARK_TEST if script == "devanagari" else transliterate.transliterate_text(MARK_TEST, script)
    family = SCRIPTS[script].family
    path = FONTS_DIR / (family.replace(" ", "") + "-Regular.ttf")
    font = hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf, {})
    dotted = font.get_nominal_glyph(0x25CC)
    glyphs = [g.codepoint for g in buf.glyph_infos]
    assert 0 not in glyphs
    assert dotted is None or dotted not in glyphs
    for ch in "॒॑᳚":
        assert font.get_nominal_glyph(ord(ch)) is not None, (script, hex(ord(ch)))


@pytest.mark.parametrize("script", ["kannada", "telugu", "devanagari"])
def test_derived_svara_font_shapes_like_the_original(script):
    """The Vedalipi copy re-cuts only svara outlines: same glyphs and positions as Noto, no dotted circles,
    and heavier marks (thicker anudatta bar, wider udatta tick)."""
    hb = pytest.importorskip("uharfbuzz")
    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.ttLib import TTFont

    text = MARK_TEST if script == "devanagari" else transliterate.transliterate_text(MARK_TEST, script)
    stem = SCRIPTS[script].family.replace(" ", "") + "-Regular.ttf"
    orig_path = FONTS_DIR / stem
    derived_path = Path(font_dirs()[1]) / stem.replace("Noto", "Vedalipi", 1)

    def shape(path):
        font = hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(font, buf, {})
        return font, [(g.codepoint, g.cluster) for g in buf.glyph_infos], [(p.x_offset, p.y_offset, p.x_advance) for p in buf.glyph_positions]

    font, glyphs, pos = shape(derived_path)
    assert (glyphs, pos) == shape(orig_path)[1:]
    dotted = font.get_nominal_glyph(0x25CC)
    assert 0 not in [g for g, _ in glyphs] and (dotted is None or dotted not in [g for g, _ in glyphs])

    def bounds(path, cp):
        f = TTFont(path)
        gs = f.getGlyphSet()
        pen = BoundsPen(gs)
        gs[f.getBestCmap()[cp]].draw(pen)
        return pen.bounds

    ox0, oy0, ox1, oy1 = bounds(orig_path, 0x0952)
    dx0, dy0, dx1, dy1 = bounds(derived_path, 0x0952)
    assert dy1 - dy0 > oy1 - oy0 and dy1 < oy1  # thicker and lower
    ux0, _, ux1, _ = bounds(orig_path, 0x0951)
    vx0, _, vx1, _ = bounds(derived_path, 0x0951)
    assert vx1 - vx0 > ux1 - ux0  # wider tick
    assert TTFont(derived_path)["name"].getDebugName(1).startswith("Vedalipi")


def test_vedic_blocks_get_wider_line_pitch():
    from vedalipi.render.document import build_document

    master = fixture_master()
    doc = build_document(master, master, script="kannada", side_by_side=False, work_dir=Path("/nonexistent"), build_dir=Path("/tmp"))
    by_id = {b["id"]: b for p in doc["pages"] for b in p["blocks"]}
    assert by_id["v1"]["vedic"] and by_id["tb"]["vedic"]
    assert not by_id["p1"]["vedic"] and not by_id["l1"]["vedic"]
    assert doc["main"]["font"][:2] == ["Vedalipi Serif Kannada", "Noto Serif Kannada"]


def test_font_chain_starts_with_primary():
    assert font_chain("Noto Serif Kannada")[0] == "Noto Serif Kannada"
    assert len(set(font_chain("Noto Serif Kannada"))) == len(font_chain("Noto Serif Kannada"))


def test_split_clusters_extract_once(kannada_pdf):
    """Typst repeats a cluster once per positioned run; the text-layer pass must leave one copy."""
    text = pdf_text(kannada_pdf)
    assert "ಭ॒ದ್ರಂ" in text
    assert "ಭ॒ಭ॒" not in text
    assert "ಷ್ಯೇಷ್ಯೇ" not in text


def test_pdftotext_reads_verse_cleanly(kannada_pdf):
    import shutil
    import subprocess

    exe = shutil.which("pdftotext")
    if exe is None:
        pytest.skip("poppler pdftotext not installed")
    text = subprocess.run([exe, str(kannada_pdf), "-"], capture_output=True, text=True, check=True).stdout
    expected = transliterate.transliterate_text("स्थि॒रैरङ्गै᳚स्तुष्टु॒वाꣳस॑स्त॒नूभिः॑", "kannada")
    assert expected in text
    assert "ಭ॒ದ್ರಂ ಪ॑ಶ್ಯೇಮಾ॒ಕ್ಷಭಿ॒ರ್ಯಜ॑ತ್ರಾಃ" in text


def test_textlayer_leaves_drawing_identical(tmp_path):
    from vedalipi.render.textlayer import fix_split_clusters

    out = render(tmp_path)
    raw = tmp_path / "raw.pdf"
    # Re-render without the repair by compiling the kept build directory directly.
    import typst

    build = build_dir_for(tmp_path / "work", "kannada", False)
    fonts = typst.Fonts(include_system_fonts=False, include_embedded_fonts=False, font_paths=font_dirs())
    typst.Compiler(str(build / "main.typ"), root=str(build), font_paths=fonts).compile(output=str(raw))
    fixed = tmp_path / "fixed.pdf"
    fixed.write_bytes(raw.read_bytes())
    assert fix_split_clusters(fixed) > 0
    with pymupdf.open(raw) as a, pymupdf.open(fixed) as b, pymupdf.open(out) as c:
        for pa, pb, pc in zip(a, b, c):
            assert pa.get_pixmap(dpi=72).samples == pb.get_pixmap(dpi=72).samples == pc.get_pixmap(dpi=72).samples


def test_textlayer_keeps_identical_clusters_on_separate_lines(tmp_path):
    """A cluster ending one line and the same cluster starting the next are two clusters, not a split piece."""
    page = Page(page=1, blocks=[
        Block(id="v", type="verse", lines=["अ॒ग्नि क्ष", "क्ष अ॒ग्नि", "क्ष॒ क्ष॒ क्ष॒"]),
    ])
    master = Master(title="t", source="t.pdf", pages=[page])
    target = transliterate.transliterate_master(master, "kannada")
    out = tmp_path / "rep.pdf"
    render_pdf(master, target, out, script="kannada", work_dir=tmp_path / "work")
    text = pdf_text(out)
    assert text.count("ಕ್ಷ") == 5, text
    assert "ಅ॒ಗ್ನಿ" in text and "ಅ॒ಅ॒" not in text


def test_prose_keeps_printed_paragraphs_and_joins_only_wrapped_lines():
    from vedalipi.render.document import prose_paragraphs

    lines = [
        "आचम्य पवित्रपाणिः शुद्धभूमौ कदळ्यादिदले प्रस्थद्वयमितं तण्डुलं निक्षिप्य,",
        "कुम्भं निधाय प्रणवव्याहृतिभिरद्भिः पूरयित्वा कूर्चं आम्रपल्लवादिभिरलंकृत्य ।",
        "ओं विष्वक्सेनाय नमः धूपं समर्पयामि",
        "[पुण्याहे चतुरो ब्राह्मणान् तोषयिष्ये]",
        "दक्षिणकरतलेन कुम्भं समालभ्य",
    ]
    assert prose_paragraphs(lines) == [lines[:2], [lines[2]], [lines[3]], [lines[4]]]


def test_contents_split_by_ocr_is_rejoined_into_one_table():
    from vedalipi.render.document import merge_contents

    blocks = [
        Block(id="l1", type="list_item", lines=["पुरुषसूक्तम्"]),
        Block(id="l2", type="list_item", lines=["नारायणसूक्तम्"]),
        Block(id="t1", type="table", lang="en", rows=[["i)", "Sri Ahobila Math", "ix"]]),
        Block(id="t2", type="table", rows=[["6.", "ग्रह प्रीतिः", "10-11"]]),
        Block(id="l3", type="list_item", lines=["शान्तिपञ्चकम्"]),
        Block(id="p", type="prose", lines=["अथ"]),
        Block(id="l4", type="list_item", lines=["अन्यत्"]),
    ]
    out = merge_contents(blocks)
    assert [b.id for b in out] == ["t1", "p", "l4"]
    assert out[0].rows == [["", "• पुरुषसूक्तम्", ""], ["", "• नारायणसूक्तम्", ""], ["i)", "Sri Ahobila Math", "ix"],
                           ["6.", "ग्रह प्रीतिः", "10-11"], ["", "• शान्तिपञ्चकम्", ""]]
    assert out[0].lang == "sa"
    assert blocks[2].rows == [["i)", "Sri Ahobila Math", "ix"]]  # inputs untouched


def test_table_layout_header_only_for_boxed_tables():
    from vedalipi.render.document import table_layout

    assert table_layout([["प्रार्थना", "प्रतिवचनम्"], ["अ", "आ"]])["header"] is True
    assert table_layout([["1.", "स्वस्ति वाचनम्", "1-2"], ["2.", "अनुज्ञा", "3-5"]])["header"] is False


def test_image_crops_never_share_a_filename(tmp_path):
    work = tmp_path / "work"
    make_scan(work / "pages" / "p0001" / "pre.png")
    page = Page(page=1, blocks=[
        Block(id="a/b", type="image", bbox=[0.1, 0.1, 0.4, 0.4]),
        Block(id="a?b", type="image", bbox=[0.5, 0.5, 0.9, 0.9]),
    ])
    master = Master(title="t", source="t.pdf", pages=[page])
    from vedalipi.render.document import build_document

    doc = build_document(master, master, script="kannada", side_by_side=False, work_dir=work, build_dir=tmp_path / "b")
    paths = [b["image"] for b in doc["pages"][0]["blocks"]]
    assert len(set(paths)) == 2 and all((tmp_path / "b" / p).exists() for p in paths)
