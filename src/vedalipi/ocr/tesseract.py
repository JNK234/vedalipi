# ABOUTME: Optional second reader: local tesseract (e.g. Shreeshrii's Vedic DevaLayer tessdata) for cross-checking
# ABOUTME: LLM output. No layout analysis: each text paragraph becomes one flagged prose block. Free, offline.

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import unicodedata
from pathlib import Path

from ..schema import FLAG_LOW_CONFIDENCE, Block, Page
from .base import OcrBackend, OcrResult
from .llm import OcrCallError


class TesseractBackend(OcrBackend):
    """Env: VEDALIPI_TESSDATA (tessdata dir holding the model), VEDALIPI_TESS_PSM (default 4).

    `model` is the tesseract language/traineddata name (default "DevaLayer-201017", the Vedic accent model;
    use "san" for the stock Sanskrit model).
    """

    name = "tesseract"

    def __init__(self, model: str | None = None) -> None:
        # The run config defaults --model to a Claude id; that means "backend default" here.
        if model and model.startswith("claude"):
            model = None
        super().__init__(model or "DevaLayer-201017")
        self.tessdata = os.environ.get("VEDALIPI_TESSDATA")
        self.psm = os.environ.get("VEDALIPI_TESS_PSM", "4")

    def traineddata(self) -> Path | None:
        """The .traineddata file tesseract will load for self.model, or None if it cannot be located."""
        if self.tessdata:
            base = Path(self.tessdata)
        else:
            try:
                proc = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=60)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return None
            m = re.search(r'"([^"]+)"', proc.stdout + proc.stderr)
            if not m:
                return None
            base = Path(m.group(1))
        path = base / f"{self.model}.traineddata"
        return path if path.is_file() else None

    def fingerprint(self) -> str:
        # The model file itself, not just its name: replacing it under the same name must redo OCR.
        path = self.traineddata()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path else "missing"
        return f"{self.name}:{self.model}:{digest}:psm{self.psm}"

    def read_page(self, image: Path, page: int) -> OcrResult:
        image = Path(image).resolve()
        # Run beside the image with a relative name: some sandboxes block tesseract from opening absolute paths.
        cmd = ["tesseract", image.name, "-", "-l", self.model, "--psm", self.psm]
        if self.tessdata:
            cmd += ["--tessdata-dir", self.tessdata]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=600, cwd=image.parent)
        except FileNotFoundError as e:
            raise OcrCallError("tesseract not installed (brew install tesseract)") from e
        if proc.returncode != 0:
            raise OcrCallError(f"tesseract failed: {proc.stderr[-500:]}")
        text = unicodedata.normalize("NFC", proc.stdout).replace("\f", "")
        blocks: list[Block] = []
        for para in text.split("\n\n"):
            lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
            if lines:
                blocks.append(Block(id=f"b{len(blocks) + 1}", type="prose", lang="sa", lines=lines,
                                    flags=[FLAG_LOW_CONFIDENCE]))
        return OcrResult(page=Page(page=page, blocks=blocks), cost_usd=0.0, model=self.model, raw=proc.stdout)
