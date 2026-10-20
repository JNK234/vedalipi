# ABOUTME: Tests for translated editions: source-script OCR prompts, Telugu->Devanagari normalize, roman svaras,
# ABOUTME: the translate stage (answer checks, caching, hand edits, cost) and rendering meanings under mantras.

from __future__ import annotations

import json
import unicodedata

import pymupdf
import pytest

from vedalipi import pipeline, transliterate
from vedalipi import translate as tl
from vedalipi.ocr import get_backend
from vedalipi.ocr import prompt as ocr_prompt
from vedalipi.ocr.llm import Answer
from vedalipi.pipeline import PageJob, RunConfig
from vedalipi.render.document import STYLE, STYLE_TRANSLATED, build_document
from vedalipi.schema import (
    Block,
    BlockTranslation,
    Label,
    Manifest,
    Master,
    Page,
    PageTranslation,
    Translation,
)

# ---------------------------------------------------------------- OCR prompt per source script


def test_devanagari_prompt_and_fingerprint_are_unchanged():
    assert ocr_prompt.system_prompt() == ocr_prompt.SYSTEM_PROMPT
    b = get_backend("claude-cli")
    assert ":src-" not in b.fingerprint()


def test_template_prompt_names_script_languages_and_notes():
    s = ocr_prompt.system_prompt("telugu", ("te", "sa"))
    assert "printed in Telugu script" in s and "te, sa" in s
    assert "TELUGU GLYPHS" in s and "talakattu" in s
    # The shared layout rules follow, with the language rule rewritten for this book.
    assert "LAYOUT → BLOCKS" in s and "Sanskrit/Devanagari text" not in s


def test_script_without_notes_still_gets_a_prompt():
    s = ocr_prompt.system_prompt("kannada", ("kn", "sa"))
    assert "printed in Kannada script" in s and "GLYPHS" not in s


def test_fingerprint_follows_script_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr_prompt, "SCRIPT_NOTES_DIR", tmp_path)
    (tmp_path / "telugu.txt").write_text("- one note", encoding="utf-8")
    b = get_backend("claude-cli", source_script="telugu", languages=("te", "sa"))
    first = b.fingerprint()
    (tmp_path / "telugu.txt").write_text("- another note", encoding="utf-8")
    assert b.fingerprint() != first and ":src-telugu:" in first


def test_backend_without_prompts_rejects_other_scripts():
    with pytest.raises(ValueError, match="Devanagari"):
        get_backend("tesseract", source_script="telugu")


# ---------------------------------------------------------------- Telugu source -> Devanagari master


def telugu_page() -> Page:
    return Page(page=1, blocks=[
        Block(id="b1", type="prose", lang="te", lines=["ఈ మంత్రమును చెప్పవలెను."]),
        Block(id="b2", type="verse", lang="sa", lines=["అ॒గ్నిమీ॑ళే పు॒రోహి॑తం।"]),
        Block(id="b3", type="table", lang="sa", rows=[["కేశవాయ నమః", "1"]]),
        Block(id="b4", type="image", lang="sa", bbox=[0, 0, 1, 1], labels=[
            Label(text="ఉత్తరం", lang="te", bbox=[0, 0, 0.5, 0.5]),
            Label(text="నమః", lang="sa", bbox=[0.5, 0.5, 1, 1]),
        ]),
    ])


def test_page_to_devanagari_converts_only_sanskrit():
    out = transliterate.page_to_devanagari(telugu_page(), "telugu")
    assert out.block("b1").lines == ["ఈ మంత్రమును చెప్పవలెను."]
    assert out.block("b2").lines == ["अ॒ग्निमी॑ळे पु॒रोहि॑तं।"]
    assert out.block("b3").rows == [["केशवाय नमः", "1"]]
    assert [lab.text for lab in out.block("b4").labels] == ["ఉత్తరం", "नमः"]


