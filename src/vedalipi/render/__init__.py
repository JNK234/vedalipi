# ABOUTME: Render stage: lays out a transliterated master as a PDF with Typst (pip "typst"), vendored Noto fonts.
# ABOUTME: Mirrors source structure, marks source page boundaries, embeds image crops and the Devanagari master.

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from ..schema import Master
from .document import build_document
from .fonts import SCRIPTS, script_font
from .textlayer import fix_split_clusters

FONTS_DIR = Path(__file__).resolve().parents[3] / "fonts"
TEMPLATE = Path(__file__).resolve().parent / "book.typ"

__all__ = ["FONTS_DIR", "SCRIPTS", "build_dir_for", "render_pdf", "script_font"]


def build_dir_for(work_dir: Path, script: str, side_by_side: bool) -> Path:
    """Scratch directory holding main.typ, data.json, master.json and image crops for one render."""
    return Path(work_dir) / "render" / f"{script}{'-side-by-side' if side_by_side else ''}"


def render_pdf(
    master: Master,
    target: Master,
    out_pdf: Path,
    *,
    script: str,
    work_dir: Path,
    side_by_side: bool = False,
    fonts_dir: Path = FONTS_DIR,
) -> None:
    """Render `target` (the transliterated master) to out_pdf.

    `master` is the Devanagari original: embedded as a PDF attachment and, with side_by_side,
    typeset next to the target text. Image blocks are cropped by bbox from
    work_dir/pages/pNNNN/pre.png (falling back to source.png). Line height ~1.9; fonts only from fonts_dir.
    """
    import typst

    build = build_dir_for(work_dir, script, side_by_side)
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)

    doc = build_document(master, target, script=script, side_by_side=side_by_side, work_dir=Path(work_dir), build_dir=build)
    (build / "data.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    (build / "master.json").write_text(master.to_json() + "\n", encoding="utf-8")
    shutil.copyfile(TEMPLATE, build / "main.typ")

    out_pdf = Path(out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_pdf.with_name(f".{out_pdf.name}.tmp.pdf")
    # Only vendored fonts: no system fonts and none of Typst's embedded defaults.
    fonts = typst.Fonts(include_system_fonts=False, include_embedded_fonts=False, font_paths=[str(fonts_dir)])
    compiler = typst.Compiler(str(build / "main.typ"), root=str(build), font_paths=fonts, ignore_system_fonts=True)
    _, warnings = compiler.compile_with_warnings(output=str(tmp), format="pdf")
    for w in warnings:
        print(f"typst warning: {w.message}", file=sys.stderr)
    fix_split_clusters(tmp)
    tmp.replace(out_pdf)
