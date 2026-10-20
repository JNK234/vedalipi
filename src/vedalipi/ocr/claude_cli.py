# ABOUTME: Default OCR backend: runs the local `claude` CLI headless on the user's subscription (no API key),
# ABOUTME: fully isolated from user MCP servers/plugins/hooks, sending each band image inline via stream-json.

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

from .llm import Answer, BandedLlmBackend, OcrCallError


class ClaudeCliBackend(BandedLlmBackend):
    """OCR through `claude -p`. Env: VEDALIPI_CLAUDE_BIN (default "claude"), VEDALIPI_CLAUDE_TIMEOUT (s)."""

    name = "claude-cli"
    default_model = "claude-opus-5-5"

    def __init__(self, model: str | None = None, **kw) -> None:
        super().__init__(model, **kw)
        self.binary = os.environ.get("VEDALIPI_CLAUDE_BIN", "claude")
        self.timeout = float(os.environ.get("VEDALIPI_CLAUDE_TIMEOUT", "900"))

    def command(self, system: str) -> list[str]:
        # Never --bare (it breaks subscription login). Everything else that could pull the user's
        # tools, MCP servers, settings, hooks or slash commands into the context is switched off.
        return [
            self.binary, "-p",
            "--model", self.model,
            "--tools", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt", system,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
        ]

    @staticmethod
    def message(image_png: Path | None, prompt: str) -> str:
        content: list[dict] = []
        if image_png is not None:
            data = base64.standard_b64encode(Path(image_png).read_bytes()).decode("ascii")
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
        content.append({"type": "text", "text": prompt})
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        return json.dumps(msg, ensure_ascii=False) + "\n"

    @staticmethod
    def parse_stream(stdout: str) -> Answer:
        """Pick the type=="result" event out of the stream-json output."""
        result = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                result = ev
        if result is None:
            raise OcrCallError(f"claude CLI produced no result event: {stdout[-500:]!r}")
        cost = float(result.get("total_cost_usd") or 0.0)
        if result.get("is_error") or result.get("subtype", "success") != "success":
            raise OcrCallError(f"claude CLI error ({result.get('subtype')}): {str(result.get('result'))[:500]}", cost)
        text = result.get("result") or ""
        if not text.strip():
            raise OcrCallError("claude CLI returned an empty answer", cost)
        return Answer(text=text, cost_usd=cost)

    def _ask(self, image_png: Path, system: str, prompt: str) -> Answer:
        return self._call(image_png, system, prompt, cwd=Path(image_png).parent)

    def ask_text(self, system: str, prompt: str) -> Answer:
        """One text-only call (no image) under `system`; used by the translate stage."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="vedalipi-text-") as tmp:
            return self._call(None, system, prompt, cwd=Path(tmp))

    def _call(self, image_png: Path | None, system: str, prompt: str, *, cwd: Path) -> Answer:
        # Drop any API key so the CLI bills the logged-in subscription, as intended.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        try:
            proc = subprocess.run(
                self.command(system), input=self.message(image_png, prompt), capture_output=True,
                text=True, encoding="utf-8", timeout=self.timeout, env=env, cwd=cwd,
            )
        except subprocess.TimeoutExpired as e:
            raise OcrCallError(f"claude CLI timed out after {self.timeout:.0f}s") from e
        except FileNotFoundError as e:
            raise OcrCallError(f"claude CLI not found ({self.binary}); install Claude Code or set VEDALIPI_CLAUDE_BIN") from e
        if proc.returncode != 0 and '"type":"result"' not in proc.stdout.replace(" ", ""):
            raise OcrCallError(f"claude CLI exit {proc.returncode}: {(proc.stderr or proc.stdout)[-800:]}")
        return self.parse_stream(proc.stdout)
