# ABOUTME: Tests for ingest: input classification, page counts, native-vs-rendered PDF extraction, text/json loading.
# ABOUTME: Builds a tiny single-image-per-page PDF on the fly so no real book is needed.

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from vedalipi import ingest
from vedalipi.schema import Master, Page


def make_page_image(path: Path, w=40, h=30, color=(200, 200, 200)) -> None:
    Image.new("RGB", (w, h), color).save(path)


def make_scan_pdf(path: Path, pages: list[Path]) -> None:
    import pymupdf

    doc = pymupdf.open()
    for p in pages:
        page = doc.new_page(width=400, height=300)
        page.insert_image(page.rect, filename=str(p))
    doc.save(path)


def test_input_kind(tmp_path):
    assert ingest.input_kind(tmp_path / "b.pdf") == "pdf"
    assert ingest.input_kind(tmp_path / "b.txt") == "text"
    assert ingest.input_kind(tmp_path / "b.json") == "json"
    assert ingest.input_kind(tmp_path) == "images"
    with pytest.raises(ValueError):
        ingest.input_kind(tmp_path / "b.docx")


def test_image_folder_natural_order_and_extract(tmp_path):
    for name in ("page10.png", "page2.png", "page1.png"):
        make_page_image(tmp_path / name, color=(150, 150, 150))
    assert ingest.page_count(tmp_path) == 3
    out = tmp_path / "out.png"
    ingest.extract_page(tmp_path, 2, out)
    assert Image.open(out).size == (40, 30)


def test_pdf_single_image_extracted_at_native_resolution(tmp_path):
    src = tmp_path / "scan.png"
    make_page_image(src, w=80, h=60, color=(100, 100, 100))
    pdf = tmp_path / "book.pdf"
    make_scan_pdf(pdf, [src] * 2)
    assert ingest.page_count(pdf) == 2
    out = tmp_path / "p1.png"
    ingest.extract_page(pdf, 1, out)
    assert np.array_equal(np.asarray(Image.open(out).convert("RGB")), np.asarray(Image.open(src).convert("RGB")))


def test_pdf_out_of_range_and_bad_page(tmp_path):
    pdf = tmp_path / "book.pdf"
    src = tmp_path / "scan.png"
    make_page_image(src)
    make_scan_pdf(pdf, [src])
    with pytest.raises(ValueError):
        ingest.extract_page(pdf, 2, tmp_path / "x.png")


def test_text_pages_form_feed_breaks(tmp_path):
    txt = tmp_path / "book.txt"
    txt.write_text("अग्निः\nस्वस्ति\fयज्ञः", encoding="utf-8")
    pages = ingest.load_text_pages(txt)
    assert [p.page for p in pages] == [1, 2]
    assert pages[0].blocks[0].lines == ["अग्निः", "स्वस्ति"]
    assert pages[1].blocks[0].lines == ["यज्ञः"]
    assert all(b.lang == "sa" and b.bbox is None for p in pages for b in p.blocks)


def test_text_pages_blank_line_breaks(tmp_path):
    txt = tmp_path / "book.txt"
    txt.write_text("अग्निः\nस्वस्ति\n\n\nसरस्वती", encoding="utf-8")
    pages = ingest.load_text_pages(txt)
    assert [p.page for p in pages] == [1, 2]
    assert pages[1].blocks[0].lines == ["सरस्वती"]


def test_text_pages_empty_raises(tmp_path):
    (tmp_path / "e.txt").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        ingest.load_text_pages(tmp_path / "e.txt")


def test_json_inputs_single_list_and_master(tmp_path):
    def page_dict(n):
        return {"page": n, "blocks": [{"id": "b1", "type": "verse", "lang": "sa", "lines": ["ॐ"]}]}

    single = tmp_path / "p.json"
    single.write_text(json.dumps(page_dict(2)), encoding="utf-8")
    assert [p.page for p in ingest.load_text_pages(single)] == [2]

    many = tmp_path / "pages.json"
    many.write_text(json.dumps([page_dict(3), page_dict(1)]), encoding="utf-8")
    assert [p.page for p in ingest.load_text_pages(many)] == [1, 3]

    master = tmp_path / "master.json"
    Master(pages=[Page(**page_dict(5))]).save(master)
    assert [p.page for p in ingest.load_text_pages(master)] == [5]

    bad = tmp_path / "bad.json"
    bad.write_text('{"nothing": 1}', encoding="utf-8")
    with pytest.raises(ValueError):
        ingest.load_text_pages(bad)
