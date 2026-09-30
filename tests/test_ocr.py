# ABOUTME: Offline tests for the OCR layer: JSON extraction/repair, block coercion, band planning, seam stitching,
# ABOUTME: claude CLI command/stream parsing and the retry loop. No network or model calls.

import json

import numpy as np
import pytest
from PIL import Image

from vedalipi.ocr import get_backend
from vedalipi.ocr.bands import Band, plan_bands, seam_overlap, stitch, to_page_bbox
from vedalipi.ocr.claude_cli import ClaudeCliBackend
from vedalipi.ocr.llm import Answer, BandedLlmBackend, OcrCallError
from vedalipi.ocr.parse import OcrParseError, coerce_block, extract_json, parse_blocks
from vedalipi.ocr.prompt import PROMPT_VERSION
from vedalipi.schema import Block, Page


# ---------- parsing ----------

def test_extract_json_fenced_with_chatter_and_trailing_commas():
    raw = 'Here you go:\n```json\n{"blocks": [{"type": "verse", "lines": ["अ॒ग्निम्",],},]}\n```\nDone.'
    assert extract_json(raw) == {"blocks": [{"type": "verse", "lines": ["अ॒ग्निम्"]}]}


def test_extract_json_trailing_junk_after_object():
    assert extract_json('{"blocks": []} and then {"x": 1') == {"blocks": []}


def test_parse_blocks_top_level_list_and_ids():
    blocks = parse_blocks('[{"type": "heading", "lines": ["श्रीः"], "level": 1}, {"type": "prose", "lines": "a\\nb"}]')
    assert [b.id for b in blocks] == ["b1", "b2"]
    assert blocks[0].level == 1
    assert blocks[1].lines == ["a", "b"]


@pytest.mark.parametrize("raw", ["no json here", '{"text": "x"}', "{broken", '{"blocks": ["x", 1]}'])
def test_parse_blocks_rejects_unusable(raw):
    with pytest.raises(OcrParseError):
        parse_blocks(raw)


def test_coerce_table_from_pipe_lines_and_rows_force_table():
    t = coerce_block({"type": "table", "lines": ["प्रार्थना | प्रतिवचनम्"]}, "t")
    assert t.rows == [["प्रार्थना", "प्रतिवचनम्"]] and t.lines == []
    r = coerce_block({"type": "prose", "rows": [["a", "b"]], "level": 2}, "r")
    assert r.type == "table" and r.level is None


def test_coerce_fixes_bad_fields_without_rejecting():
    b = coerce_block({"type": "Mystery Box", "lang": None, "align": "justify", "bbox": [0.9, 1.4, 0.1, -2],
                      "level": "x", "extra_key": 1, "flags": "truncated_right"}, "b")
    assert b.type == "prose" and b.lang == "sa" and b.align == "left"
    assert b.bbox == [0.1, 0.0, 0.9, 1.0]
    assert "truncated_right" in b.flags and "unknown_type:mystery_box" in b.flags
    img = coerce_block({"type": "image", "lang": "sa"}, "i")
    assert img.bbox == [0.0, 0.0, 1.0, 1.0] and "low_confidence" in img.flags
    h = coerce_block({"type": "heading", "lines": ["x"]}, "h")
    assert h.level == 2


# ---------- bands ----------

def _page_with_lines(h=1000, w=400, rows=((100, 130), (300, 330), (480, 520), (700, 730))):
    a = np.full((h, w), 255, np.uint8)
    for y0, y1 in rows:
        a[y0:y1, 20:380] = 0
    return a


def test_plan_bands_cuts_in_gaps_with_overlap():
    gray = _page_with_lines()
    bands = plan_bands(gray, 2, overlap=0.03, search=0.1)
    assert len(bands) == 2 and bands[0].y0 == 0 and bands[-1].y1 == 1000
    cut = bands[0].y1 - 30
    # seam lands mid-gap next to the middle line (480-520), never inside a text line
    assert cut in (440, 560)
    assert bands[1].y0 == cut - 30


def test_plan_bands_single():
    assert plan_bands(_page_with_lines(), 1) == [Band(1, 0, 1000, 1000)]


def test_to_page_bbox():
    band = Band(2, 500, 1000, 1000)
    assert to_page_bbox([0.1, 0.0, 0.9, 0.5], band) == [0.1, 0.5, 0.9, 0.75]
    assert to_page_bbox(None, band) is None


