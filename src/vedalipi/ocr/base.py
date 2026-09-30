# ABOUTME: OCR backend interface: every backend turns one page image into a Page (block contract) plus its cost.
# ABOUTME: Backends must copy text exactly as printed; the pipeline hashes fingerprint() to decide re-runs.

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..schema import Page


@dataclass
class OcrResult:
    page: Page
    cost_usd: float = 0.0
    model: str = ""
    raw: str = ""


class OcrBackend(ABC):
    """A page reader. Implementations: claude-cli (default), anthropic-api, gemini-api, tesseract (cross-check)."""

    name: str = ""

    def __init__(self, model: str | None = None) -> None:
        self.model = model

    def fingerprint(self) -> str:
        """String that changes whenever output for the same image could change (backend, model, prompt version)."""
        return f"{self.name}:{self.model or ''}"

    @abstractmethod
    def read_page(self, image: Path, page: int) -> OcrResult:
        """OCR one preprocessed page image (1-based page number) into structured blocks."""
