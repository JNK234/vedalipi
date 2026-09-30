# ABOUTME: Tests for the resumable stage runner: hash-based skipping, force-stage, failures, concurrency.
# ABOUTME: Also covers page-range parsing, master building that keeps corrections, and corrections apply.

import json
import threading
import time
from pathlib import Path

import pytest

from vedalipi import pipeline
from vedalipi.pipeline import PageJob, RunConfig, Stage, StageRunner, parse_pages
from vedalipi.schema import Block, Correction, FlagReport, Manifest, Master, Page


def make_cfg(tmp_path, **kw):
    src = tmp_path / "book.txt"
    if not src.exists():
        src.write_text("x", encoding="utf-8")
    return RunConfig(input_path=src, out_dir=tmp_path / "out", **kw)


def make_manifest(n=3):
    return Manifest(input_path="book.txt", input_hash="doc", input_kind="text", num_pages=n)


class Counter:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []
        self.lock = threading.Lock()

    def hit(self, name, page):
        with self.lock:
            self.calls.append((name, page))


def text_stages(counter, source_text=lambda p: f"page {p}", fail_pages=()):
    def first(job: PageJob):
        counter.hit("first", job.page)
        job.path("a.txt").write_text(source_text(job.page), encoding="utf-8")
        return 0.5

    def second(job: PageJob):
        counter.hit("second", job.page)
        if job.page in fail_pages:
            raise RuntimeError("bad page")
        job.path("b.txt").write_text(job.path("a.txt").read_text().upper(), encoding="utf-8")

    return [
        Stage("first", (), ("a.txt",), first, lambda job: f"{job.shared.get('doc', '')}:{job.page}"),
        Stage("second", ("a.txt",), ("b.txt",), second),
    ]


def run_once(cfg, stages, shared=None, pages=(1, 2, 3), manifest=None):
    manifest = manifest or (Manifest.load(cfg.manifest_path) if cfg.manifest_path.exists() else make_manifest())
    runner = StageRunner(stages, manifest, cfg, shared or {})
    runner.run(pages)
    return runner


def test_second_run_is_fully_cached(tmp_path):
    cfg = make_cfg(tmp_path)
    c = Counter()
    r1 = run_once(cfg, text_stages(c))
    assert len(r1.ran) == 6 and not r1.skipped
    assert (cfg.work_dir / "pages" / "p0002" / "b.txt").read_text() == "PAGE 2"
    r2 = run_once(cfg, text_stages(c))
    assert not r2.ran and len(r2.skipped) == 6
    assert len(c.calls) == 6
    m = Manifest.load(cfg.manifest_path)
    assert m.record(1, "first").status == "done"
    assert m.total_cost() == 1.5


def test_changed_fingerprint_reruns_and_downstream_follows_content(tmp_path):
    cfg = make_cfg(tmp_path)
    c = Counter()
    run_once(cfg, text_stages(c), {"doc": "v1"})
    c.calls.clear()
    # New fingerprint, same output content: first reruns, second stays cached (its input bytes are equal).
    r = run_once(cfg, text_stages(c), {"doc": "v2"})
    assert sorted(set(n for n, _ in c.calls)) == ["first"]
    assert len(r.skipped) == 3
    c.calls.clear()
    # New fingerprint and new content: both rerun.
    run_once(cfg, text_stages(c, source_text=lambda p: f"new {p}"), {"doc": "v3"})
    assert sorted(set(n for n, _ in c.calls)) == ["first", "second"]
    assert (cfg.work_dir / "pages" / "p0001" / "b.txt").read_text() == "NEW 1"


def test_missing_output_file_reruns_stage(tmp_path):
    cfg = make_cfg(tmp_path)
    c = Counter()
    run_once(cfg, text_stages(c))
    (cfg.work_dir / "pages" / "p0003" / "b.txt").unlink()
    c.calls.clear()
    run_once(cfg, text_stages(c))
    assert c.calls == [("second", 3)]