def test_normalize_stage_converts_a_telugu_book(tmp_path):
    src = tmp_path / "book.txt"
    src.write_text("x", encoding="utf-8")
    cfg = RunConfig(input_path=src, out_dir=tmp_path / "out", source_script="telugu")
    job = PageJob(1, pipeline.page_dir(cfg.work_dir, 1), cfg)
    job.page_dir.mkdir(parents=True)
    telugu_page().save(job.path(pipeline.OCR_JSON))
    pipeline._normalize(job)
    assert Page.load(job.path(pipeline.NORM_JSON)).block("b2").lines == ["अ॒ग्निमी॑ळे पु॒रोहि॑तं।"]
    deva = RunConfig(input_path=src, out_dir=tmp_path / "out")
    assert pipeline._normalize_fingerprint(job) != pipeline._normalize_fingerprint(PageJob(1, job.page_dir, deva))


def test_validate_skips_the_headline_svara_count_for_other_scripts(tmp_path, monkeypatch):
    from vedalipi import validate

    seen = []
    monkeypatch.setattr(validate, "validate_page", lambda page, image, second=None: seen.append(image) or
                        validate.FlagReport(page=page.page))
    src = tmp_path / "book.txt"
    src.write_text("x", encoding="utf-8")
    for script in ("telugu", "devanagari"):
        cfg = RunConfig(input_path=src, out_dir=tmp_path / script, source_script=script)
        job = PageJob(1, pipeline.page_dir(cfg.work_dir, 1), cfg)
        job.page_dir.mkdir(parents=True)
        Page(page=1).save(job.path(pipeline.NORM_JSON))
        job.path(pipeline.PRE_PNG).write_bytes(b"png")
        pipeline._validate(job)
    assert seen[0] is None and seen[1] is not None


# ---------------------------------------------------------------- roman schemes


def test_is_roman():
    assert transliterate.is_roman("iast") and transliterate.is_roman("iso15919") and transliterate.is_roman("harvardkyoto")
    assert not transliterate.is_roman("telugu") and not transliterate.is_roman("devanagari")


@pytest.mark.parametrize("deva, iast", [
    ("भु॑वः॑", "bhu̍va̍ḥ"),  # u + svarita, which vidyut fuses into ṷ
    ("पा॑पे", "pā̍pe"),  # svarita after the macron, so ā stays one letter
    ("स॒वि", "sa̱vi"),
    ("वरे᳚ण्यं", "vare̎ṇyaṃ"),
    ("धीमहि। प्र॥", "dhīmahi| pra||"),
    ("नक्षत्रे ........ गोत्रः", "nakṣatre ........ gotraḥ"),
    ("‘गायन्तं’", "‘gāyantaṃ’"),
    ("सोऽहम्", "so'ham"),
])
def test_roman_display_and_round_trip(deva, iast):
    raw, out = transliterate._render_line(deva, "iast", None)
    assert unicodedata.normalize("NFC", out) == unicodedata.normalize("NFC", iast)
    assert transliterate.roundtrip_ok(deva, raw, "iast")
    # No Devanagari mark is left for a Latin font to miss.
    assert not any(c in out for c in "॒॑᳚")


def test_roman_vedic_anusvara_round_trips():
    raw, out = transliterate._render_line("सꣳहि॒ता", "iast", None)
    assert out.startswith("sam̐hi") and transliterate.roundtrip_ok("सꣳहि॒ता", raw, "iast")


def test_indic_targets_are_untouched_by_roman_rules():
    assert transliterate.transliterate_text("धीमहि। ....", "kannada") == "ಧೀಮಹಿ। ...."


# ---------------------------------------------------------------- translate stage