# ---------- stitching ----------

def B(i, typ="verse", lines=(), **kw):
    return Block(id=f"x{i}", type=typ, lines=list(lines), **kw)


def test_stitch_dedups_overlap_and_merges_prose():
    top = Band(1, 0, 600, 1000)
    bot = Band(2, 500, 1000, 1000)
    a = [B(1, "running_header", ["श्री सुदर्शन यज्ञः"]), B(2, "prose", ["one line", "two line", "three line"])]
    b = [B(1, "prose", ["three line", "four line"]), B(2, "page_number", ["7"], lang="en")]
    page = stitch([(top, a), (bot, b)])
    assert [blk.id for blk in page] == ["b1", "b2", "b3"]
    assert page[1].lines == ["one line", "two line", "three line", "four line"]
    assert page[2].type == "page_number"
    Page(page=1, blocks=page)  # valid contract


def test_stitch_drops_clipped_fragment_and_flags_disagreement():
    top, bot = Band(1, 0, 600, 1000), Band(2, 500, 1000, 1000)
    a = [B(1, "verse", ["ओं भ॒द्रं कर्णे॑भिः ।", "स्व॒स्ति नः॑ पू॒षा ॥"])]
    # next band starts with a clipped partial reading, then the same last line read slightly differently
    b = [B(1, "verse", ["ओ॒ं ॥", "स्व॒स्ति नः॑ पूषा ॥", "मङ्गलं श्रीविष्वक्सेनः ।"])]
    page = stitch([(top, a), (bot, b)])
    assert page[0].lines == ["ओं भ॒द्रं कर्णे॑भिः ।", "स्व॒स्ति नः॑ पू॒षा ॥"]
    assert "low_confidence" in page[0].flags
    assert page[1].lines == ["मङ्गलं श्रीविष्वक्सेनः ।"]  # verse ending in ॥ is not merged


def test_seam_overlap_none_when_no_repeat():
    seam = seam_overlap([B(1, lines=["a b c"])], [B(1, lines=["x y z"])])
    assert (seam.drop_next, seam.drop_prev, seam.pairs) == (0, 0, [])


def test_seam_ignores_dot_run_length():
    a = [B(1, "fill_blank", ["........ नक्षत्रे ...... राशौ"])]
    b = [B(1, "fill_blank", ["...... नक्षत्रे ........ राशौ"]), B(2, "fill_blank", ["नया"])]
    seam = seam_overlap(a, b)
    assert seam.drop_next == 1 and len(seam.pairs) == 1 and seam.pairs[0][0] == seam.pairs[0][1]


def test_stitch_drops_clipped_fragment_at_bottom_of_upper_band():
    top, bot = Band(1, 0, 600, 1000), Band(2, 500, 1000, 1000)
    a = [B(1, "prose", ["alpha complete line", "ZZ clip"])]
    b = [B(1, "prose", ["alpha complete line", "ZZ complete and whole", "beta"])]
    page = stitch([(top, a), (bot, b)])
    assert page[0].lines == ["alpha complete line", "ZZ complete and whole", "beta"]


def test_seam_ignores_repeats_far_from_overlap():
    top, bot = Band(1, 0, 600, 1000), Band(2, 500, 1000, 1000)
    refrain = "ओं शान्तिः शान्तिः शान्तिः ॥"
    # band-relative bboxes: the refrain sits at the very top of the upper band and the bottom of the lower one
    a = [B(1, "verse", [refrain], bbox=[0.1, 0.0, 0.9, 0.1]), B(2, "verse", ["middle ॥"], bbox=[0.1, 0.4, 0.9, 0.5])]
    b = [B(1, "verse", ["lower text"], bbox=[0.1, 0.5, 0.9, 0.6]), B(2, "verse", [refrain], bbox=[0.1, 0.9, 0.9, 1.0])]
    page = stitch([(top, a), (bot, b)])
    assert [blk.lines for blk in page] == [[refrain], ["middle ॥"], ["lower text"], [refrain]]


def test_image_captions_do_not_shift_dedup():
    a = [B(1, "prose", ["x line"]), Block(id="i", type="image", lines=["cap"], bbox=[0, 0, 1, 1])]
    b = [Block(id="i", type="image", lines=["cap"], bbox=[0, 0.9, 1, 1]), B(2, "prose", ["x line", "y line"])]
    seam = seam_overlap(a, b)
    assert seam.drop_next == 1


