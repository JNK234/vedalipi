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


def get_backend(
    name: str, *, model: str | None = None, source_script: str = "devanagari", languages: tuple[str, ...] = ()
) -> OcrBackend:
    """Instantiate the backend registered under `name`, reading books printed in `source_script`
    whose text is in `languages` (ISO 639-1 codes; empty means Sanskrit only)."""
    if name not in BACKENDS:
        raise ValueError(f"unknown OCR backend {name!r}; choose from {sorted(BACKENDS)}")
    module_name, cls_name = BACKENDS[name].split(":")
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as e:
        if e.name == module_name:
            raise NotImplementedError(f"OCR backend {name!r} is not implemented yet ({module_name})") from e
        raise
    backend = getattr(module, cls_name)(model=model)
    if source_script != "devanagari" or languages:
        if not hasattr(backend, "source_script"):
            raise ValueError(f"OCR backend {name!r} reads Sanskrit-only Devanagari books, not {source_script!r}")
        backend.source_script, backend.languages = source_script, tuple(languages)
    return backend


__all__ = ["BACKENDS", "OcrBackend", "OcrResult", "get_backend"]
