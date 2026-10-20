# ABOUTME: Tests for `vedalipi judge`: renders a tiny master, then checks the PDF holds every line, cell and image.
# ABOUTME: A clean render passes; a line, copy, image, marker or page the PDF lacks is reported, a moved line is out of order.

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from vedalipi import cli, judge, pipeline
from vedalipi.schema import Block, Manifest, Master, Page


def tiny_master() -> Master:
    p1 = Page(page=1, blocks=[
        Block(id="h", type="running_header", lines=["श्री सुदर्शन यज्ञः"], bbox=[0.1, 0.05, 0.8, 0.08]),
        Block(id="logo", type="image", bbox=[0.82, 0.04, 0.92, 0.1]),
        Block(id="t", type="heading", level=1, lines=["शान्तिपाठः"], align="center"),
        Block(id="v", type="verse", lines=["ओं भ॒द्रं कर्णे॑भिः शृणु॒याम॑ देवाः ।", "स्थि॒रैरङ्गै᳚स्तुष्टु॒वाꣳस॑स्त॒नूभिः॑ ॥ १ ॥"]),
        Block(id="fb", type="fill_blank", lines=["........ नक्षत्रे ........ राशौ जातस्य"]),
        Block(id="l1", type="list_item", lines=["१. गणपति पूजा"]),
        Block(id="l2", type="list_item", lines=["कलश पूजा"]),
        Block(id="en", type="foreign", lang="en", lines=["Note: recite slowly (आचार्य वन्दनम्)"]),
        Block(id="pn", type="page_number", lines=["१"], align="center"),
    ])
    p2 = Page(page=2, blocks=[
        Block(id="h", type="running_header", lines=["श्री सुदर्शन यज्ञः"], bbox=[0.1, 0.05, 0.8, 0.08]),
        Block(id="tb", type="table", rows=[["प्रार्थना", "प्रतिवचनम्"], ["स्व॒स्ति न॒ इन्द्रो॑ वृ॒द्धश्र॑वाः ।", "तथास्तु"]]),
        Block(id="img", type="image", bbox=[0.28, 0.4, 0.72, 0.64]),
        Block(id="edge", type="image", bbox=[0.0, 0.1, 0.02, 0.9]),
        Block(id="p", type="prose", lines=["अथ संकल्पः । मम उपात्त समस्त दुरितक्षयद्वारा"]),
        Block(id="pn", type="page_number", lines=["२"], align="center"),
    ])
    return Master(title="tiny", source="tiny.pdf", pages=[p1, p2])