def test_force_stage(tmp_path):
    c = Counter()
    run_once(make_cfg(tmp_path), text_stages(c))
    c.calls.clear()
    run_once(make_cfg(tmp_path, force_stages=frozenset({"second"})), text_stages(c))
    assert sorted(c.calls) == [("second", 1), ("second", 2), ("second", 3)]
    with pytest.raises(ValueError):
        StageRunner(text_stages(c), make_manifest(), make_cfg(tmp_path, force_stages=frozenset({"nope"})))


def test_failure_is_recorded_and_other_pages_continue_then_resume(tmp_path):
    cfg = make_cfg(tmp_path)
    c = Counter()
    r = run_once(cfg, text_stages(c, fail_pages={2}))
    assert [(p, s) for p, s, _ in r.failed] == [(2, "second")]
    m = Manifest.load(cfg.manifest_path)
    assert m.record(2, "second").status == "failed" and "bad page" in m.record(2, "second").error
    assert m.record(3, "second").status == "done"
    c.calls.clear()
    r = run_once(cfg, text_stages(c))
    assert c.calls == [("second", 2)] and not r.failed


def test_stage_that_writes_nothing_fails(tmp_path):
    cfg = make_cfg(tmp_path)
    r = run_once(cfg, [Stage("lazy", (), ("out.txt",), lambda job: None)], pages=(1,))
    assert r.failed and "wrote no" in r.failed[0][2]


def test_jobs_runs_pages_concurrently(tmp_path):
    cfg = make_cfg(tmp_path, jobs=4)
    active, peak, lock = [0], [0], threading.Lock()

    def slow(job: PageJob):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        job.path("o.txt").write_text("ok")
        with lock:
            active[0] -= 1

    r = run_once(cfg, [Stage("slow", (), ("o.txt",), slow)], pages=range(1, 9), manifest=make_manifest(8))
    assert len(r.ran) == 8 and 1 < peak[0] <= 4
    assert len(Manifest.load(cfg.manifest_path).pages) == 8


def test_parse_pages():
    assert parse_pages(None, 4) == [1, 2, 3, 4]
    assert parse_pages("1-2,4", 5) == [1, 2, 4]
    assert parse_pages("3-", 5) == [3, 4, 5]
    assert parse_pages("2,2,1", 5) == [1, 2]
    for bad in ("0", "6", "4-2", "1-9"):
        with pytest.raises(ValueError):
            parse_pages(bad, 5)


def write_norm(cfg, page, text):
    d = pipeline.page_dir(cfg.work_dir, page)
    d.mkdir(parents=True, exist_ok=True)
    Page(page=page, blocks=[Block(id="b1", type="verse", lines=[text])]).save(d / pipeline.NORM_JSON)
    FlagReport(page=page, block_flags={"b1": ["low_confidence"]}).save(d / pipeline.FLAGS_JSON)


def test_build_master_merges_flags_and_keeps_corrections(tmp_path):
    cfg = make_cfg(tmp_path)
    write_norm(cfg, 1, "अ॒ग्निः")
    write_norm(cfg, 2, "सोमः")
    master = pipeline.build_master(cfg, [1, 2])
    assert master.page(1).block("b1").flags == ["low_confidence"]

    fixed = pipeline.apply_corrections(master, [Correction(page=2, block_id="b1", lines=["सो॒मः"], flags=[])])
    assert master.page(2).block("b1").lines == ["सोमः"]  # original untouched
    fixed.save(cfg.master_path)

    master = pipeline.build_master(cfg, [1, 2])  # nothing re-OCR'd: correction survives
    assert master.page(2).block("b1").lines == ["सो॒मः"] and master.page(2).block("b1").flags == []

    write_norm(cfg, 2, "सोमो")  # page re-OCR'd: replaced
    master = pipeline.build_master(cfg, [1, 2])
    assert master.page(2).block("b1").lines == ["सोमो"]
    assert Master.load(cfg.master_path) == master