def deva_master(title="Test book") -> Master:
    return Master(title=title, source="book.pdf", pages=[
        Page(page=1, blocks=[
            Block(id="b1", type="heading", lang="te", lines=["సంధ్య"]),
            Block(id="b2", type="verse", lang="sa", lines=["ॐ भूर्भु॑वः॑"]),
            Block(id="b3", type="table", lang="te", rows=[["ఒకటి", "1"], ["రెండు", "2"]]),
            Block(id="b4", type="page_number", lang="en", lines=["1"]),
            Block(id="b5", type="separator", lang="en"),
        ]),
        Page(page=2, blocks=[Block(id="b1", type="prose", lang="te", lines=["నీళ్లు తీసుకొని"])]),
        Page(page=3, blocks=[Block(id="b1", type="page_number", lang="en", lines=["3"])]),
    ])


def good_answer(payload: dict) -> str:
    blocks = []
    for b in payload["blocks"]:
        if "rows" in b:
            blocks.append({"id": b["id"], "rows": [[f"EN {c}" for c in r] for r in b["rows"]]})
        elif "labels" in b:
            blocks.append({"id": b["id"], "labels": [f"EN {x}" for x in b["labels"]]})
        else:
            item = {"id": b["id"], "lines": [f"EN {line}" for line in b["lines"]]}
            if b["lang"] == "sa":
                item["gloss"] = [["oṃ", "the sacred syllable"]]
            blocks.append(item)
    return json.dumps({"blocks": blocks}, ensure_ascii=False)


class FakeClaude:
    """Stands in for the claude CLI: answers from the page JSON in the prompt, logs every call."""

    def __init__(self, answers=None, cost=0.1):
        self.calls: list[str] = []
        self.answers = list(answers or [])
        self.cost = cost

    def ask_text(self, system: str, prompt: str) -> Answer:
        self.calls.append(prompt)
        if self.answers:
            return Answer(self.answers.pop(0), self.cost)
        # Retries append feedback after the page JSON: read just the JSON object.
        payload, _ = json.JSONDecoder().raw_decode(prompt.split("PAGE:\n", 1)[1])
        return Answer(good_answer(payload), self.cost)


def setup_out(tmp_path, master=None) -> tl.TranslateConfig:
    out = tmp_path / "out"
    (out / "work").mkdir(parents=True)
    (master or deva_master()).save(out / "master.json")
    Manifest(input_path="book.pdf", input_hash="h", input_kind="pdf", num_pages=3).save(out / "work" / "manifest.json")
    return tl.TranslateConfig(out_dir=out, context="Smarta, Krishna Yajurveda", jobs=2)


def test_payload_skips_blocks_without_text_and_adds_context_before():
    m = deva_master()
    payload = tl.page_payload(m.page(2), m.page(1))
    assert [b["id"] for b in payload["blocks"]] == ["b1"]
    assert payload["context_before"] == ["ॐ भूर्भु॑वः॑"]
    assert [b["id"] for b in tl.page_payload(m.page(1))["blocks"]] == ["b1", "b2", "b3"]
    assert tl.page_payload(m.page(3))["blocks"] == []


def test_prompts_carry_language_context_glossary_and_gloss_rule(tmp_path):
    cfg = setup_out(tmp_path)
    cfg.to, cfg.glossary = "hi", {"arghya": "अर्घ्य"}
    system = tl.system_prompt(cfg)
    assert 'ISO 639-1 code "hi"' in system and "GLOSS" in system and "IAST letters" in system
    cfg.gloss = "none"
    assert "GLOSS:" not in tl.system_prompt(cfg)
    user = tl.user_prompt(cfg, "Book", 4, {"blocks": []})
    assert "Smarta, Krishna Yajurveda" in user and "- arghya: अर्घ्य" in user and "page 4" in user


@pytest.mark.parametrize("answer, error", [
    ('{"blocks": []}', "missing"),
    ('{"blocks": [{"id": "b1", "lines": ["x"]}, {"id": "b2", "lines": ["y"]}, {"id": "b3", "rows": [["a"]]}]}', "same shape"),
    ('{"blocks": [{"id": "b1", "lines": []}, {"id": "b2", "lines": ["y"]}, {"id": "b3", "rows": [["a","b"],["c","d"]]}]}', "non-empty"),
    ("no json here", ""),
])
def test_parse_answer_rejects_answers_that_do_not_fit(answer, error):
    payload = tl.page_payload(deva_master().page(1))
    with pytest.raises(ValueError, match=error):
        tl.parse_answer(answer, payload, gloss="key")


