# ABOUTME: OCR backend on the Google Gemini API (GEMINI_API_KEY) via google-genai, same bands and prompt as the
# ABOUTME: Claude backends; asks for JSON output and prices tokens from VEDALIPI_GEMINI_PRICE (USD per 1M in,out).

from __future__ import annotations

import os
from pathlib import Path

from google import genai
from google.genai import errors, types

from .llm import Answer, BandedLlmBackend, OcrCallError

# Default list price (USD per 1M input, output tokens) used when VEDALIPI_GEMINI_PRICE is not set.
DEFAULT_PRICE = (2.0, 12.0)


class GeminiApiBackend(BandedLlmBackend):
    """Env: GEMINI_API_KEY; VEDALIPI_GEMINI_PRICE="in,out" overrides the per-1M-token price used for cost."""

    name = "gemini-api"
    default_model = "gemini-pro-latest"

    def __init__(self, model: str | None = None, **kw) -> None:
        # The run config defaults --model to a Claude id; that means "backend default" here.
        if model and model.startswith("claude"):
            model = None
        super().__init__(model, **kw)
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise OcrCallError("gemini-api backend needs GEMINI_API_KEY")
        self.client = genai.Client(api_key=key)
        price = os.environ.get("VEDALIPI_GEMINI_PRICE")
        self.price = tuple(float(x) for x in price.split(",")) if price else DEFAULT_PRICE

    def _ask(self, image_png: Path, system: str, prompt: str) -> Answer:
        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=[types.Part.from_bytes(data=Path(image_png).read_bytes(), mime_type="image/png"), prompt],
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    temperature=0.0,
                    max_output_tokens=32000,
                ),
            )
        except errors.APIError as e:
            raise OcrCallError(f"Gemini API error {e.code}: {e.message}") from e
        u = resp.usage_metadata
        cost = 0.0
        if u is not None:
            out_tokens = (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
            cost = ((u.prompt_token_count or 0) * self.price[0] + out_tokens * self.price[1]) / 1e6
        text = resp.text or ""
        if not text.strip():
            reason = resp.candidates[0].finish_reason if resp.candidates else "no candidates"
            raise OcrCallError(f"Gemini returned no text ({reason})")
        return Answer(text=text, cost_usd=cost)
