# ABOUTME: Command-line entry point "vedalipi": run, render, report, judge and apply-corrections.
# ABOUTME: Thin argparse layer over pipeline.py; all work and caching happen in the pipeline.

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import pipeline
from .transliterate import supported_scripts
from .schema import Correction, Master


def _cmd_run(a: argparse.Namespace) -> int:
    cfg = pipeline.RunConfig(
        input_path=Path(a.input).expanduser().resolve(),
        out_dir=Path(a.out).expanduser().resolve(),
        script=a.script,
        pages=a.pages,
        jobs=a.jobs,
        backend=a.backend,
        model=a.model,
        force_stages=frozenset(a.force_stage or []),
        keep_stages=frozenset(a.keep_stage or []),
        dpi=a.dpi,
        upscale=a.upscale,
        side_by_side=a.side_by_side,
        cross_read=a.cross_read,
    )
    runner, pdf = pipeline.run(cfg)
    print(f"stages run: {len(runner.ran)}, cached: {len(runner.skipped)}, failed: {len(runner.failed)}")
    print(f"master: {cfg.master_path}")
    if pdf is None:
        print("not rendered: some pages failed (see `vedalipi report`)", file=sys.stderr)
        return 1
    print(f"pdf: {pdf}")
    return 0


def _cmd_render(a: argparse.Namespace) -> int:
    pdf = pipeline.render_from_master(Path(a.dir).expanduser().resolve(), a.script, side_by_side=a.side_by_side)
    print(f"pdf: {pdf}")
    return 0


def _cmd_report(a: argparse.Namespace) -> int:
    print(pipeline.report(Path(a.dir).expanduser().resolve()))
    return 0


def _cmd_judge(a: argparse.Namespace) -> int:
    from . import judge

    report = judge.judge(Path(a.dir).expanduser().resolve(), a.script)
    print(judge.summary(report))
    return 0 if report["passed"] else 1


def _cmd_apply(a: argparse.Namespace) -> int:
    out_dir = Path(a.dir).expanduser().resolve()
    data = json.loads(Path(a.corrections).read_text(encoding="utf-8"))
    corrections = [Correction.model_validate(c) for c in (data if isinstance(data, list) else [data])]
    with pipeline.out_lock(out_dir):
        master = Master.load(out_dir / "master.json")
        pipeline.apply_corrections(master, corrections).save(out_dir / "master.json")
    print(f"applied {len(corrections)} correction(s) to {out_dir / 'master.json'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vedalipi", description="Transliterate a Sanskrit book into another Indic script.")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="OCR/ingest INPUT, build master.json, render the target script")
    r.add_argument("input", help="scanned PDF, folder of page images, or Devanagari .txt/.json")
    r.add_argument("--out", required=True, help="output directory (cache lives in OUT/work)")
    r.add_argument("--script", default="kannada", choices=supported_scripts(), metavar="SCRIPT",
                   help="target script: kannada (default), telugu, or any vidyut script name")
    r.add_argument("--pages", help='page selection, e.g. "1-5,8"')
    r.add_argument("--jobs", type=int, default=4, help="pages processed concurrently")
    r.add_argument("--backend", default="claude-cli", help="OCR backend: claude-cli, anthropic-api, gemini-api")
    r.add_argument("--model", default="claude-opus-5-5", help="OCR model name for the backend")
    r.add_argument("--force-stage", action="append", metavar="STAGE", help="redo a stage (repeatable)")
    r.add_argument("--keep-stage", action="append", metavar="STAGE",
                   help="reuse a stage's existing outputs even if its settings changed, e.g. keep OCR made with "
                        "an older prompt (repeatable)")
    r.add_argument("--dpi", type=int, default=300, help="rasterization DPI for vector PDFs")
    r.add_argument("--upscale", type=float, default=1.5, help="preprocess upscale factor")
    r.add_argument("--side-by-side", action="store_true", help="typeset Devanagari next to the target script")
    r.add_argument("--cross-read", action="store_true",
                   help="OCR every page a second time and flag lines the two reads disagree on (doubles OCR cost)")
    r.set_defaults(func=_cmd_run)

    rd = sub.add_parser("render", help="re-render from DIR/master.json only (no OCR)")
    rd.add_argument("dir")
    rd.add_argument("--script", default="kannada", choices=supported_scripts(), metavar="SCRIPT",
                    help="target script: kannada (default), telugu, or any vidyut script name")
    rd.add_argument("--side-by-side", action="store_true")
    rd.set_defaults(func=_cmd_render)

    rp = sub.add_parser("report", help="stage status, OCR cost and flagged blocks")
    rp.add_argument("dir")
    rp.set_defaults(func=_cmd_report)

    jd = sub.add_parser("judge", help="check that DIR's rendered PDF holds every line, cell and image of master.json")
    jd.add_argument("dir")
    jd.add_argument("--script", default="kannada", choices=supported_scripts(), metavar="SCRIPT",
                    help="script of the PDF to check: kannada (default), telugu, devanagari, ...")
    jd.set_defaults(func=_cmd_judge)

    ac = sub.add_parser("apply-corrections", help="apply a JSON list of corrections to DIR/master.json")
    ac.add_argument("dir")
    ac.add_argument("corrections", help='JSON: [{"page": 3, "block_id": "b2", "lines": ["..."]}]')
    ac.set_defaults(func=_cmd_apply)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as e:  # e.g. another process holds the output directory
        print(f"vedalipi: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
