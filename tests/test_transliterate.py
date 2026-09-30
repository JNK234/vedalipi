# ABOUTME: Tests for the transliterate stage: exact target-script codepoints for svaras, marks and symbols.
# ABOUTME: Also covers the round-trip check, the configurable U+A8F3 mapping, the sanscript fallback and master conversion.

import pytest

from vedalipi import transliterate
from vedalipi.schema import Block, Master, Page

# (Devanagari, Kannada, Telugu) — every case must convert exactly and round trip back.
CASES = [
    ("अ॒ग्निः", "ಅ॒ಗ್ನಿಃ", "అ॒గ్నిః"),  # anudatta before a conjunct
    ("सो॑मः", "ಸೋ॑ಮಃ", "సో॑మః"),  # svarita on a matra
    ("ग्नि᳚", "ಗ್ನಿ᳚", "గ్ని᳚"),  # double svarita on a conjunct + matra
    ("त॒त्त्व॑", "ತ॒ತ್ತ್ವ॑", "త॒త్త్వ॑"),  # anudatta and svarita around a conjunct
    ("कि॑", "ಕಿ॑", "కి॑"),  # svarita after a matra
    ("क᳚", "ಕ᳚", "క᳚"),  # double svarita on a bare consonant
    ("अं॑", "ಅಂ॑", "అం॑"),  # anusvara + svarita
    ("अः॑", "ಅಃ॑", "అః॑"),  # visarga + svarita
    ("कँ", "ಕಁ", "కఁ"),  # candrabindu
    ("१॒॑", "೧॒॑", "౧॒॑"),  # kampa: anudatta + svarita on a digit
    ("सोऽहं", "ಸೋಽಹಂ", "సోఽహం"),  # avagraha
    ("ॐ", "ಓಂ", "ఓం"),  # om sign
    ("ओं", "ಓಂ", "ఓం"),  # o + anusvara, folded by the engine to the same target form
    ("।", "।", "।"),  # danda
    ("॥", "॥", "॥"),  # double danda
    ("१२३", "೧೨೩", "౧౨౩"),  # Devanagari digits
    ("........", "........", "........"),  # dotted fill-in blank
    ("ळ", "ಳ", "ళ"),  # retroflex lla
    ("क़", "ಕ಼", "క఼"),  # nukta qa
]

IDS = [c[0] for c in CASES]


@pytest.mark.parametrize("deva,kannada,telugu", CASES, ids=IDS)
def test_kannada_exact_codepoints(deva, kannada, telugu):
    assert transliterate.transliterate_text(deva, "kannada") == kannada


@pytest.mark.parametrize("deva,kannada,telugu", CASES, ids=IDS)
def test_telugu_exact_codepoints(deva, kannada, telugu):
    assert transliterate.transliterate_text(deva, "telugu") == telugu


@pytest.mark.parametrize("deva,kannada,telugu", CASES, ids=IDS)
@pytest.mark.parametrize("script", ["kannada", "telugu"])
def test_roundtrip_passes(deva, kannada, telugu, script):
    converted = transliterate.transliterate_text(deva, script)
    assert transliterate.roundtrip_ok(deva, converted, script)


def test_svaras_are_preserved_as_codepoints():
    out = transliterate.transliterate_text("अ॒॑᳚", "kannada")
    assert "॑" in out and "॒" in out and "᳚" in out


def test_a8f3_default_mapping_kannada():
    assert transliterate.transliterate_text("अꣳ", "kannada") == "ಅಗ್ಂ"


def test_a8f3_default_mapping_telugu():
    assert transliterate.transliterate_text("अꣳ", "telugu") == "అగ్ం"


def test_a8f3_override():
    assert transliterate.transliterate_text("अꣳ", "kannada", a8f3="*") == "ಅ*"


def test_supported_scripts_lists_kannada_and_telugu():
    scripts = transliterate.supported_scripts()
    assert "kannada" in scripts and "telugu" in scripts and "devanagari" in scripts
    assert scripts == sorted(scripts)


def test_unknown_script_raises():
    with pytest.raises(ValueError):
        transliterate.transliterate_text("अ", "klingon")


