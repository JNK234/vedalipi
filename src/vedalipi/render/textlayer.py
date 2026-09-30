# ABOUTME: Repairs the PDF text layer Typst writes for Indic clusters split across positioned runs (marks, subjoined forms).
# ABOUTME: Each split piece maps to the whole cluster, so copy/search shows it 2-3 times; later pieces get empty ActualText.

from __future__ import annotations

import re
from pathlib import Path

# Typst (via krilla) puts glyphs with a vertical offset (Vedic svaras, below-base forms) in their own text
# run. Glyphs of one cluster inside one run get a correct /ActualText span, but a cluster spread over
# several runs repeats its full text once per run: "ಭ॒" extracts as "ಭ॒ಭ॒". This pass walks each page's
# content stream and gives every repeated piece an empty /ActualText, which extractors honour. Only
# marked-content operators are added; glyphs, positions and the drawing are untouched.

EMPTY_ACTUAL = b"/Span<</ActualText<FEFF>>>BDC "
_DELIMS = b"()<>[]{}/%"
_WS = b" \t\r\n\x0c\x00"


def _tokenize(data: bytes) -> list[tuple[str, bytes]]:
    """Split a content stream into (kind, raw) tokens; kinds: str, hex, name, num, op, [, ], <<, >>."""
    out: list[tuple[str, bytes]] = []
    i, n = 0, len(data)
    while i < n:
        c = data[i : i + 1]
        if c in (b" ", b"\t", b"\r", b"\n", b"\x0c", b"\x00"):
            i += 1
        elif c == b"%":
            j = data.find(b"\n", i)
            i = n if j < 0 else j
        elif c == b"(":
            depth, j = 1, i + 1
            while j < n and depth:
                ch = data[j : j + 1]
                if ch == b"\\":
                    j += 2
                    continue
                depth += (ch == b"(") - (ch == b")")
                j += 1
            out.append(("str", data[i:j]))
            i = j
        elif data.startswith(b"<<", i) or data.startswith(b">>", i):
            out.append((data[i : i + 2].decode(), data[i : i + 2]))
            i += 2
        elif c == b"<":
            j = data.index(b">", i) + 1
            out.append(("hex", data[i:j]))
            i = j
        elif c in (b"[", b"]"):
            out.append((c.decode(), c))
            i += 1
        else:
            j = i + 1 if c == b"/" else i
            while j < n and data[j] not in _WS and data[j] not in _DELIMS:
                j += 1
            raw = data[i:j]
            kind = "name" if c == b"/" else ("num" if re.fullmatch(rb"[+-]?(\d+\.?\d*|\.\d+)", raw) else "op")
            out.append((kind, raw))
            i = max(j, i + 1)
    return out


def _string_bytes(raw: bytes) -> bytes:
    """Decode a PDF literal or hex string token to its bytes."""
    if raw.startswith(b"<"):
        h = re.sub(rb"\s", b"", raw[1:-1])
        return bytes.fromhex((h + b"0" * (len(h) % 2)).decode())
    body, out, i = raw[1:-1], bytearray(), 0
    esc = {b"n": 10, b"r": 13, b"t": 9, b"b": 8, b"f": 12, b"(": 40, b")": 41, b"\\": 92}
    while i < len(body):
        ch = body[i : i + 1]
        if ch != b"\\":
            out += ch
            i += 1
            continue
        nxt = body[i + 1 : i + 2]
        if nxt in esc:
            out.append(esc[nxt])
            i += 2
        elif nxt.isdigit():
            m = re.match(rb"[0-7]{1,3}", body[i + 1 :])
            out.append(int(m.group(0), 8) & 0xFF)
            i += 1 + len(m.group(0))
        elif nxt in (b"\n", b"\r"):
            i += 2
        else:
            out += nxt
            i += 2
    return bytes(out)


def _hex_text(raw: bytes) -> str:
    """Text of a hex string holding UTF-16BE (ActualText values)."""
    b = _string_bytes(raw)
    if b.startswith(b"\xfe\xff"):
        b = b[2:]
    return b.decode("utf-16-be", errors="replace")


def parse_tounicode(cmap: bytes) -> dict[int, str]:
    """Glyph code -> text from a ToUnicode CMap (bfchar and bfrange sections)."""
    out: dict[int, str] = {}
    text = cmap.decode("latin-1")

    def u(h: str) -> str:
        return bytes.fromhex(h).decode("utf-16-be", errors="replace")

    for sec in re.findall(r"beginbfchar(.*?)endbfchar", text, re.DOTALL):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", sec):
            out[int(src, 16)] = u(dst)
    for sec in re.findall(r"beginbfrange(.*?)endbfrange", text, re.DOTALL):
        for lo, hi, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", sec):
            lo_i, hi_i = int(lo, 16), int(hi, 16)
            if dst.startswith("["):
                for k, d in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", dst)):
                    out[lo_i + k] = u(d)
            else:
                base = bytes.fromhex(dst[1:-1])
                for k in range(hi_i - lo_i + 1):
                    last = int.from_bytes(base[-2:], "big") + k
                    out[lo_i + k] = (base[:-2] + last.to_bytes(2, "big")).decode("utf-16-be", errors="replace")
    return out