def test_stitch_merges_tables_and_straddling_image():
    top, bot = Band(1, 0, 600, 1000), Band(2, 500, 1000, 1000)
    a = [Block(id="i", type="image", bbox=[0.3, 0.8, 0.7, 1.0]),
         Block(id="t", type="table", rows=[["प्रार्थना", "प्रतिवचनम्"], ["अ", "आ"]])]
    b = [Block(id="i", type="image", bbox=[0.3, 0.0, 0.7, 0.2]),
         Block(id="t", type="table", rows=[["अ", "आ"], ["इ", "ई"]])]
    page = stitch([(top, a), (bot, b)])
    assert [p.type for p in page] == ["image", "table"]
    assert page[0].bbox == pytest.approx([0.3, 0.48, 0.7, 0.6])
    assert page[1].rows == [["प्रार्थना", "प्रतिवचनम्"], ["अ", "आ"], ["इ", "ई"]]


# ---------- claude CLI ----------

def test_cli_command_is_isolated():
    cmd = ClaudeCliBackend(bands=1).command("SYS")
    assert "--bare" not in cmd
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", "--verbose"):
        assert flag in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"


def test_cli_message_carries_inline_image(tmp_path):
    png = tmp_path / "b.png"
    Image.new("L", (4, 4), 255).save(png)
    msg = json.loads(ClaudeCliBackend.message(png, "read it"))
    content = msg["message"]["content"]
    assert msg["type"] == "user" and content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png" and content[1]["text"] == "read it"


def test_cli_parse_stream():
    out = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": '{"blocks": []}',
                    "total_cost_usd": 0.1123, "usage": {"output_tokens": 5}}),
    ])
    ans = ClaudeCliBackend.parse_stream(out)
    assert ans.text == '{"blocks": []}' and ans.cost_usd == pytest.approx(0.1123)
    with pytest.raises(OcrCallError):
        ClaudeCliBackend.parse_stream(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}))
    with pytest.raises(OcrCallError):
        ClaudeCliBackend.parse_stream("not json")


def test_registry_and_fingerprint():
    be = get_backend("claude-cli", model="claude-opus-5-5")
    assert isinstance(be, ClaudeCliBackend)
    assert f"prompt-{PROMPT_VERSION}" in be.fingerprint() and "bands3" in be.fingerprint()


# ---------- retry loop (scripted answers, no model) ----------

class Scripted(BandedLlmBackend):
    name = "scripted"
    default_model = "m"

    def __init__(self, answers, **kw):
        super().__init__(**kw)
        self.answers = list(answers)
        self.prompts = []

    def _ask(self, image_png, system, prompt):
        self.prompts.append(prompt)
        return Answer(self.answers.pop(0), 0.05)


def _page_png(tmp_path):
    p = tmp_path / "pre.png"
    Image.fromarray(_page_with_lines()).save(p)
    return p


def test_read_page_retries_malformed_then_succeeds(tmp_path):
    be = Scripted(["sorry, cannot", '{"blocks": [{"type": "verse", "lines": ["अ॒ग्निमी॑ळे"]}]}'], bands=1, retries=2)
    res = be.read_page(_page_png(tmp_path), 3)
    assert res.page.page == 3 and res.page.blocks[0].lines == ["अ॒ग्निमी॑ळे"]
    assert res.cost_usd == pytest.approx(0.10)
    assert "could not be used" in be.prompts[1]


def test_read_page_gives_up_after_retries(tmp_path):
    be = Scripted(["x", "y"], bands=1, retries=1)
    with pytest.raises(OcrParseError):
        be.read_page(_page_png(tmp_path), 1)


def test_read_page_two_bands_stitched(tmp_path):
    be = Scripted([
        '{"blocks": [{"type": "prose", "lines": ["l1", "l2"], "bbox": [0, 0, 1, 1]}]}',
        '{"blocks": [{"type": "prose", "lines": ["l2", "l3"]}]}',
    ], bands=2)
    res = be.read_page(_page_png(tmp_path), 1)
    assert [b.lines for b in res.page.blocks] == [["l1", "l2", "l3"]]
    assert "band 2 of 2" in be.prompts[1]


# ---------- review fixes ----------

def test_trailing_comma_repair_leaves_strings_alone():
    data = extract_json('{"blocks": [{"type": "prose", "lines": ["literal ,] retained", "a \\" ,} b"],},]}')
    assert data["blocks"][0]["lines"] == ["literal ,] retained", 'a " ,} b']