def test_parse_answer_keeps_gloss_only_for_sanskrit():
    payload = tl.page_payload(deva_master().page(1))
    raw = json.loads(good_answer(payload))
    raw["blocks"][0]["gloss"] = [["x", "y"]]  # a Telugu heading cannot carry a gloss
    out = tl.parse_answer(json.dumps(raw), payload, gloss="key")
    assert out["b1"].gloss is None and out["b2"].gloss == [["oṃ", "the sacred syllable"]]
    assert tl.parse_answer(json.dumps(raw), payload, gloss="none")["b2"].gloss is None


def test_translate_master_caches_records_cost_and_keeps_hand_edits(tmp_path):
    cfg = setup_out(tmp_path)
    fake = FakeClaude()
    tr, failed = tl.translate_master(cfg, backend=fake)
    assert failed == [] and len(fake.calls) == 2  # page 3 has nothing to translate
    assert tr.pages[1].blocks["b3"].rows == [["EN ఒకటి", "EN 1"], ["EN రెండు", "EN 2"]]
    assert sorted(tr.pages) == [1, 2, 3]
    m = Manifest.load(cfg.out_dir / "work" / "manifest.json")
    assert m.record(1, "translate-en").cost_usd == pytest.approx(0.1)

    # A hand edit survives a re-run with the same text and settings, and nothing is re-asked.
    tr.pages[2].blocks["b1"].lines = ["Take water."]
    tr.save(cfg.path)
    tr2, _ = tl.translate_master(cfg, backend=fake)
    assert len(fake.calls) == 2 and tr2.pages[2].blocks["b1"].lines == ["Take water."]

    # Changed text on page 2 re-translates page 2 only.
    m2 = deva_master()
    m2.page(2).blocks[0].lines = ["వేరే వాక్యం"]
    m2.save(cfg.out_dir / "master.json")
    tl.translate_master(cfg, backend=fake)
    assert len(fake.calls) == 3 and "వేరే వాక్యం" in fake.calls[-1]
    assert tl.stale_pages(m2, Translation.load(cfg.path), cfg) == []

    # Changed settings re-translate every page.
    cfg.gloss = "none"
    assert tl.stale_pages(m2, Translation.load(cfg.path), cfg) == [1, 2]


def test_translate_retries_with_feedback_then_records_failure(tmp_path):
    cfg = setup_out(tmp_path)
    cfg.pages, cfg.retries = [2], 1
    fake = FakeClaude(answers=["not json", '{"blocks": []}'])
    tr, failed = tl.translate_master(cfg, backend=fake)
    assert [p for p, _ in failed] == [2] and 2 not in tr.pages
    assert "could not be used" in fake.calls[1]
    rec = Manifest.load(cfg.out_dir / "work" / "manifest.json").record(2, "translate-en")
    assert rec.status == "failed" and rec.spent_usd == pytest.approx(0.2)
    # The next answer is usable: the retry succeeds and the page is stored.
    tr, failed = tl.translate_master(cfg, backend=FakeClaude(answers=["oops"]))
    assert failed == [] and tr.pages[2].blocks["b1"].lines == ["EN నీళ్లు తీసుకొని"]


def test_bad_gloss_mode_is_rejected(tmp_path):
    cfg = setup_out(tmp_path)
    cfg.gloss = "some"
    with pytest.raises(ValueError, match="gloss"):
        tl.translate_master(cfg, backend=FakeClaude())