def test_apply_corrections_unknown_block_raises(tmp_path):
    cfg = make_cfg(tmp_path)
    write_norm(cfg, 1, "x")
    master = pipeline.build_master(cfg, [1])
    with pytest.raises(KeyError):
        pipeline.apply_corrections(master, [Correction(page=1, block_id="zz", lines=["y"])])


def test_report_summarizes(tmp_path):
    cfg = make_cfg(tmp_path)
    run_once(cfg, text_stages(Counter(), fail_pages={1}))
    write_norm(cfg, 1, "x")
    pipeline.build_master(cfg, [1])
    out = pipeline.report(cfg.out_dir)
    assert "FAILED p0001 second" in out and "low_confidence: 1" in out and "OCR cost: $1.50" in out


def test_cli_apply_corrections(tmp_path):
    from vedalipi.cli import main

    cfg = make_cfg(tmp_path)
    write_norm(cfg, 1, "x")
    pipeline.build_master(cfg, [1])
    corr = tmp_path / "c.json"
    corr.write_text(json.dumps([{"page": 1, "block_id": "b1", "lines": ["य॒ज्ञः"]}]), encoding="utf-8")
    assert main(["apply-corrections", str(cfg.out_dir), str(corr)]) == 0
    assert Master.load(cfg.master_path).page(1).block("b1").lines == ["य॒ज्ञः"]


def test_build_master_refreshes_flags_but_keeps_corrected_text(tmp_path):
    from vedalipi.pipeline import RunConfig, build_master, page_dir
    from vedalipi.schema import Block, FlagReport, Page

    cfg = RunConfig(input_path=tmp_path / "in.pdf", out_dir=tmp_path / "out")
    d = page_dir(cfg.work_dir, 1)
    d.mkdir(parents=True)
    Page(page=1, blocks=[Block(id="b1", type="verse", lines=["क"])]).save(d / "norm.json")
    FlagReport(page=1, block_flags={"b1": ["svara_count_mismatch"]}).save(d / "flags.json")
    master = build_master(cfg, [1])
    master.page(1).blocks[0].lines = ["ख"]  # a hand correction
    master.save(cfg.master_path)
    FlagReport(page=1).save(d / "flags.json")  # re-validated: clean
    master = build_master(cfg, [1])
    assert master.page(1).blocks[0].lines == ["ख"]
    assert master.page(1).blocks[0].flags == []


def test_corrupt_page_cache_is_skipped_not_fatal(tmp_path):
    cfg = make_cfg(tmp_path)
    write_norm(cfg, 1, "अ")
    write_norm(cfg, 2, "आ")
    (pipeline.page_dir(cfg.work_dir, 1) / pipeline.NORM_JSON).write_text("{corrupt", encoding="utf-8")
    errors: list = []
    master = pipeline.build_master(cfg, [1, 2], errors=errors)
    assert [p.page for p in master.pages] == [2]
    assert errors and errors[0][0] == 1


def test_master_drops_pages_the_input_no_longer_has(tmp_path):
    cfg = make_cfg(tmp_path)
    for p in (1, 2, 3):
        write_norm(cfg, p, "क")
    pipeline.build_master(cfg, [1, 2, 3], num_pages=3)
    master = pipeline.build_master(cfg, [1, 2], num_pages=2)
    assert [p.page for p in master.pages] == [1, 2]
    assert set(master.norm_hashes) == {1, 2}


def test_interrupted_stage_is_not_cached(tmp_path):
    cfg = make_cfg(tmp_path)
    counter = Counter()

    def crash(job):
        job.path("a.txt").write_text("half", encoding="utf-8")
        raise KeyboardInterrupt  # not an Exception: the runner does not catch it, like a kill mid-write

    stages = text_stages(counter)
    run_once(cfg, stages, pages=[1])
    with pytest.raises(KeyboardInterrupt):
        run_once(cfg, [Stage("first", (), ("a.txt",), crash, lambda job: "changed")], pages=[1])
    rec = Manifest.load(cfg.manifest_path).record(1, "first")
    assert rec.status == "pending"  # a later run redoes it instead of trusting the half-written file
    run_once(cfg, stages, pages=[1])
    assert counter.calls.count(("first", 1)) == 2


