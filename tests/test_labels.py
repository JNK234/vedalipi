# ABOUTME: Tests for drawing labels: image blocks keep their scan crop but show their text labels in the target script.
# ABOUTME: Covers label validation, corrections that turn a table into a labelled drawing, transliteration, render and judge.

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from vedalipi import judge, pipeline, transliterate
from vedalipi.render import build_dir_for
from vedalipi.schema import Block, Correction, Label, Manifest, Master, Page

# Page coordinates of the drawing and of two labels printed inside it (on a 600x900 scan).
DRAWING = [0.2, 0.3, 0.8, 0.7]
LABELS = [
    Label(text="सूर्यः", bbox=[0.3, 0.35, 0.45, 0.4]),
    Label(text="Sun (सोमः)", lang="en", bbox=[0.45, 0.55, 0.7, 0.6]),
]


def make_scan(path: Path) -> None:
    """A fake scan: a boxed drawing whose label spots are filled with black 'ink'."""
    im = Image.new("RGB", (600, 900), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((150, 300, 450, 600), outline="black", width=6)
    for lab in LABELS:
        x0, y0, x1, y1 = lab.bbox
        d.rectangle((x0 * 600, y0 * 900, x1 * 600 - 1, y1 * 900 - 1), fill="black")
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def labelled_master() -> Master:
    page = Page(page=1, blocks=[
        Block(id="t", type="heading", lines=["मण्डलम्"]),
        Block(id="d", type="image", bbox=DRAWING, labels=LABELS),
        Block(id="p", type="prose", lines=["अथ संकल्पः ।"]),
    ])
    return Master(title="labels", source="labels.pdf", pages=[page])


def make_out(out: Path, master: Master) -> Path:
    (out / "work").mkdir(parents=True)
    master.save(out / "master.json")
    Manifest(input_path="labels.pdf", input_hash="x", input_kind="pdf", num_pages=1).save(out / "work" / "manifest.json")
    make_scan(out / "work" / "pages" / "p0001" / "pre.png")
    return out


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> Path:
    out = make_out(tmp_path_factory.mktemp("labels") / "out", labelled_master())
    pipeline.render_from_master(out, "kannada")
    return out


def test_labels_only_on_image_blocks_and_inside_them():
    with pytest.raises(ValidationError, match="only image blocks may have labels"):
        Block(id="x", type="prose", lines=["a"], labels=[Label(text="a", bbox=[0.1, 0.1, 0.2, 0.2])])
    with pytest.raises(ValidationError, match="outside image block"):
        Block(id="x", type="image", bbox=DRAWING, labels=[Label(text="a", bbox=[0.1, 0.35, 0.3, 0.4])])
    with pytest.raises(ValidationError):
        Label(text="a", bbox=[0.5, 0.5, 0.4, 0.6])
    ok = Block(id="x", type="image", bbox=DRAWING, labels=LABELS)
    assert ok.labels[1].lang == "en" and ok.labels[0].lang == "sa"


def test_blocks_without_labels_serialize_as_before():
    b = Block(id="x", type="prose", lines=["a"])
    assert "labels" not in json.loads(b.model_dump_json())
    m = labelled_master()
    assert Master.model_validate_json(m.to_json()) == m


def test_correction_turns_table_into_labelled_image():
    table = Block(id="b5", type="table", rows=[["सूर्यः", "सोमः"]], bbox=DRAWING, flags=["low_confidence"])
    master = Master(pages=[Page(page=3, blocks=[table])])
    fixed = pipeline.apply_corrections(master, [Correction(page=3, block_id="b5", type="image", labels=LABELS)])
    b = fixed.page(3).block("b5")
    assert (b.type, b.lines, b.rows, b.bbox, b.flags) == ("image", [], None, DRAWING, ["low_confidence"])
    assert b.labels == LABELS
    assert master.page(3).block("b5").type == "table"  # the input is not modified

    # A block without a bbox needs one given in the correction.
    prose = Master(pages=[Page(page=1, blocks=[Block(id="p", type="prose", lines=["x"])])])
    with pytest.raises(ValidationError, match="needs a bbox"):
        pipeline.apply_corrections(prose, [Correction(page=1, block_id="p", type="image")])
    got = pipeline.apply_corrections(prose, [Correction(page=1, block_id="p", type="image", bbox=DRAWING)])
    assert got.page(1).block("p").bbox == DRAWING and got.page(1).block("p").lines == []
    # A label outside the drawing is refused.
    with pytest.raises(ValidationError, match="outside image block"):
        pipeline.apply_corrections(master, [Correction(
            page=3, block_id="b5", type="image", labels=[Label(text="x", bbox=[0.0, 0.0, 0.1, 0.1])])])


def test_labels_are_transliterated():
    diags: list[dict] = []
    target = transliterate.transliterate_master(labelled_master(), "kannada", diagnostics=diags)
    labels = target.page(1).block("d").labels
    assert [lab.text for lab in labels] == ["ಸೂರ್ಯಃ", "Sun (ಸೋಮಃ)"]
    assert diags == [] and "roundtrip_fail" not in target.page(1).block("d").flags
    assert labelled_master().page(1).block("d").labels[0].text == "सूर्यः"  # input untouched


def test_label_roundtrip_failure_is_flagged(monkeypatch):
    monkeypatch.setattr(transliterate, "roundtrip_ok", lambda *a: False)
    diags: list[dict] = []
    target = transliterate.transliterate_master(labelled_master(), "kannada", diagnostics=diags)
    assert "roundtrip_fail" in target.page(1).block("d").flags
    assert {"सूर्यः", "सोमः"} <= {d["text"] for d in diags if d["block"] == "d"}


def test_render_overlays_labels_and_whites_out_the_scan_text(rendered):
    pdf = pipeline.pdf_path(rendered, labelled_master(), "kannada")
    with pymupdf.open(pdf) as doc:
        text = "\n".join(page.get_text() for page in doc)
        assert sum(len(p.get_images()) for p in doc) == 1
    assert "ಸೂರ್ಯಃ" in text and "Sun(ಸೋಮಃ)" in judge.norm(text)
    crop = build_dir_for(rendered / "work", "kannada", False) / "img" / "p0001-002.png"
    with Image.open(crop) as im:
        im = im.convert("L")
        w, h = im.size
        for lab in LABELS:
            x0, y0, x1, y1 = lab.bbox
            # The label's ink, in crop pixels, is painted white.
            box = (round((x0 - 0.2) * 600), round((y0 - 0.3) * 900), round((x1 - 0.2) * 600), round((y1 - 0.3) * 900))
            assert im.crop(box).getextrema() == (255, 255)
        # The drawing itself (its frame) is untouched.
        assert im.getpixel((33, h // 2)) < 50 and im.getpixel((w // 2, 33)) < 50 and im.getpixel((w - 33, h - 33)) < 50


def test_label_placed_inside_its_spot(rendered):
    data = json.loads((build_dir_for(rendered / "work", "kannada", False) / "data.json").read_text(encoding="utf-8"))
    d = next(b for b in data["pages"][0]["blocks"] if b["id"] == "d")
    assert [lab["text"] for lab in d["labels"]] == ["ಸೂರ್ಯಃ", "Sun (ಸೋಮಃ)"]
    first = d["labels"][0]
    # The label's box holds its printed box (1/6, 0.125, w 0.25, h 0.125), grown into free space around it.
    assert first["x"] <= 1 / 6 + 1e-4 and first["y"] <= 0.125 + 1e-4
    assert first["x"] + first["w"] >= 1 / 6 + 0.25 - 1e-4 and first["y"] + first["h"] >= 0.25 - 1e-4
    pdf = pipeline.pdf_path(rendered, labelled_master(), "kannada")
    with pymupdf.open(pdf) as doc:
        page = doc[0]
        img = page.get_image_rects(page.get_images()[0][0])[0]
        hit = page.search_for("Sun")[0]
    # The overlaid text sits within the label's (grown) box on the drawn image.
    sun = d["labels"][1]
    lx0, ly0 = img.x0 + sun["x"] * img.width, img.y0 + sun["y"] * img.height
    lx1, ly1 = lx0 + sun["w"] * img.width, ly0 + sun["h"] * img.height
    assert lx0 - 1 <= hit.x0 and hit.x1 <= lx1 + 1 and ly0 - 3 <= hit.y0 and hit.y1 <= ly1 + 3


def test_judge_checks_labels(rendered, tmp_path):
    report = judge.judge(rendered, "kannada")
    assert report["passed"], report
    assert report["labels_checked"] == 2 and report["images_checked"] == 1
    # A label in master.json that the PDF does not show is missing.
    master = labelled_master()
    master.page(1).block("d").labels.append(Label(text="गुरुः", bbox=[0.3, 0.6, 0.4, 0.65]))
    out = tmp_path / "out"
    shutil.copytree(rendered, out)
    master.save(out / "master.json")
    report = judge.judge(out, "kannada")
    assert not report["passed"]
    assert [(m["block"], m["kind"], m["text"]) for m in report["missing"]] == [("d", "label", "ಗುರುಃ")]


def test_label_padding_whitens_glyph_edges_but_keeps_drawing_strokes(tmp_path):
    # A 200x200 scan: a label's ink at x 100-139, y 80-99, with a faint glyph edge 2 px left of its box, and a
    # drawing stroke (a line) that runs from far outside up to 2 px below the label's box.
    from vedalipi.render.document import LABEL_PAD, crop_image

    im = Image.new("L", (200, 200), 255)
    d = ImageDraw.Draw(im)
    d.rectangle((100, 80, 139, 99), fill=0)
    d.rectangle((98, 84, 98, 86), fill=120)  # anti-aliased edge of the old glyph, inside the padding
    d.rectangle((10, 101, 120, 103), fill=0)  # drawing line, its top rows inside the padding below the label
    src = tmp_path / "scan.png"
    im.save(src)
    dest = tmp_path / "crop.png"
    crop_image(src, [0, 0, 1, 1], dest, blank=[[100 / 200, 80 / 200, 140 / 200, 100 / 200]])
    with Image.open(dest) as out:
        assert out.crop((100 - LABEL_PAD, 80 - LABEL_PAD, 140 + LABEL_PAD, 100)).getextrema() == (255, 255)
        # The line keeps every pixel, including those within the padding; nothing past the padding is painted.
        assert out.crop((10, 101, 121, 104)).getextrema() == (0, 0)
        assert out.getpixel((100 - LABEL_PAD - 1, 90)) == 255 and out.getpixel((120, 120)) == 255


def test_label_rooms_grow_into_free_space_without_overlapping(tmp_path):
    from PIL import Image, ImageDraw

    from vedalipi.render.document import label_rooms

    png = tmp_path / "crop.png"
    im = Image.new("L", (400, 200), 255)
    ImageDraw.Draw(im).line((300, 0, 300, 199), fill=0, width=3)  # a drawing stroke at x=300
    im.save(png)
    bbox = [0.0, 0.0, 1.0, 1.0]
    a = [0.40, 0.40, 0.50, 0.50]  # printed label boxes, 40 px wide, 20 px tall
    b = [0.40, 0.62, 0.50, 0.72]  # a second label 24 px below the first
    ra, rb = label_rooms(png, bbox, [a, b])
    assert all(v >= 0 for v in (*ra, *rb))
    assert ra[0] > 0 and ra[2] > 0  # both labels widen sideways
    # Neither reaches the stroke at x=300 (0.75 of the width).
    assert a[2] + ra[2] < 0.75 and b[2] + rb[2] < 0.75
    # The grown boxes do not overlap vertically.
    assert a[3] + ra[3] <= b[1] - rb[1]