def test_apply_translation_replaces_other_languages_and_returns_meanings():
    m = deva_master()
    m.page(1).blocks.append(Block(id="b6", type="image", lang="sa", bbox=[0, 0, 1, 1], labels=[
        Label(text="ఉత్తరం", lang="te", bbox=[0, 0, 0.5, 0.5]), Label(text="नमः", lang="sa", bbox=[0.5, 0.5, 1, 1])]))
    tr = Translation(lang="en", mantra_script="iast", pages={1: PageTranslation(key="k", blocks={
        "b1": BlockTranslation(lines=["Sandhya"]),
        "b2": BlockTranslation(lines=["Om, earth and sky."], gloss=[["oṃ", "om"]]),
        "b3": BlockTranslation(rows=[["One", "1"], ["Two", "2"]]),
        "b6": BlockTranslation(labels=["North", "salutation"]),
    })})
    target = transliterate.transliterate_master(m, "iast")
    out, meanings, missing = tl.apply_translation(target, tr)
    p1 = out.page(1)
    assert p1.block("b1").lines == ["Sandhya"] and p1.block("b1").lang == "en"
    assert p1.block("b2").lines == ["oṃ bhūrbhu̍va̍ḥ"] and p1.block("b2").lang == "sa"
    assert meanings[(1, "b2")].lines == ["Om, earth and sky."]
    assert p1.block("b3").rows == [["One", "1"], ["Two", "2"]]
    # A Telugu label is translated; a Sanskrit label stays transliterated.
    assert [lab.text for lab in p1.block("b6").labels] == ["North", "namaḥ"]
    assert missing == [(2, "b1")]


# ---------------------------------------------------------------- render


def test_document_of_a_translated_edition(tmp_path):
    m = deva_master()
    tr = Translation(lang="en", mantra_script="iast", pages={1: PageTranslation(key="k", blocks={
        "b1": BlockTranslation(lines=["Sandhya"]), "b2": BlockTranslation(lines=["Om."], gloss=[["oṃ", "om"]]),
        "b3": BlockTranslation(rows=[["One", "1"], ["Two", "2"]])})})
    target, meanings, _ = tl.apply_translation(transliterate.transliterate_master(m, "iast"), tr)
    doc = build_document(m, target, script="iast", side_by_side=False, work_dir=tmp_path, build_dir=tmp_path,
                         meanings=meanings, meaning_lang="en", translated=True)
    blocks = {b["id"]: b for b in doc["pages"][0]["blocks"]}
    assert doc["style"] == STYLE_TRANSLATED and STYLE_TRANSLATED["size"] < STYLE["size"]
    assert blocks["b2"]["meaning"]["lines"] == ["Om."] and blocks["b2"]["meaning"]["gloss"] == [["oṃ", "om"]]
    assert blocks["b2"]["vedic"] and not blocks["b2"]["reflow"]
    assert blocks["b1"]["reflow"] and blocks["b1"]["meaning"] is None
    plain = build_document(m, m, script="kannada", side_by_side=False, work_dir=tmp_path, build_dir=tmp_path)
    assert plain["style"] == STYLE and not plain["pages"][0]["blocks"][0]["reflow"]


def test_render_translated_edition_end_to_end(tmp_path):
    cfg = setup_out(tmp_path)
    tl.translate_master(cfg, backend=FakeClaude())
    pdf = pipeline.render_from_master(cfg.out_dir, "iast", translation="en")
    assert pdf.name == "book.iast.en.pdf"
    text = unicodedata.normalize("NFC", "".join(page.get_text() for page in pymupdf.open(pdf)))
    assert "EN ॐ भूर्भु॑वः॑" in text  # the fake's meaning of the mantra
    assert "EN నీళ్లు తీసుకొని" in text and "oṃ" in text
    assert "the sacred syllable" in text  # the gloss


def test_render_translation_needs_its_file(tmp_path):
    cfg = setup_out(tmp_path)
    with pytest.raises(FileNotFoundError, match="vedalipi translate"):
        pipeline.render_from_master(cfg.out_dir, "iast", translation="en")
