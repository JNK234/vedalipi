# ABOUTME: OCR backend registry: maps --backend names to backend classes, imported lazily.
# ABOUTME: Each backend lives in its own module under vedalipi.ocr and implements ocr.base.OcrBackend.

from __future__ import annotations

from importlib import import_module

from .base import OcrBackend, OcrResult

BACKENDS: dict[str, str] = {
    "claude-cli": "vedalipi.ocr.claude_cli:ClaudeCliBackend",
    "anthropic-api": "vedalipi.ocr.anthropic_api:AnthropicApiBackend",
    "gemini-api": "vedalipi.ocr.gemini_api:GeminiApiBackend",
    "tesseract": "vedalipi.ocr.tesseract:TesseractBackend",
}


def get_backend(name: str, *, model: str | None = None) -> OcrBackend:
    """Instantiate the backend registered under `name`."""
    if name not in BACKENDS:
        raise ValueError(f"unknown OCR backend {name!r}; choose from {sorted(BACKENDS)}")
    module_name, cls_name = BACKENDS[name].split(":")
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as e:
        if e.name == module_name:
            raise NotImplementedError(f"OCR backend {name!r} is not implemented yet ({module_name})") from e
        raise
    return getattr(module, cls_name)(model=model)


__all__ = ["BACKENDS", "OcrBackend", "OcrResult", "get_backend"]
