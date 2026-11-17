# ABOUTME: Smoke test for examples/walkthrough.py: it runs offline end to end and shows each change working.
# ABOUTME: Keeps the walkthrough honest as the code changes; the assertions are the lines a reader checks.

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "walkthrough.py"


def test_walkthrough_runs_and_shows_each_change(capsys):
    spec = importlib.util.spec_from_file_location("walkthrough", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    for want in [
        "validate flags         {'b1': ['non_vedic_mark']}",
        "validate flags         {'b1': ['unconverted_letter']}",
        "normalized (master)    परि॒षद्यꣳ॑ स्तु॒वंतः।",
        "normalized (master)    गं॑तु",
        "validate, after        {}",
        "dotted circle (te)     no",
        "p0064.read1.json     ꣳ 7  expected 7  ok",
        "old (read 2 raw)       reader_disagreement lines: 0",
        "b3 line 1: second read differs: क॑विं / क॒विं",
        "b5 line 13: single_read: only the first read has this line",
        "3 pages: 2 PASS, 1 CHECK",
        "output_svara_mismatch: b1-1 line 1: ᳚ 1 -> 0",
        "output_svara_mismatch: b1-1 line 1: ꣳ 1 -> 0",
        "3 pages: 3 PASS, 0 CHECK",
        # 6. the proofreading cycle
        "39 lines to check on 3 pages, most svara differences first",
        "p1 b1-1: 6 of 6 listed lines checked, 1 edited; flags cleared: reader_disagreement",
        "16 entries (0 new, 0 kept from the last proof, 23 resolved)",
        "[p0003] normalize changed (version 3 -> 4); OCR text unchanged: page rebuilt, 1 correction re-applied",
        "p1 line 1 (edited)     ॐ ग॒णानां᳚ त्वा ग॒णप॑तिꣳ हवामहे क॒विं",
    ]:
        assert want in out, want
    assert "mismatch" not in out.split("Real fixture reads")[1].split("===")[0].lower()
    assert out.split("6. Proofreading cycle")[1].count("3 pages: 2 PASS, 1 CHECK") == 2  # before and after re-normalize
