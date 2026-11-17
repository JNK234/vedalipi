# ABOUTME: Tests for the EPUB writer: package structure, embedded fonts, Apple Books font options, chapters and navigation.
# ABOUTME: Checks the EPUB holds every line of the transliterated master, and that translated and side-by-side editions build.

from __future__ import annotations

import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from vedalipi import judge, pipeline, transliterate
from vedalipi.render import FONTS_DIR, font_dirs
from vedalipi.render.document import BLANK_RE, build_document
from vedalipi.render.epub import MAX_CHAPTER_BLOCKS, SCRIPT_TAGS, language_tags, render_epub, write_epub
from vedalipi.schema import Block, Label, Manifest, Master, Page

from test_labels import LABELS, labelled_master, make_out
from test_render import MARK_TEST, fixture_master, make_scan

XHTML = "{http://www.w3.org/1999/xhtml}"
OPF = "{http://www.idpf.org/2007/opf}"
DC = "{http://purl.org/dc/elements/1.1/}"
EPUB = "{http://www.idpf.org/2007/ops}"


def read_epub(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def chapter_text(files: dict[str, bytes]) -> str:
    """Visible text of every spine chapter, in order (XML-parsed, so entities and tags are resolved)."""
    out = []
    for name in sorted(n for n in files if re.fullmatch(r"OEBPS/ch\d+\.xhtml", n)):
        root = ET.fromstring(files[name])
        out.append("".join(root.itertext()))
    return "\n".join(out)


def manifest_hrefs(files: dict[str, bytes]) -> dict[str, ET.Element]:
    opf = ET.fromstring(files["OEBPS/content.opf"])
    return {item.get("href"): item for item in opf.iter(f"{OPF}item")}


@pytest.fixture(scope="module")
def kannada_epub(tmp_path_factory) -> tuple[Path, Master, Master]:
    work = tmp_path_factory.mktemp("epub") / "work"
    make_scan(work / "pages" / "p0008" / "pre.png")
    master = fixture_master()
    target = transliterate.transliterate_master(master, "kannada")
    out = work.parent / "fixture.kannada.epub"
    render_epub(master, target, out, script="kannada", work_dir=work)
    return out, master, target


def test_epub_container_is_well_formed(kannada_epub):
    out, _, _ = kannada_epub
    with zipfile.ZipFile(out) as z:
        infos = z.infolist()
        # The mimetype entry comes first and uncompressed, as the OCF spec requires.
        assert infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        assert z.testzip() is None
    files = read_epub(out)
    container = ET.fromstring(files["META-INF/container.xml"])
    rootfile = next(container.iter("{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"))
    assert rootfile.get("full-path") == "OEBPS/content.opf"
    # Every manifest item exists in the package, and every XHTML and XML file parses.
    for href in manifest_hrefs(files):
        assert f"OEBPS/{href}" in files, href
    for name, data in files.items():
        if name.endswith((".xhtml", ".xml", ".opf")):
            ET.fromstring(data)


def test_package_metadata(kannada_epub):
    out, master, _ = kannada_epub
    files = read_epub(out)
    opf = ET.fromstring(files["OEBPS/content.opf"])
    assert opf.get("version") == "3.0"
    meta = opf.find(f"{OPF}metadata")
    assert meta.find(f"{DC}title").text == master.title
    assert meta.find(f"{DC}language").text == "sa-Knda"
    ident = meta.find(f"{DC}identifier")
    assert ident.get("id") == opf.get("unique-identifier") and ident.text.startswith("urn:uuid:")
    props = {m.get("property"): m.text for m in meta.iter(f"{OPF}meta")}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", props["dcterms:modified"])
    assert props["rendition:layout"] == "reflowable"
    # Apple Books honours embedded fonts only when told so, in both the EPUB 3 and the older iBooks form.
    assert props["ibooks:specified-fonts"] == "true"
    assert "specified-fonts" in files["META-INF/com.apple.ibooks.display-options.xml"].decode()
    # The spine: title page, then the chapters; the nav document is declared.
    spine = [r.get("idref") for r in opf.iter(f"{OPF}itemref")]
    assert spine[:2] == ["titlepage", "nav"] and spine[2].startswith("ch")
    assert any(i.get("properties") == "nav" for i in opf.iter(f"{OPF}item"))


def test_fonts_are_embedded_and_declared(kannada_epub):
    out, _, _ = kannada_epub
    files = read_epub(out)
    fonts = sorted(n for n in files if n.startswith("OEBPS/fonts/") and n.endswith(".ttf"))
    # The svara-emphasised Kannada copy is what the body uses; Noto Serif covers digits and Latin notes;
    # Tamil for the Tamil note block. The Devanagari font is not needed in a Kannada-only book.
    assert any("VedalipiSerifKannada-Regular" in f for f in fonts)
    assert any("NotoSerif-Regular" in f for f in fonts)
    assert any("NotoSansTamil-Regular" in f for f in fonts)
    assert not any("Devanagari" in f for f in fonts)
    assert "OEBPS/fonts/OFL.txt" in files  # the licence travels with the fonts
    css = files["OEBPS/style.css"].decode()
    for f in fonts:
        assert f"url(\"{f.removeprefix('OEBPS/')}\")" in css
    assert '@font-face { font-family: "Vedalipi Serif Kannada"' in css
    hrefs = manifest_hrefs(files)
    assert all(hrefs[f.removeprefix("OEBPS/")].get("media-type") == "font/ttf" for f in fonts)
    # Embedded font bytes are the vendored/derived files themselves.
    derived = Path(font_dirs(FONTS_DIR)[1]) / "VedalipiSerifKannada-Regular.ttf"
    assert files["OEBPS/fonts/VedalipiSerifKannada-Regular.ttf"] == derived.read_bytes()


def test_epub_holds_every_line_of_the_target(kannada_epub):
    out, master, target = kannada_epub
    text = judge.norm(chapter_text(read_epub(out)))
    for page in target.pages:
        for b in page.blocks:
            if b.type in ("running_header", "page_number"):
                continue
            for line in b.lines:
                # Fill-in leaders ("........") are drawn as dotted rules, not text, as in the PDF.
                assert judge.norm(BLANK_RE.sub("", line)) in text, (page.page, b.id, line)
            for row in b.rows or []:
                for cell in row:
                    assert judge.norm(cell) in text, (page.page, b.id, cell)
    # Svara marks and the U+A8F3 stand-in survive into the e-book exactly.
    assert "\u0951" in text and "\u0952" in text and "\u1cda" in text and "ಗ್ಂ" in text
    # Running headers and page numbers are not body text.
    assert judge.norm("तैत्तिरीयोपनिषत्") not in text


def test_master_json_travels_inside_the_package(kannada_epub):
    out, master, _ = kannada_epub
    files = read_epub(out)
    assert Master.model_validate_json(files["OEBPS/master.json"]) == master
    assert manifest_hrefs(files)["master.json"].get("media-type") == "application/json"


def test_printed_lines_keep_their_breaks_and_vedic_pitch(kannada_epub):
    out, _, target = kannada_epub
    files = read_epub(out)
    html = files["OEBPS/ch001.xhtml"].decode()
    v1 = target.page(7).block("v1")
    # One <p class="line"> per printed line, in a block marked vedic (wider line pitch in the CSS).
    m = re.search(r'<div class="block[^"]*vedic[^"]*" id="p0007-v1"[^>]*>(.*?)</div>', html, re.S)
    assert m and m.group(1).count('<p class="line">') == len(v1.lines)
    assert ".vedic { line-height: 1.97; }" in files["OEBPS/style.css"].decode()
    # A plain prose block is not vedic.
    assert re.search(r'<div class="block left f\d+" id="p0007-p1"', html)
    # The body's language tag follows the script's OpenType language (Kannada), foreign blocks their own.
    assert 'lang="sa-Knda"' in html and 'lang="en"' in html and 'lang="ta"' in html


def test_block_types_render_to_their_html(kannada_epub):
    out, _, _ = kannada_epub
    files = read_epub(out)
    html = "".join(files[n].decode() for n in sorted(files) if re.fullmatch(r"OEBPS/ch\d+\.xhtml", n))
    assert "<h1 " in html and "<h2 " in html  # heading levels
    assert html.count('<span class="blank"') == 4  # two fill-in lines with two blanks each
    assert '<span class="marker">೧.</span>' in html and '<span class="marker">•</span>' in html  # printed and default
    assert '<table class="ruled">' in html and "<thead>" in html and html.count("<th>") == 2
    assert '<hr class="sep"' in html
    # Crops are named by body position (3rd body block of page 8), as the PDF build does.
    assert '<img src="img/p0008-003.png"' in html and "OEBPS/img/p0008-003.png" in files
    assert manifest_hrefs(files)["img/p0008-003.png"].get("media-type") == "image/png"
    # Consecutive list items sit tight together.
    assert "tight-below" in html and "tight-above" in html


def test_navigation_has_toc_page_list_and_landmarks(kannada_epub):
    out, _, target = kannada_epub
    files = read_epub(out)
    nav = ET.fromstring(files["OEBPS/nav.xhtml"])
    navs = {n.get(f"{EPUB}type"): n for n in nav.iter(f"{XHTML}nav")}
    assert set(navs) == {"toc", "page-list", "landmarks"}
    toc_links = [(a.get("href"), a.text) for a in navs["toc"].iter(f"{XHTML}a")]
    # The level-1 heading of page 7 opens the book; the level-2 heading of page 8 is nested under it.
    assert toc_links[0] == ("ch001.xhtml#p0007-t", target.page(7).block("t").lines[0])
    assert toc_links[1] == ("ch001.xhtml#p0008-t2", target.page(8).block("t2").lines[0])
    assert navs["toc"].find(f"{XHTML}ol/{XHTML}li/{XHTML}ol") is not None
    pages = [a.text for a in navs["page-list"].iter(f"{XHTML}a")]
    assert pages == ["7", "8"]
    html = files["OEBPS/ch001.xhtml"].decode()
    assert 'epub:type="pagebreak" role="doc-pagebreak" id="srcp0008" aria-label="8"' in html
    assert 'id="srcp0007"' in html
    # The first source page has no "src p." label (as in the PDF); the second has.
    assert "src p. 7" not in html and "src p. 8" in html
    landmarks = {a.get(f"{EPUB}type"): a.get("href") for a in navs["landmarks"].iter(f"{XHTML}a")}
    assert landmarks["bodymatter"] == "ch001.xhtml" and landmarks["toc"] == "nav.xhtml"


def test_chapters_split_at_top_level_headings(tmp_path):
    pages = []
    for i in range(1, 5):
        blocks = [Block(id="v", type="verse", lines=[f"श्लोकः {i}"])]
        if i in (1, 3):
            blocks.insert(0, Block(id="h", type="heading", level=1, lines=[f"अध्यायः {i}"]))
        if i == 2:
            blocks.insert(0, Block(id="h2", type="heading", level=2, lines=["उपशीर्षकम्"]))
        pages.append(Page(page=i, blocks=blocks))
    master = Master(title="Chapters", source="c.pdf", pages=pages)
    target = transliterate.transliterate_master(master, "telugu")
    out = render_epub(master, target, tmp_path / "c.epub", script="telugu", work_dir=tmp_path / "work")
    files = read_epub(out)
    chapters = sorted(n for n in files if re.fullmatch(r"OEBPS/ch\d+\.xhtml", n))
    assert len(chapters) == 2
    # Chapter 2 starts with page 3's marker and heading; page 2's level-2 heading stays in chapter 1.
    ch2 = files["OEBPS/ch002.xhtml"].decode()
    assert 'id="srcp0003"' in ch2 and 'id="p0003-h"' in ch2 and 'id="p0002-h2"' not in ch2
    assert ch2.index('id="srcp0003"') < ch2.index('id="p0003-h"')
    assert ET.fromstring(files["OEBPS/ch002.xhtml"]).find(f"{XHTML}head/{XHTML}title").text == target.page(3).block("h").lines[0]
    nav = ET.fromstring(files["OEBPS/nav.xhtml"])
    toc = next(n for n in nav.iter(f"{XHTML}nav") if n.get(f"{EPUB}type") == "toc")
    assert len(list(toc.iter(f"{XHTML}li"))) == 3  # two chapters, one nested sub-heading
    assert '<dc:language>sa-Telu</dc:language>' in files["OEBPS/content.opf"].decode()


def test_long_chapter_is_cut_at_a_page_boundary(tmp_path):
    per_page = 40
    pages = [
        Page(page=i, blocks=[Block(id=f"v{j}", type="verse", lines=[f"पदम् {i} {j}"]) for j in range(per_page)])
        for i in range(1, 9)
    ]
    master = Master(title="Long", source="l.pdf", pages=pages)
    out = render_epub(master, master, tmp_path / "l.epub", script="devanagari", work_dir=tmp_path / "work")
    files = read_epub(out)
    chapters = sorted(n for n in files if re.fullmatch(r"OEBPS/ch\d+\.xhtml", n))
    assert len(chapters) == 2
    first = files[chapters[0]].decode()
    assert first.count('class="block') == 7 * per_page >= MAX_CHAPTER_BLOCKS
    assert 'id="srcp0008"' in files[chapters[1]].decode()
    # Nothing is lost across the cut.
    text = judge.norm(chapter_text(files))
    assert all(judge.norm(f"पदम् {i} {j}") in text for i in range(1, 9) for j in range(per_page))


def test_book_without_headings_still_has_a_contents_entry(tmp_path):
    master = Master(title="Plain", source="p.pdf", pages=[Page(page=1, blocks=[Block(id="v", type="verse", lines=["नमः"])])])
    out = render_epub(master, master, tmp_path / "p.epub", script="devanagari", work_dir=tmp_path / "work")
    files = read_epub(out)
    nav = ET.fromstring(files["OEBPS/nav.xhtml"])
    toc = next(n for n in nav.iter(f"{XHTML}nav") if n.get(f"{EPUB}type") == "toc")
    assert [(a.get("href"), a.text) for a in toc.iter(f"{XHTML}a")] == [("ch001.xhtml", "Plain")]


def test_image_labels_are_set_in_svg_over_the_crop(tmp_path):
    out_dir = make_out(tmp_path / "out", labelled_master())
    epub = pipeline.render_from_master(out_dir, "kannada", fmt="epub")
    assert epub == out_dir / "labels.kannada.epub"
    files = read_epub(epub)
    html = files["OEBPS/ch001.xhtml"].decode()
    assert "<svg " in html and "<image " in html and html.count("<text ") == len(LABELS)
    assert transliterate.transliterate_text(LABELS[0].text, "kannada") in html
    # The Kannada run inside the English label is set in the Kannada font explicitly (a tspan of its own).
    soma = transliterate.transliterate_text("सोमः", "kannada")
    assert re.search(rf'Sun \(<tspan style="font-family: &quot;Vedalipi Serif Kannada&quot;[^"]*">{soma}</tspan>\)', html)
    hrefs = manifest_hrefs(files)
    assert hrefs["ch001.xhtml"].get("properties") == "svg"
    assert "OEBPS/img/p0001-002.png" in files
    ET.fromstring(files["OEBPS/ch001.xhtml"])  # inline SVG keeps the document well-formed


def test_missing_scan_gives_a_placeholder(tmp_path):
    master = Master(title="x", source="x.pdf", pages=[Page(page=1, blocks=[Block(id="i", type="image", bbox=[0.2, 0.2, 0.8, 0.6])])])
    out = render_epub(master, master, tmp_path / "x.epub", script="devanagari", work_dir=tmp_path / "work")
    html = read_epub(out)["OEBPS/ch001.xhtml"].decode()
    assert "image not available" in html and "<img" not in html


def test_side_by_side_pairs_each_line_with_its_original(tmp_path):
    master = fixture_master()
    target = transliterate.transliterate_master(master, "telugu")
    out = render_epub(master, target, tmp_path / "s.epub", script="telugu", work_dir=tmp_path / "work", side_by_side=True)
    files = read_epub(out)
    html = files["OEBPS/ch001.xhtml"].decode()
    v1, o1 = target.page(7).block("v1"), master.page(7).block("v1")
    block = re.search(r'id="p0007-v1">(.*?)</div></div></div>', html, re.S).group(0)
    assert block.count('<div class="pair">') == len(v1.lines)
    # (Verse-number glue turns some spaces into no-break spaces, so compare without whitespace.)
    assert all(judge.norm(line) in judge.norm(block) for line in [*v1.lines, *o1.lines])
    # A Devanagari font is embedded for the original; the phone layout stacks, the tablet layout sets columns.
    assert any("Devanagari" in n for n in files if n.startswith("OEBPS/fonts/"))
    assert "@media (min-width: 600px)" in files["OEBPS/style.css"].decode()
    # Foreign (English) text is the same on both sides, so it is not paired.
    assert re.search(r'id="p0007-en"[^>]*>(?:(?!pair).)*</div>', html, re.S)


def test_translated_edition_epub(tmp_path):
    import test_translate as tt
    from vedalipi import translate as tl

    cfg = tt.setup_out(tmp_path)
    tl.translate_master(cfg, backend=tt.FakeClaude())
    outputs = pipeline.render_outputs(cfg.out_dir, "iast", ("epub", "pdf"), translation="en")
    assert set(outputs) == {"epub", "pdf"} and outputs["epub"].name == "book.iast.en.epub" and outputs["pdf"].exists()
    files = read_epub(outputs["epub"])
    opf = files["OEBPS/content.opf"].decode()
    assert "<dc:language>en</dc:language><dc:language>sa-Latn</dc:language>" in opf
    html = files["OEBPS/ch001.xhtml"].decode()
    assert '<div class="mantra"' in html and 'class="cue' in html and 'class="mtext' in html
    assert 'class="meaning' in html and 'class="gloss' in html
    text = chapter_text(files)
    assert "EN ॐ भूर्भु॑वः॑" in text and "the sacred syllable" in text  # meaning and gloss from the fake
    assert "EN నీళ్లు తీసుకొని" in text  # translated Telugu prose, reflowed
    assert '<p class="prose">' in files["OEBPS/ch001.xhtml"].decode() + files.get("OEBPS/ch002.xhtml", b"").decode()
    # Roman svaras are in the text; the translated style sets every font at one scale.
    assert "\u030d" in text
    assert "font-size: 0.88em" not in files["OEBPS/style.css"].decode()


def test_language_tags():
    assert language_tags("kannada", None) == ["sa-Knda"]
    assert language_tags("iast", "en") == ["en", "sa-Latn"]
    assert language_tags("harvardkyoto", None) == ["sa-Latn"]
    assert all(re.fullmatch(r"[a-z]{2}(-[A-Z][a-z]{3})?", t) for t in SCRIPT_TAGS.values())


def test_write_epub_is_deterministic(tmp_path):
    master = fixture_master()
    target = transliterate.transliterate_master(master, "kannada")
    build = tmp_path / "build"
    build.mkdir()
    doc = build_document(master, target, script="kannada", side_by_side=False, work_dir=tmp_path / "work", build_dir=build)
    when = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    a = write_epub(master, doc, tmp_path / "a.epub", script="kannada", build_dir=build, font_dirs=font_dirs(FONTS_DIR), modified=when)
    b = write_epub(master, doc, tmp_path / "b.epub", script="kannada", build_dir=build, font_dirs=font_dirs(FONTS_DIR), modified=when)
    assert a.read_bytes() == b.read_bytes()
    assert "2030-01-02T03:04:05Z" in read_epub(a)["OEBPS/content.opf"].decode()


def test_special_characters_survive_as_text(tmp_path):
    master = Master(title="Esc <&>", source="e.pdf", pages=[Page(page=1, blocks=[
        Block(id="h", type="heading", level=1, lines=["<शीर्षकम् & \"उद्धरणम्\">"]),
        Block(id="v", type="verse", lines=["a < b && c > d", "१ <b>bold</b> ॥ २ ॥"]),
        Block(id="t", type="table", rows=[["<x>", "y & z"]]),
    ])])
    out = render_epub(master, master, tmp_path / "e.epub", script="devanagari", work_dir=tmp_path / "work")
    files = read_epub(out)
    text = chapter_text(files)
    assert "<शीर्षकम् & \"उद्धरणम्\">" in text and "a < b && c > d" in text and "<b>bold</b>" in text
    assert "<x>" in text and "y & z" in text
    nav = ET.fromstring(files["OEBPS/nav.xhtml"])
    assert next(nav.iter(f"{XHTML}a")).text == "<शीर्षकम् & \"उद्धरणम्\">"
    assert ET.fromstring(files["OEBPS/content.opf"]).find(f"{OPF}metadata/{DC}title").text == "Esc <&>"


def test_output_paths_and_format_validation(tmp_path):
    master = Master(title="t", source="/x/My Book.pdf")
    assert pipeline.epub_path(tmp_path, master, "kannada") == tmp_path / "My Book.kannada.epub"
    assert pipeline.output_path(tmp_path, master, "iast", "epub", translation="en") == tmp_path / "My Book.iast.en.epub"
    assert pipeline.output_path(tmp_path, master, "telugu", "pdf", side_by_side=True) == tmp_path / "My Book.telugu.side-by-side.pdf"
    with pytest.raises(ValueError, match="unknown output format"):
        pipeline.output_path(tmp_path, master, "kannada", "mobi")


def test_cli_renders_both_formats(tmp_path, capsys):
    from vedalipi.cli import main

    out = tmp_path / "out"
    (out / "work").mkdir(parents=True)
    fixture_master().save(out / "master.json")
    Manifest(input_path="fixture.pdf", input_hash="x", input_kind="pdf", num_pages=2).save(out / "work" / "manifest.json")
    assert main(["render", str(out), "--script", "kannada", "--format", "both"]) == 0
    printed = capsys.readouterr().out
    assert (out / "fixture.kannada.pdf").exists() and (out / "fixture.kannada.epub").exists()
    assert "pdf: " in printed and "epub: " in printed
    assert main(["render", str(out), "--script", "telugu", "--format", "epub"]) == 0
    assert (out / "fixture.telugu.epub").exists() and not (out / "fixture.telugu.pdf").exists()


def test_run_config_formats_flow_through_text_input(tmp_path):
    src = tmp_path / "book.txt"
    src.write_text("ॐ भूर्भुवः स्वः\n", encoding="utf-8")
    cfg = pipeline.RunConfig(input_path=src, out_dir=tmp_path / "out", script="kannada", jobs=1, formats=("epub",))
    runner, outputs = pipeline.run(cfg)
    assert not runner.failed and set(outputs) == {"epub"}
    assert outputs["epub"] == tmp_path / "out" / "book.kannada.epub" and outputs["epub"].exists()
    text = chapter_text(read_epub(outputs["epub"]))
    assert transliterate.transliterate_text("ॐ भूर्भुवः स्वः", "kannada") in text


def _epubcheck_jar() -> Path | None:
    """The epubcheck jar, when VEDALIPI_EPUBCHECK points at it (or at its directory); otherwise None."""
    import os

    p = os.environ.get("VEDALIPI_EPUBCHECK")
    if not p:
        return None
    p = Path(p)
    if p.is_dir():
        p = next(iter(p.glob("epubcheck*.jar")), p)
    return p if p.is_file() else None


@pytest.mark.skipif(_epubcheck_jar() is None, reason="set VEDALIPI_EPUBCHECK to the epubcheck jar to run the official validator")
@pytest.mark.parametrize("edition", ["plain", "side_by_side", "labels", "translated"])
def test_epubcheck_passes(tmp_path, edition):
    import shutil
    import subprocess

    if edition == "labels":
        out_dir = make_out(tmp_path / "out", labelled_master())
        epub = pipeline.render_from_master(out_dir, "kannada", fmt="epub")
    elif edition == "translated":
        import test_translate as tt
        from vedalipi import translate as tl

        cfg = tt.setup_out(tmp_path)
        tl.translate_master(cfg, backend=tt.FakeClaude())
        epub = pipeline.render_from_master(cfg.out_dir, "iast", translation="en", fmt="epub")
    else:
        work = tmp_path / "work"
        make_scan(work / "pages" / "p0008" / "pre.png")
        master = fixture_master()
        target = transliterate.transliterate_master(master, "kannada")
        epub = render_epub(master, target, tmp_path / "f.epub", script="kannada", work_dir=work,
                           side_by_side=edition == "side_by_side")
    assert shutil.which("java"), "epubcheck needs java"
    res = subprocess.run(["java", "-jar", str(_epubcheck_jar()), str(epub)], capture_output=True, text=True, timeout=300)
    assert res.returncode == 0 and "0 errors / 0 warnings" in res.stdout + res.stderr, res.stdout + res.stderr