def test_failed_stage_keeps_what_it_spent(tmp_path):
    cfg = make_cfg(tmp_path)

    def billed_then_failed(job):
        e = RuntimeError("model gave up")
        e.cost_usd = 0.4
        raise e

    run_once(cfg, [Stage("first", (), ("a.txt",), billed_then_failed)], pages=[1])
    m = Manifest.load(cfg.manifest_path)
    assert m.record(1, "first").status == "failed"
    assert m.total_cost() == 0 and m.total_spent() == pytest.approx(0.4)
    run_once(cfg, text_stages(Counter()), pages=[1])  # the successful rerun adds to what was spent
    assert Manifest.load(cfg.manifest_path).total_spent() == pytest.approx(0.9)


def test_keep_stage_reuses_outputs_despite_changed_settings(tmp_path):
    cfg = make_cfg(tmp_path)
    counter = Counter()
    run_once(cfg, text_stages(counter), shared={"doc": "v1"}, pages=[1])
    kept = make_cfg(tmp_path, keep_stages=frozenset({"first"}))
    run_once(kept, text_stages(counter), shared={"doc": "v2"}, pages=[1])
    assert counter.calls.count(("first", 1)) == 1
    run_once(cfg, text_stages(counter), shared={"doc": "v2"}, pages=[1])
    assert counter.calls.count(("first", 1)) == 2


def test_runner_reports_only_completed_pages(tmp_path):
    cfg = make_cfg(tmp_path)
    runner = run_once(cfg, text_stages(Counter(), fail_pages={2}))
    assert sorted(runner.completed) == [1, 3]


def test_validate_fingerprint_follows_image_content(tmp_path):
    cfg = make_cfg(tmp_path)
    job = PageJob(1, pipeline.page_dir(cfg.work_dir, 1), cfg)
    job.page_dir.mkdir(parents=True)
    job.path(pipeline.PRE_PNG).write_bytes(b"scan one")
    a = pipeline._validate_fingerprint(job)
    job.path(pipeline.PRE_PNG).write_bytes(b"scan two")
    assert pipeline._validate_fingerprint(job) != a


def test_normalize_stage_has_a_version_fingerprint():
    stage = next(s for s in pipeline.default_stages("pdf") if s.name == "normalize")
    from vedalipi.normalize import NORMALIZE_VERSION

    assert NORMALIZE_VERSION in stage.fingerprint(None)


def test_output_dir_lock_is_exclusive(tmp_path):
    with pipeline.out_lock(tmp_path):
        with pytest.raises(RuntimeError, match="another vedalipi process"):
            with pipeline.out_lock(tmp_path):
                pass
    with pipeline.out_lock(tmp_path):  # released
        pass


def test_render_rejects_unknown_script_cleanly(tmp_path):
    from vedalipi.cli import main

    with pytest.raises(SystemExit):
        main(["render", str(tmp_path), "--script", "klingon"])


def test_corrections_can_fix_heading_level_and_alignment(tmp_path):
    cfg = make_cfg(tmp_path)
    write_norm(cfg, 1, "श्री")
    master = pipeline.build_master(cfg, [1])
    fixed = pipeline.apply_corrections(master, [Correction(page=1, block_id="b1", level=1, align="center")])
    assert fixed.page(1).block("b1").level == 1 and fixed.page(1).block("b1").align == "center"


def test_cross_read_adds_a_second_ocr_stage():
    names = [s.name for s in pipeline.default_stages("pdf", cross_read=True)]
    assert names == ["ingest", "preprocess", "ocr", "crossread", "normalize", "validate"]
    assert "crossread" not in [s.name for s in pipeline.default_stages("pdf")]
