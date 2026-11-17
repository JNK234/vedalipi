# ABOUTME: End-to-end run of a book given as text in another script: a Kannada .txt (read 1 of the three fixture
# ABOUTME: pages) through run, judge and report into a Telugu PDF, plus .json input in another script. No OCR.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vedalipi import pipeline, transliterate
from vedalipi.cli import main
from vedalipi.pipeline import RunConfig
from vedalipi.render import SCRIPTS, font_dirs
from vedalipi.schema import Block, Master, Page

FIXTURES = Path(__file__).parent / "fixtures" / "kannada_ocr"
BOOK = FIXTURES / "book.kn.txt"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
SOURCES = ["p0005.read1.json", "p0010.read1.json", "p0064.read1.json"]  # book.kn.txt pages 1-3, in order
A8F3 = "\ua8f3"


@pytest.fixture(scope="module")
def telugu_book(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("t6")
    assert main(["run", str(BOOK), "--source-script", "kannada", "--script", "telugu", "--out", str(out)]) == 0
    return out


def test_fixture_book_is_read1_of_each_page():
    pages = BOOK.read_text(encoding="utf-8").rstrip("\n").split("\f")
    assert [p.splitlines() for p in pages] == [
        [line for b in Page.load(FIXTURES / name).blocks for line in b.lines] for name in SOURCES
    ]


def test_master_is_devanagari_with_the_expected_a8f3(telugu_book):
    master = Master.load(telugu_book / "master.json")
    assert [p.page for p in master.pages] == [1, 2, 3]
    for page, name in zip(master.pages, SOURCES):
        text = "".join(line for b in page.blocks for line in b.lines)
        assert text.count(A8F3) == EXPECTED["files"][name]["expected_A8F3_after_normalize"]
        assert not any(0x0C80 <= ord(c) <= 0x0CFF for c in text)  # no Kannada left in the master


def test_judge_passes_on_the_telugu_pdf(telugu_book, capsys):
    assert (telugu_book / "book.kn.telugu.pdf").exists()
    capsys.readouterr()
    assert main(["judge", str(telugu_book), "--script", "telugu"]) == 0
    out = capsys.readouterr().out
    n_lines = sum(len(p.splitlines()) for p in BOOK.read_text(encoding="utf-8").rstrip("\n").split("\f"))
    assert f"checked 3 pages, {n_lines} lines" in out and "missing: 0" in out and "PASS" in out


def test_report_shows_the_flags_and_no_roundtrip_failures(telugu_book, capsys):
    capsys.readouterr()
    assert main(["report", str(telugu_book)]) == 0
    out = capsys.readouterr().out
    assert "ingest: done=3" in out and "validate: done=3" in out
    assert "non_vedic_mark: 1 (p3:b3-1)" in out
    assert "total: non_vedic_mark U+0331 COMBINING MACRON BELOW x26" in out
    assert "roundtrip_fail (telugu): 0" in out


def test_pdf_text_layer_keeps_every_letter_and_mark(telugu_book):
    # The PDF text of the Telugu book holds each U+0331 once and no lost letter (a NUL), as the master does.
    import pymupdf

    with pymupdf.open(telugu_book / "book.kn.telugu.pdf") as doc:
        text = "".join(p.get_text() for p in doc)
    assert text.count("\u0331") == 26 and "\x00" not in text


def test_every_line_shapes_without_a_dotted_circle(telugu_book):
    # What Typst shapes: each transliterated line in the derived Telugu font. A mark with no base
    # (an anusvara after a foreign mark) would get a dotted circle.
    hb = pytest.importorskip("uharfbuzz")
    master = Master.load(telugu_book / "master.json")
    target = transliterate.transliterate_master(master, "telugu")
    path = Path(font_dirs()[1]) / (SCRIPTS["telugu"].family.replace("Noto", "Vedalipi", 1).replace(" ", "") + "-Regular.ttf")
    font = hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))
    dotted = font.get_nominal_glyph(0x25CC)
    for page in target.pages:
        for line in (ln for b in page.blocks for ln in b.lines):
            buf = hb.Buffer()
            buf.add_str(line)
            buf.guess_segment_properties()
            hb.shape(font, buf, {})
            glyphs = [g.codepoint for g in buf.glyph_infos]
            assert 0 not in glyphs and dotted not in glyphs, line


