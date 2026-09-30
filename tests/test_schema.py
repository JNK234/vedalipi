# ABOUTME: Tests for the page/block contract, flag reports, manifest and master models in vedalipi.schema.
# ABOUTME: Covers validation rules and exact JSON round trips of Devanagari text with Vedic svara marks.

import pytest
from pydantic import ValidationError

from vedalipi.schema import Block, FlagReport, Manifest, Master, Page, StageRecord

SVARA_LINE = "अ॒ग्निमी॑ळे पु॒रोहि॑तं य॒ज्ञस्य॑ दे॒वमृ॒त्विज᳚म् । गꣳ ऽ"


def make_page(n=1):
    return Page(
        page=n,
        source_image=f"p{n:04d}/pre.png",
        blocks=[
            Block(id="b1", type="running_header", lines=["श्री सुदर्शन यज्ञः"], align="center"),
            Block(id="b2", type="verse", lines=[SVARA_LINE, "........ नक्षत्रे"]),
            Block(id="b3", type="table", rows=[["प्रार्थना", "प्रतिवचनम्"]]),
            Block(id="b4", type="foreign", lang="en", lines=["Contents"]),
            Block(id="b5", type="image", bbox=[0.1, 0.2, 0.5, 0.6]),
        ],
    )


def test_page_roundtrip_preserves_codepoints(tmp_path):
    page = make_page()
    path = tmp_path / "norm.json"
    page.save(path)
    loaded = Page.load(path)
    assert loaded == page
    assert loaded.block("b2").lines[0] == SVARA_LINE
    assert "॑" in loaded.block("b2").lines[0] and "ꣳ" in loaded.block("b2").lines[0]
    assert "अ॒ग्नि" in path.read_text(encoding="utf-8")  # stored as UTF-8, not \u escapes


def test_block_validation():
    with pytest.raises(ValidationError):
        Block(id="t", type="table")
    with pytest.raises(ValidationError):
        Block(id="v", type="verse", rows=[["x"]])
    with pytest.raises(ValidationError):
        Block(id="i", type="image")
    with pytest.raises(ValidationError):
        Block(id="b", type="prose", bbox=[0.5, 0.1, 0.2, 0.9])
    with pytest.raises(ValidationError):
        Block(id="b", type="prose", bbox=[0, 0, 1.2, 1])
    with pytest.raises(ValidationError):
        Block(id="b", type="poem")
    with pytest.raises(ValidationError):
        Block(id="b", type="prose", colour="red")


def test_duplicate_block_ids_rejected():
    with pytest.raises(ValidationError):
        Page(page=1, blocks=[Block(id="b1", type="prose"), Block(id="b1", type="verse")])


def test_flag_report_apply_does_not_mutate():
    page = make_page()
    report = FlagReport(page=1, block_flags={"b2": ["svara_count_mismatch", "truncated_right"]})
    flagged = report.apply_to(page)
    assert flagged.block("b2").flags == ["svara_count_mismatch", "truncated_right"]
    assert page.block("b2").flags == []
    assert report.apply_to(flagged).block("b2").flags == flagged.block("b2").flags  # idempotent


def test_manifest_roundtrip_int_keys_and_cost(tmp_path):
    m = Manifest(input_path="book.pdf", input_hash="abc", input_kind="pdf", num_pages=2)
    m.set_record(1, "ocr", StageRecord(status="done", input_hash="h1", cost_usd=0.25))
    m.set_record(2, "ocr", StageRecord(status="failed", error="boom"))
    m.save(tmp_path / "manifest.json")
    loaded = Manifest.load(tmp_path / "manifest.json")
    assert loaded.record(1, "ocr").cost_usd == 0.25
    assert loaded.record(2, "ocr").status == "failed"
    assert loaded.record(3, "ocr") is None
    assert loaded.total_cost() == 0.25


def test_master_upsert_keeps_order(tmp_path):
    master = Master(title="t")
    master.upsert_page(make_page(3))
    master.upsert_page(make_page(1))
    master.upsert_page(make_page(3).model_copy(update={"source_image": "x"}))
    assert [p.page for p in master.pages] == [1, 3]
    assert master.page(3).source_image == "x"
    master.norm_hashes[3] = "h"
    master.save(tmp_path / "master.json")
    assert Master.load(tmp_path / "master.json") == master