def _glyphs(raw: bytes) -> list[bytes]:
    """Split a shown string into 2-byte glyph codes (Typst embeds fonts as Identity-H CID fonts)."""
    b = _string_bytes(raw)
    return [b[k : k + 2] for k in range(0, len(b), 2)]


def _lit(codes: list[bytes]) -> bytes:
    return b"<" + b"".join(codes).hex().upper().encode() + b">"


class _Walker:
    """Rewrites one content stream; `last` is the text most recently emitted to an extractor."""

    def __init__(self, fonts: dict[str, dict[int, str]]):
        self.fonts = fonts
        self.cmap: dict[int, str] = {}
        self.last = ""
        self.new_run = True  # a Tm/BT/cm since the last emission: the next glyph starts a new positioned run
        self.stack: list[bool] = []  # BDC nesting; True for /ActualText spans
        self.changed = 0

    def _is_repeat(self, text: str) -> bool:
        return self.new_run and len(text) > 1 and text == self.last

    def _emit(self, text: str) -> None:
        if text:
            self.last = text
            self.new_run = False

    def show(self, op: bytes, operands: list[tuple[str, bytes]]) -> bytes:
        """Handle Tj/TJ; returns replacement bytes for the operator with its operands."""
        items: list[tuple[str, bytes]] = [t for t in operands if t[0] in ("str", "hex", "num")]
        in_actual = any(self.stack)
        pieces: list[tuple[bytes, list[bytes], bool]] = []  # (adjustments before, codes, repeat)
        adj = b""
        for kind, raw in items:
            if kind == "num":
                adj += raw + b" "
                continue
            for code in _glyphs(raw):
                text = self.cmap.get(int.from_bytes(code, "big"), "")
                repeat = not in_actual and self._is_repeat(text)
                if not in_actual and not repeat:
                    self._emit(text)
                if pieces and pieces[-1][2] == repeat and not adj:
                    pieces[-1][1].append(code)
                else:
                    pieces.append((adj, [code], repeat))
                adj = b""
        if not any(p[2] for p in pieces):
            return b""  # unchanged; caller keeps the original bytes
        self.changed += 1
        out = b""
        for before, codes, repeat in pieces:
            tj = b"[" + before + _lit(codes) + b"]TJ"
            out += (EMPTY_ACTUAL + tj + b" EMC ") if repeat else (tj + b" ")
        if adj:
            out += b"[" + adj + b"]TJ "
        return out

    def run(self, data: bytes) -> bytes:
        toks = _tokenize(data)
        out: list[bytes] = []
        operands: list[tuple[str, bytes]] = []
        for kind, raw in toks:
            if kind != "op":
                operands.append((kind, raw))
                continue
            orig = b" ".join(r for _, r in operands) + b" " + raw + b"\n"
            repl = b""
            if raw == b"Tf":
                names = [r for k, r in operands if k == "name"]
                self.cmap = self.fonts.get(names[-1][1:].decode(), {}) if names else {}
            elif raw in (b"Tm", b"BT", b"cm", b"Td", b"TD", b"T*"):
                self.new_run = True
            elif raw == b"BDC":
                body = b" ".join(r for _, r in operands)
                actual = b"/ActualText" in body
                self.stack.append(actual)
                if actual:
                    idx = [i for i, (_, r) in enumerate(operands) if r == b"/ActualText"]
                    val = operands[idx[0] + 1] if idx and idx[0] + 1 < len(operands) else None
                    text = _hex_text(val[1]) if val and val[0] == "hex" else ""
                    if self._is_repeat(text):
                        operands[idx[0] + 1] = ("hex", b"<FEFF>")
                        repl = b" ".join(r for _, r in operands) + b" BDC\n"
                        self.changed += 1
                    else:
                        self._emit(text)
            elif raw == b"EMC":
                if self.stack:
                    self.stack.pop()
            elif raw in (b"Tj", b"TJ", b"'", b'"'):
                if raw in (b"Tj", b"TJ"):
                    repl = self.show(raw, operands)
            out.append(repl or orig)
            operands = []
        if operands:
            out.append(b" ".join(r for _, r in operands) + b"\n")
        return b"".join(out)


def _page_fonts(doc, page) -> dict[str, dict[int, str]]:
    fonts: dict[str, dict[int, str]] = {}
    for f in page.get_fonts(full=True):
        xref, name = f[0], f[4]
        ref = doc.xref_get_key(xref, "ToUnicode")
        if ref[0] == "xref":
            fonts[name] = parse_tounicode(doc.xref_stream(int(ref[1].split()[0])))
    return fonts


def fix_split_clusters(pdf: Path) -> int:
    """Rewrite pdf in place so split clusters extract once; returns the number of repairs made."""
    import pymupdf

    pdf = Path(pdf)
    total = 0
    with pymupdf.open(pdf) as doc:
        for page in doc:
            fonts = _page_fonts(doc, page)
            walker = _Walker(fonts)
            for xref in page.get_contents():
                data = doc.xref_stream(xref)
                new = walker.run(data)
                if walker.changed:
                    doc.update_stream(xref, new)
            total += walker.changed
        if total:
            tmp = pdf.with_name(f".{pdf.name}.fix.pdf")
            doc.save(tmp, garbage=1, deflate=True)
    if total:
        tmp.replace(pdf)
    return total
