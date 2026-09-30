# ABOUTME: OCR backend on the Anthropic Messages API (ANTHROPIC_API_KEY): one call per band, or a Message Batch
# ABOUTME: for whole books at half price. Cost is computed from token usage and the per-model price table.

from __future__ import annotations

import base64
import os
import sys
import tempfile
import time
from pathlib import Path

import anthropic

from ..schema import Page
from .bands import crop_band, load_gray, plan_bands, stitch
from .base import OcrResult
from .llm import Answer, BandedLlmBackend, OcrCallError
from .parse import OcrParseError, parse_blocks
from .prompt import SYSTEM_PROMPT, user_prompt

# USD per million tokens (input, output). Cache writes bill 1.25x input, cache reads 0.1x input.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
MAX_TOKENS = 16000
# Serialized request bytes per submitted batch (the API caps a batch at 256 MB and 100k requests).
BATCH_BYTES = 180_000_000
BATCH_REQUESTS = 10_000


def usage_cost(model: str, usage, *, discount: float = 1.0) -> float:
    """USD for one response's usage; unknown models cost 0 (reported, not guessed)."""
    price = PRICES.get(model)
    if price is None:
        return 0.0
    p_in, p_out = (p / 1e6 for p in price)
    cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
    return discount * (
        (usage.input_tokens or 0) * p_in + cache_w * p_in * 1.25 + cache_r * p_in * 0.1 + (usage.output_tokens or 0) * p_out
    )


class AnthropicApiBackend(BandedLlmBackend):
    """Env: ANTHROPIC_API_KEY (or any credential the SDK resolves), VEDALIPI_OCR_EFFORT (default "medium")."""

    name = "anthropic-api"
    default_model = "claude-opus-5-5"

    def __init__(self, model: str | None = None, **kw) -> None:
        super().__init__(model, **kw)
        self.effort = os.environ.get("VEDALIPI_OCR_EFFORT", "medium")
        self.client = anthropic.Anthropic()

    def fingerprint(self) -> str:
        return super().fingerprint() + f":effort-{self.effort}"

    def _params(self, image_png: Path, system: str, prompt: str) -> dict:
        data = base64.standard_b64encode(Path(image_png).read_bytes()).decode("ascii")
        return {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            # The system prompt is identical for every band of every page: cache it.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "output_config": {"effort": self.effort},
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
                    {"type": "text", "text": prompt},
                ],
            }],
        }

    @staticmethod
    def _text(message) -> str:
        if message.stop_reason == "refusal":
            raise OcrCallError(f"model refused: {getattr(message.stop_details, 'category', None)}")
        if message.stop_reason == "max_tokens":
            raise OcrCallError("answer hit max_tokens (band too dense? use more bands)")
        return "".join(b.text for b in message.content if b.type == "text")

    def _ask(self, image_png: Path, system: str, prompt: str) -> Answer:
        try:
            # Server-side fallback reruns a declined request on another model inside the same call.
            msg = self.client.beta.messages.create(
                **self._params(image_png, system, prompt),
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIStatusError as e:
            raise OcrCallError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise OcrCallError(f"Anthropic API connection error: {e}") from e
        return Answer(text=self._text(msg), cost_usd=usage_cost(msg.model, msg.usage))

    def read_pages_batch(self, pages: list[tuple[Path, int]], *, poll_seconds: float = 30.0) -> dict[int, OcrResult]:
        """OCR many pages through one Message Batch (50% price, results within 24h).

        Bands whose batch result is missing or unparseable are re-read synchronously with retries.
        Returns {page: OcrResult}. Page images are read one at a time; only band PNGs are held on disk.
        """
        plans: dict[int, list] = {}
        pngs: dict[str, tuple[Path, str]] = {}
        with tempfile.TemporaryDirectory(prefix="vedalipi-batch-") as tmp:
            for image, page in pages:
                bands = plan_bands(load_gray(image), self.bands, self.overlap)
                plans[page] = bands
                for band in bands:
                    cid = f"p{page:05d}-b{band.index:02d}"
                    png = crop_band(image, band, Path(tmp) / f"{cid}.png", max_width=self.max_width)
                    pngs[cid] = (png, user_prompt(page, band.index, len(bands), band.top, band.bottom))
            # Submit in chunks so no single batch (or our memory) holds more than ~BATCH_BYTES of images.
            batch_ids, chunk, size = [], [], 0
            overhead = len(SYSTEM_PROMPT.encode()) + 2000
            last = next(reversed(pngs))
            for cid, (png, prompt) in pngs.items():
                chunk.append((cid, png, prompt))
                # base64 image + repeated system prompt + per-request prompt and JSON framing
                size += png.stat().st_size * 4 // 3 + overhead + len(prompt.encode())
                if size >= BATCH_BYTES or len(chunk) >= BATCH_REQUESTS or cid == last:
                    reqs = [{"custom_id": c, "params": self._params(p, SYSTEM_PROMPT, q)} for c, p, q in chunk]
                    batch = self.client.messages.batches.create(requests=reqs)
                    print(f"[ocr] batch {batch.id}: {len(reqs)} band requests submitted", file=sys.stderr)
                    batch_ids.append(batch.id)
                    chunk, size = [], 0
                    del reqs
            pending = set(batch_ids)
            while pending:
                time.sleep(poll_seconds)
                pending = {b for b in pending if self.client.messages.batches.retrieve(b).processing_status != "ended"}
            results_iter = (r for b in batch_ids for r in self.client.messages.batches.results(b))
            answers: dict[str, tuple[list, float, str]] = {}
            spent: dict[str, float] = {}  # batch charges for answers that could not be used
            for res in results_iter:
                if res.result.type != "succeeded":
                    continue
                msg = res.result.message
                cost = usage_cost(msg.model, msg.usage, discount=0.5)
                spent[res.custom_id] = cost
                try:
                    text = self._text(msg)
                    answers[res.custom_id] = (parse_blocks(text), cost, text)
                    del spent[res.custom_id]
                except (OcrCallError, OcrParseError) as e:
                    print(f"[ocr] {res.custom_id}: {e}; re-reading synchronously", file=sys.stderr)
            out: dict[int, OcrResult] = {}
            for page, bands in plans.items():
                results, cost, raws = [], 0.0, []
                for band in bands:
                    cid = f"p{page:05d}-b{band.index:02d}"
                    if cid in answers:
                        blocks, c, raw = answers[cid]
                    else:
                        png, prompt = pngs[cid]
                        blocks, c, raw = self.read_band(png, prompt, label=cid)
                        c += spent.get(cid, 0.0)
                    results.append((band, blocks))
                    cost += c
                    raws.append(raw)
                out[page] = OcrResult(
                    page=Page(page=page, blocks=stitch(results)), cost_usd=cost, model=self.model or "",
                    raw="\n\n".join(f"=== band {i} ===\n{r}" for i, r in enumerate(raws, 1)),
                )
        return out