def make_scan(path: Path) -> None:
    im = Image.new("RGB", (600, 900), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((180, 380, 420, 560), outline="black", width=6)
    d.ellipse((500, 40, 540, 85), outline=(150, 30, 30), width=4)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def make_out(out: Path, master: Master, *, scans: bool = True, num_pages: int = 2) -> Path:
    (out / "work").mkdir(parents=True)
    master.save(out / "master.json")
    Manifest(input_path="tiny.pdf", input_hash="x", input_kind="pdf", num_pages=num_pages).save(out / "work" / "manifest.json")
    if scans:
        for p in master.pages:
            make_scan(out / "work" / "pages" / f"p{p.page:04d}" / "pre.png")
    return out


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> Path:
    out = make_out(tmp_path_factory.mktemp("judge") / "out", tiny_master())
    pdf = pipeline.render_from_master(out, "kannada")
    assert pdf == pipeline.pdf_path(out, tiny_master(), "kannada")
    return out


def copy_with(rendered: Path, tmp_path: Path, master: Master) -> Path:
    """The rendered output directory with master.json replaced; the PDF is not re-rendered."""
    out = tmp_path / "out"
    shutil.copytree(rendered, out)
    master.save(out / "master.json")
    return out


def test_clean_render_passes(rendered):
    report = judge.judge(rendered, "kannada")
    assert report["passed"], report
    assert report["missing"] == report["out_of_order"] == report["missing_markers"] == []
    assert report["empty_pages"] == []
    # header x2, heading, verse x2, fill_blank, list items x2, foreign, prose; 4 table cells; logo and image
    assert (report["lines_checked"], report["cells_checked"], report["images_checked"]) == (10, 4, 2)
    replaced = {(r["page"], r["block"], r["kind"]) for r in report["replaced_by_template"]}
    assert replaced == {(1, "pn", "page_number"), (2, "pn", "page_number"), (1, "fb", "fill_blank"), (2, "edge", "image")}
    saved = json.loads((rendered / "work" / "judge-kannada.json").read_text(encoding="utf-8"))
    assert saved["missing"] == [] and saved["script"] == "kannada"
    assert cli.main(["judge", str(rendered), "--script", "kannada"]) == 0


def test_line_absent_from_pdf_is_missing(rendered, tmp_path):
    master = tiny_master()
    master.page(1).block("v").lines.append("व्यशे॑म दे॒वहि॑तं॒ यदायुः॑ ॥ २ ॥")
    master.page(2).block("tb").rows.append(["शान्तिः", "पुनः"])
    out = copy_with(rendered, tmp_path, master)
    report = judge.judge(out, "kannada")
    assert not report["passed"]
    got = {(m["page"], m["block"], m["kind"]) for m in report["missing"]}
    assert got == {(1, "v", "line"), (2, "tb", "cell")}, report["missing"]
    assert [m["text"] for m in report["missing"] if m["kind"] == "line"] == ["ವ್ಯಶೇ॑ಮ ದೇ॒ವಹಿ॑ತಂ॒ ಯದಾಯುಃ॑ ॥ ೨ ॥"]
    assert cli.main(["judge", str(out), "--script", "kannada"]) == 1


def test_missing_image_is_reported(tmp_path):
    out = make_out(tmp_path / "out", tiny_master(), scans=False)  # no scans: the crop is a placeholder
    pipeline.render_from_master(out, "kannada")
    report = judge.judge(out, "kannada")
    got = {(m["page"], m["block"], m["kind"]) for m in report["missing"]}
    assert got == {(1, "logo", "logo"), (2, "img", "image")}, report["missing"]
    assert not report["passed"]


def test_image_added_after_render_is_missing(rendered, tmp_path):
    master = tiny_master()
    master.page(1).blocks.insert(3, Block(id="fig", type="image", bbox=[0.3, 0.3, 0.7, 0.5]))
    report = judge.judge(copy_with(rendered, tmp_path, master), "kannada")
    assert [(m["page"], m["block"], m["kind"]) for m in report["missing"]] == [(1, "fig", "image")]


def test_line_on_another_page_is_out_of_order(rendered, tmp_path):
    master = tiny_master()
    # The PDF has this prose line on source page 2; master now says it belongs on page 1.
    master.page(1).blocks.insert(3, Block(id="moved", type="prose", lines=["अथ संकल्पः । मम उपात्त समस्त दुरितक्षयद्वारा"]))
    master.page(2).blocks = [b for b in master.page(2).blocks if b.id != "p"]
    report = judge.judge(copy_with(rendered, tmp_path, master), "kannada")
    assert [(m["page"], m["block"]) for m in report["out_of_order"]] == [(1, "moved")]
    assert report["missing"] == [] and report["passed"]


def test_repeated_line_dropped_from_pdf_is_missing(rendered, tmp_path):
    master = tiny_master()
    # Master repeats the verse's first line and adds a heading that is only a piece of the prose; the PDF has
    # one copy of each text, already backing other lines, so neither may count as found or out of order.
    master.page(1).blocks.insert(5, Block(id="rep", type="verse", lines=["ओं भ॒द्रं कर्णे॑भिः शृणु॒याम॑ देवाः ।"]))
    master.page(2).blocks.append(Block(id="frag", type="heading", lines=["संकल्पः"]))
    report = judge.judge(copy_with(rendered, tmp_path, master), "kannada")
    assert {(m["page"], m["block"]) for m in report["missing"]} == {(1, "rep"), (2, "frag")}, report["missing"]
    assert report["out_of_order"] == [] and not report["passed"]


def test_cluster_split_over_two_text_objects_is_read_once(tmp_path):
    # Typst draws this line's "ई" as two text objects that both carry "ई"; it must read as one akshara.
    lines = ["श्री मधुभयवे महाविद्वान्, तर्क्करत्नम्, न्याय वेदान्त केसरि",
             "देशिक दर्शन रत्नाकर, मधुरान्दकम् तिरुमलै ईच्चंपाडि"]
    master = Master(title="t", source="t.pdf", pages=[Page(page=1, blocks=[Block(id="p", type="prose", lines=lines)])])
    out = make_out(tmp_path / "out", master, num_pages=1)
    pipeline.render_from_master(out, "devanagari")
    report = judge.judge(out, "devanagari")
    assert report["missing"] == [] and report["passed"], report["missing"]


def test_page_inside_a_footer_range_needs_its_own_divider(tmp_path):
    pages = [Page(page=n, blocks=[Block(id="v", type="verse", lines=[f"ओं शान्तिः {n}"])]) for n in (1, 3)]
    out = make_out(tmp_path / "out", Master(title="t", source="t.pdf", pages=pages), num_pages=3)
    pipeline.render_from_master(out, "kannada")  # one output page, footer "src pp. 1–3", no "src p. 2"
    blank = Page(page=2, blocks=[Block(id="pn", type="page_number", lines=["२"])])
    Master(title="t", source="t.pdf", pages=[pages[0], blank, pages[1]]).save(out / "master.json")
    report = judge.judge(out, "kannada")
    assert report["missing_markers"] == [2] and report["empty_pages"] == [2]
    assert not report["passed"]


def test_missing_manifest_is_an_error(rendered, tmp_path):
    out = copy_with(rendered, tmp_path, tiny_master())
    (out / "work" / "manifest.json").unlink()
    with pytest.raises(RuntimeError, match="manifest"):
        judge.judge(out, "kannada")
    assert cli.main(["judge", str(out), "--script", "kannada"]) == 2


def test_page_left_out_of_master_is_missing(rendered, tmp_path):
    out = copy_with(rendered, tmp_path, tiny_master())
    Manifest(input_path="tiny.pdf", input_hash="x", input_kind="pdf", num_pages=3).save(out / "work" / "manifest.json")
    report = judge.judge(out, "kannada")
    assert [(m["page"], m["kind"]) for m in report["missing"]] == [(3, "page")]
    assert report["missing_markers"] == [3]


def test_empty_page_is_a_warning(rendered, tmp_path):
    master = tiny_master()
    master.pages.append(Page(page=3, blocks=[Block(id="pn", type="page_number", lines=["३"])]))
    out = copy_with(rendered, tmp_path, master)
    report = judge.judge(out, "kannada")
    assert report["empty_pages"] == [3]
    assert report["missing_markers"] == [3]  # never rendered, so no "src p. 3" either


def test_norm_drops_spacing_and_joiners():
    assert judge.norm("ಕ‍ಷ ­ ಾ\n x") == judge.norm("ಕಷಾx")
