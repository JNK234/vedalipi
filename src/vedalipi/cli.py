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
        source_script=a.source_script,
        languages=tuple(x.strip() for x in a.languages.split(",") if x.strip()) if a.languages else (),
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
    out_dir = Path(a.dir).expanduser().resolve()
    script = a.script
    if script is None:
        # A translated edition shows Sanskrit in the script it was translated for; otherwise Kannada.
        script = "kannada"
        if a.translation:
            from . import translate
            from .schema import Translation

            tpath = translate.translation_path(out_dir, a.translation)
            if tpath.exists():
                script = Translation.load(tpath).mantra_script
    pdf = pipeline.render_from_master(out_dir, script, side_by_side=a.side_by_side, translation=a.translation)
    print(f"pdf: {pdf}")
    return 0


def _cmd_translate(a: argparse.Namespace) -> int:
    from . import translate

    out_dir = Path(a.dir).expanduser().resolve()
    context = a.context or ""
    if a.context_file:
        context = (context + "\n" + Path(a.context_file).read_text(encoding="utf-8")).strip()
    glossary = json.loads(Path(a.glossary).read_text(encoding="utf-8")) if a.glossary else {}
    if not isinstance(glossary, dict):
        raise SystemExit('--glossary must be a JSON object {"term": "rendering", ...}')
    pages = None
    if a.pages:
        master = Master.load(out_dir / "master.json")
        pages = pipeline.parse_pages(a.pages, max((p.page for p in master.pages), default=0))
    cfg = translate.TranslateConfig(
        out_dir=out_dir, to=a.to, mantra_script=a.mantra_script, gloss=a.gloss, context=context,
        glossary={str(k): str(v) for k, v in glossary.items()}, model=a.model, pages=pages, jobs=a.jobs,
        force=a.force,
    )
    with pipeline.out_lock(out_dir):
        tr, failed = translate.translate_master(cfg)
    print(f"translated pages: {len(tr.pages)}, failed: {len(failed)}")
    print(f"translation: {cfg.path}")
    if failed:
        for p, err in failed:
            print(f"  p{p}: {err[:200]}", file=sys.stderr)
        return 1
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
    r.add_argument("--source-script", default="devanagari", choices=supported_scripts(), metavar="SCRIPT",
                   help="script the book is printed in: devanagari (default), telugu, or any vidyut script name; "
                        "its Sanskrit becomes the Devanagari master")
    r.add_argument("--languages", metavar="CODES",
                   help='languages printed in the book as ISO 639-1 codes, e.g. "te,sa" for Telugu instructions '
                        "around Sanskrit mantras (default: Sanskrit only)")
    r.set_defaults(func=_cmd_run)

    rd = sub.add_parser("render", help="re-render from DIR/master.json only (no OCR)")
    rd.add_argument("dir")
    rd.add_argument("--script", choices=supported_scripts(), metavar="SCRIPT",
                    help="target script: kannada (default), telugu, iast, or any vidyut script name; with "
                         "--translation the default is the script the translation was made for")
    rd.add_argument("--side-by-side", action="store_true")
    rd.add_argument("--translation", metavar="LANG",
                    help="render the translated edition from DIR/translation.LANG.json (see `vedalipi translate`)")

    tr = sub.add_parser("translate", help="translate DIR/master.json into another language (claude CLI)")
    tr.add_argument("dir")
    tr.add_argument("--to", default="en", metavar="LANG", help="target language, ISO 639-1 code (default en)")
    tr.add_argument("--mantra-script", default="iast", choices=supported_scripts(), metavar="SCRIPT",
                    help="script Sanskrit is shown in next to its meaning (default iast)")
    tr.add_argument("--gloss", default="key", choices=["none", "key", "all"],
                    help="word-by-word gloss: none, key (central mantras only, default) or all Sanskrit blocks")
    tr.add_argument("--context", help='free text about the book, e.g. "Smarta, Krishna Yajurveda, follow Sayana"')
    tr.add_argument("--context-file", help="file with more book context, appended to --context")
    tr.add_argument("--glossary", help='JSON object of fixed renderings: {"achamana": "sipping water (achamana)"}')
    tr.add_argument("--model", default="claude-opus-5-5", help="model for the claude CLI")
    tr.add_argument("--pages", help='page selection, e.g. "1-5,8"')
    tr.add_argument("--jobs", type=int, default=4, help="pages translated concurrently")
    tr.add_argument("--force", action="store_true", help="re-translate pages even when unchanged (drops hand edits)")
    tr.set_defaults(func=_cmd_translate)
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
