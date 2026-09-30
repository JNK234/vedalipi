# ABOUTME: Shared driver for the vision-LLM backends: band the page, ask the model per band, parse with retry
# ABOUTME: on malformed JSON, stitch bands into one Page and total the cost. Subclasses only implement _ask().

from __future__ import annotations

import os
import sys
import tempfile
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..schema import Page
from .bands import crop_band, load_gray, plan_bands, stitch
from .base import OcrBackend, OcrResult
from .parse import OcrParseError, parse_blocks
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, retry_prompt, user_prompt


@dataclass
class Answer:
    text: str
    cost_usd: float = 0.0


class OcrCallError(RuntimeError):
    """The model call itself failed (process error, HTTP error, refusal, empty answer).

    `cost_usd` is what was billed before or by the failing call, so the pipeline can record the spend.
    """

    def __init__(self, message: str, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


def _carry_cost(e: Exception, cost: float) -> Exception:
    """Add money already spent on this page to an exception on its way out."""
    e.cost_usd = float(getattr(e, "cost_usd", 0.0) or 0.0) + cost  # type: ignore[attr-defined]
    return e


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v else default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v else default


class BandedLlmBackend(OcrBackend):
    """Reads a page as `bands` overlapping horizontal strips, one model call each.

    Knobs (constructor args, or env VEDALIPI_OCR_BANDS / _OVERLAP / _MAX_WIDTH / _RETRIES):
    bands: strips per page (1 = whole page); overlap: fraction of page height shared across a seam;
    max_width: band images wider than this are downscaled before sending; retries: extra calls per band
    when the answer is not valid block JSON.
    """

    default_model: str = ""

    def __init__(
        self,
        model: str | None = None,
        *,
        bands: int | None = None,
        overlap: float | None = None,
        max_width: int | None = None,
        retries: int | None = None,
    ) -> None:
        super().__init__(model or self.default_model)
        self.bands = bands if bands is not None else _env_int("VEDALIPI_OCR_BANDS", 3)
        self.overlap = overlap if overlap is not None else _env_float("VEDALIPI_OCR_OVERLAP", 0.03)
        self.max_width = max_width if max_width is not None else _env_int("VEDALIPI_OCR_MAX_WIDTH", 2400)
        self.retries = retries if retries is not None else _env_int("VEDALIPI_OCR_RETRIES", 2)

    def fingerprint(self) -> str:
        return f"{self.name}:{self.model}:prompt-{PROMPT_VERSION}:bands{self.bands}:ov{self.overlap}:w{self.max_width}"

    @abstractmethod
    def _ask(self, image_png: Path, system: str, prompt: str) -> Answer:
        """One vision call: the image plus the text prompt under the system prompt. Returns text and USD cost."""

    def read_band(self, image_png: Path, prompt: str, *, label: str) -> tuple[list, float, str]:
        """Call the model for one band, retrying with feedback while the answer is not parseable."""
        cost, raws, ask = 0.0, [], prompt
        for attempt in range(self.retries + 1):
            try:
                ans = self._ask(image_png, SYSTEM_PROMPT, ask)
            except Exception as e:
                raise _carry_cost(e, cost)
            cost += ans.cost_usd
            raws.append(ans.text)
            try:
                return parse_blocks(ans.text), cost, ans.text
            except OcrParseError as e:
                print(f"[ocr] {label}: unusable answer (attempt {attempt + 1}): {e}", file=sys.stderr)
                ask = prompt + "\n\n" + retry_prompt(str(e))
        raise OcrParseError(
            f"{label}: no valid JSON after {self.retries + 1} attempts; last answer: {raws[-1][:300]!r}", cost_usd=cost
        )

    def read_page(self, image: Path, page: int) -> OcrResult:
        gray = load_gray(image)
        bands = plan_bands(gray, self.bands, self.overlap)
        del gray
        results, cost, raws = [], 0.0, []
        with tempfile.TemporaryDirectory(prefix="vedalipi-ocr-") as tmp:
            for band in bands:
                png = crop_band(image, band, Path(tmp) / f"band{band.index}.png", max_width=self.max_width)
                prompt = user_prompt(page, band.index, len(bands), band.top, band.bottom)
                try:
                    blocks, c, raw = self.read_band(png, prompt, label=f"page {page} band {band.index}/{len(bands)}")
                except Exception as e:
                    raise _carry_cost(e, cost)  # earlier bands were billed too
                results.append((band, blocks))
                cost += c
                raws.append(raw)
        return OcrResult(
            page=Page(page=page, blocks=stitch(results)),
            cost_usd=cost,
            model=self.model or "",
            raw="\n\n".join(f"=== band {i} ===\n{r}" for i, r in enumerate(raws, 1)),
        )