# ---------------------------------------------------------------- .json input in another script


def test_json_input_in_telugu_becomes_the_devanagari_master(tmp_path):
    src = tmp_path / "book.json"
    src.write_text(Page(page=1, blocks=[
        Block(id="b1", type="prose", lang="te", lines=["ఈ మంత్రమును చెప్పవలెను."]),
        Block(id="b2", type="verse", lang="sa", lines=["అ॒గ్నిమీ॑ళే పు॒రోహి॑తం। అహి॑గ్ం"]),
    ]).model_dump_json(), encoding="utf-8")
    cfg = RunConfig(input_path=src, out_dir=tmp_path / "out", source_script="telugu", jobs=1)
    runner, outputs = pipeline.run(cfg)
    pdf = outputs["pdf"] if outputs else None
    assert not runner.failed and pdf is not None and pdf.exists()
    page = Master.load(cfg.master_path).page(1)
    assert page.block("b1").lines == ["ఈ మంత్రమును చెప్పవలెను."]  # Telugu prose keeps its script
    assert page.block("b2").lines == ["अ॒ग्निमी॑ळे पु॒रोहि॑तं। अहि॑ꣳ"]


def test_verify_passes_pages_1_2_and_checks_page_3(telugu_book, capsys):
    capsys.readouterr()
    assert main(["verify", str(telugu_book)]) == 1  # script: the one rendered (telugu)
    out = capsys.readouterr().out
    assert "(telugu; book printed in kannada)" in out
    assert "p1    PASS   ॑ 22->22  ॒ 20->20  ᳚ 1->1  ꣳ 1->1  [source text]" in out
    assert "p2    PASS   ॑ 45->45  ॒ 78->78  ᳚ 2->2  ꣳ 5->5  [source text]" in out
    assert "p3    CHECK  ॑ 52->52  ॒ 42->42  ᳚ 6->6  ꣳ 7->7  [source text]" in out
    assert "    non_vedic_mark: b3-1" in out and "3 pages: 2 PASS, 1 CHECK" in out
    # Source counts are those of the Kannada reads (expected.json), as printed.
    for n, name in enumerate(SOURCES):
        want = EXPECTED["files"][name]
        line = out.splitlines()[2 + n]
        assert (f"॑ {want['u0951']}->{want['u0951']}  ॒ {want['u0952']}->{want['u0952']}  "
                f"᳚ {want['u1cda']}->{want['u1cda']}  ꣳ {want['expected_A8F3_after_normalize']}->") in line


# ---------------------------------------------------------------- a second reading of a text book


def test_second_read_of_a_text_book_is_compared_like_a_cross_read(tmp_path):
    out = tmp_path / "out"
    assert main(["run", str(BOOK), "--second-read", str(FIXTURES / "book2.kn.txt"), "--source-script", "kannada",
                 "--script", "telugu", "--out", str(out)]) == 0
    master = Master.load(out / "master.json")
    flags = {p.page: sorted({f for b in p.blocks for f in b.flags}) for p in master.pages}
    assert flags == {1: ["reader_disagreement"], 2: ["reader_disagreement", "single_read"],
                     3: ["non_vedic_mark", "reader_disagreement"]}


def test_second_read_is_only_for_text_input(tmp_path):
    with pytest.raises(ValueError, match="--cross-read"):
        pipeline.run(RunConfig(input_path=tmp_path / "book.pdf", out_dir=tmp_path / "out",
                               second_read=FIXTURES / "book2.kn.txt"))