def test_sanscript_fallback(monkeypatch):
    monkeypatch.setattr(transliterate, "_vidyut_schemes", lambda: {})
    assert transliterate.transliterate_text("अ॒ग्निः", "kannada") == "ಅ॒ಗ್ನಿಃ"
    assert transliterate.transliterate_text("सोऽहं", "telugu") == "సోఽహం"


def make_master(*blocks):
    return Master(title="t", source="book.pdf", pages=[Page(page=1, blocks=list(blocks))])


def test_master_transliterates_sa_only():
    master = make_master(
        Block(id="v", type="verse", lines=["अ॒ग्निः"]),
        Block(id="e", type="foreign", lang="en", lines=["Agni"]),
        Block(id="t", type="table", lang="sa", rows=[["सो॑मः", "Agni"]]),
    )
    out = transliterate.transliterate_master(master, "kannada")
    assert out.page(1).block("v").lines == ["ಅ॒ಗ್ನಿಃ"]
    assert out.page(1).block("e").lines == ["Agni"]
    assert out.page(1).block("t").rows == [["ಸೋ॑ಮಃ", "Agni"]]
    assert master.page(1).block("v").lines == ["अ॒ग्निः"]  # input untouched


def test_master_a8f3_default_is_not_a_roundtrip_failure():
    master = make_master(Block(id="v", type="verse", lines=["अꣳ"]))
    out = transliterate.transliterate_master(master, "kannada")
    assert out.page(1).block("v").lines == ["ಅಗ್ಂ"]
    assert out.page(1).block("v").flags == []


def test_master_flags_failed_roundtrip_and_keeps_going(monkeypatch):
    monkeypatch.setattr(transliterate, "roundtrip_ok", lambda *a, **k: False)
    master = make_master(
        Block(id="v", type="verse", lines=["अ॒ग्निः", "सो॑मः"]),
        Block(id="ok", type="verse", lines=["ग्नि᳚"]),
    )
    out = transliterate.transliterate_master(master, "kannada")
    assert out.page(1).block("v").flags == ["roundtrip_fail"]
    assert out.page(1).block("v").lines == ["ಅ॒ಗ್ನಿಃ", "ಸೋ॑ಮಃ"]


def test_devanagari_runs_in_english_block_are_converted():
    master = Master(pages=[Page(page=1, blocks=[
        Block(id="t", type="table", lang="en", rows=[["viii)", "Acharya Vandhanam (आचार्य वन्दनम्)", "xxv"]]),
    ])])
    out = transliterate.transliterate_master(master, "kannada")
    assert out.pages[0].blocks[0].rows == [["viii)", "Acharya Vandhanam (ಆಚಾರ್ಯ ವನ್ದನಮ್)", "xxv"]]
    assert out.pages[0].blocks[0].flags == []


def test_roundtrip_catches_a_character_the_target_drops():
    # U+0929 (ऩ) has no Telugu counterpart: it must not pass just because both sides lose it.
    converted = transliterate.transliterate_text("ऩ", "telugu")
    assert not transliterate.roundtrip_ok("ऩ", converted, "telugu")


def test_om_sign_and_o_anusvara_are_the_only_folded_pair():
    assert transliterate.roundtrip_ok("ॐ", transliterate.transliterate_text("ॐ", "kannada"), "kannada")


def test_explicit_empty_a8f3_strips_the_sign():
    assert transliterate.transliterate_text("अꣳ", "kannada", a8f3="") == "ಅ"
    assert transliterate.transliterate_text("अꣳ", "kannada") == "ಅಗ್ಂ"


def test_diagnostics_record_failed_lines_and_unknown_script_is_rejected():
    master = Master(pages=[Page(page=1, blocks=[Block(id="b1", type="verse", lines=["ऩ क", "क"])])])
    diags: list = []
    out = transliterate.transliterate_master(master, "telugu", diagnostics=diags)
    assert "roundtrip_fail" in out.page(1).block("b1").flags
    assert [(d["page"], d["block"], d["text"]) for d in diags] == [(1, "b1", "ऩ क")]
    with pytest.raises(ValueError, match="unsupported script"):
        transliterate.transliterate_master(Master(), "klingon")