def test_blocks_list_with_non_object_entries_is_rejected():
    with pytest.raises(OcrParseError):
        parse_blocks('{"blocks": [{"type": "prose", "lines": ["a"]}, "stray text line", null]}')


def test_seam_keeps_unmatched_line_without_clipping_evidence():
    """[refrain, unique] above and [refrain, different] below: "unique" is not a clipped "different"."""
    top, bot = Band(1, 0, 600, 1000), Band(2, 500, 1000, 1000)
    a = [B(1, "verse", ["अग्निमीळे पुरोहितं", "यज्ञस्य देवमृत्विजम्"])]
    b = [B(1, "verse", ["अग्निमीळे पुरोहितं", "होतारं रत्नधातमम्"])]
    page = stitch([(top, a), (bot, b)])
    lines = [ln for blk in page for ln in blk.lines]
    assert "यज्ञस्य देवमृत्विजम्" in lines and "होतारं रत्नधातमम्" in lines
    assert "low_confidence" in page[0].flags


def test_cli_error_result_carries_its_cost():
    stream = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                         "result": "", "total_cost_usd": 0.31})
    with pytest.raises(OcrCallError) as e:
        ClaudeCliBackend.parse_stream(stream)
    assert e.value.cost_usd == pytest.approx(0.31)


def test_exhausted_retries_carry_the_spend(tmp_path):
    be = Scripted(["x", "y"], bands=1, retries=1)
    with pytest.raises(OcrParseError) as e:
        be.read_page(_page_png(tmp_path), 1)
    assert e.value.cost_usd == pytest.approx(0.10)


def test_later_band_failure_carries_earlier_band_cost(tmp_path):
    class FailSecond(Scripted):
        def _ask(self, image_png, system, prompt):
            if "band 2" in prompt:
                raise OcrCallError("boom", 0.07)
            return super()._ask(image_png, system, prompt)

    be = FailSecond(['{"blocks": [{"type": "prose", "lines": ["a"]}]}'], bands=2)
    with pytest.raises(OcrCallError) as e:
        be.read_page(_page_png(tmp_path), 1)
    assert e.value.cost_usd == pytest.approx(0.12)


def test_tesseract_fingerprint_follows_model_file(tmp_path, monkeypatch):
    from vedalipi.ocr.tesseract import TesseractBackend

    monkeypatch.setenv("VEDALIPI_TESSDATA", str(tmp_path))
    (tmp_path / "san.traineddata").write_bytes(b"model one")
    a = TesseractBackend("san").fingerprint()
    (tmp_path / "san.traineddata").write_bytes(b"model two")
    assert TesseractBackend("san").fingerprint() != a


def test_batch_checkpoint_resumes_submitted_batches(tmp_path):
    """An interrupted batch run re-run with the same pages collects the batches it already paid for."""
    from types import SimpleNamespace

    from vedalipi.ocr.anthropic_api import AnthropicApiBackend

    answer = '{"blocks": [{"type": "prose", "lines": ["अ"]}]}'

    class Batches:
        def __init__(self):
            self.created, self.ids = 0, {}

        def create(self, requests):
            self.created += 1
            bid = f"batch{self.created}"
            self.ids[bid] = [r["custom_id"] for r in requests]
            return SimpleNamespace(id=bid)

        def retrieve(self, bid):
            return SimpleNamespace(processing_status="ended")

        def results(self, bid):
            usage = SimpleNamespace(input_tokens=10, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0)
            msg = SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", usage=usage,
                                  content=[SimpleNamespace(type="text", text=answer)])
            return [SimpleNamespace(custom_id=c, result=SimpleNamespace(type="succeeded", message=msg)) for c in self.ids[bid]]

    batches = Batches()
    be = AnthropicApiBackend.__new__(AnthropicApiBackend)
    BandedLlmBackend.__init__(be, "claude-opus-5-5", bands=1)
    be.effort = "medium"
    be.client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    png = _page_png(tmp_path)
    ck = tmp_path / "batch.json"
    first = be.read_pages_batch([(png, 1)], poll_seconds=0, checkpoint=ck)
    second = be.read_pages_batch([(png, 1)], poll_seconds=0, checkpoint=ck)
    assert batches.created == 1
    assert first[1].page.blocks[0].lines == second[1].page.blocks[0].lines == ["अ"]
