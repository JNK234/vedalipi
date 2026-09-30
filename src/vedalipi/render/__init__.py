# ABOUTME: Render stage: lays out a transliterated master as a PDF with Typst (pip "typst"), vendored Noto fonts.
# ABOUTME: Mirrors source structure, marks source page boundaries, embeds image crops and the Devanagari master.

from __future__ import annotations

from pathlib import Path

from ..schema import Master

FONTS_DIR = Path(__file__).resolve().parents[3] / "fonts"


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
    raise NotImplementedError
