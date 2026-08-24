"""Render agent markdown as Telegram HTML messages.

Telegram shows formatting only when ``sendMessage`` carries a ``parse_mode``.
Agent output is ordinary markdown — not Telegram MarkdownV2, whose escaping
rules reject typical agent text outright, and not HTML. This module converts
the common markdown constructs into Telegram's supported HTML subset and
splits long turns into messages that fit the Bot API's 4096-character cap.

Every rendered message keeps its source markdown alongside the HTML so the
sender can fall back to plain text if Telegram rejects the entities.
"""

from __future__ import annotations

import html
import re
from typing import NamedTuple

TELEGRAM_MESSAGE_LIMIT = 4096
# Pack messages up to this length, leaving headroom under the hard cap.
_TARGET_LENGTH = 4000
# Pathological single lines are chopped to this many source characters so that
# even worst-case HTML escaping (5x inflation) stays under the cap.
_MAX_SOURCE_LINE = 800

_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_ITALIC_STAR = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_ITALIC_UNDER = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_LANGUAGE = re.compile(r"[^\w+-]")


class RenderedMessage(NamedTuple):
    html: str
    plain: str


class _Block(NamedTuple):
    kind: str  # "text" | "code" | "table"
    text: str
    language: str = ""


def _split_blocks(markdown: str) -> list[_Block]:
    """Split markdown into paragraph, fenced-code, and table blocks."""
    blocks: list[_Block] = []
    text_lines: list[str] = []
    table_lines: list[str] = []
    code_lines: list[str] | None = None
    code_language = ""

    def flush_text() -> None:
        if any(line.strip() for line in text_lines):
            blocks.append(_Block("text", "\n".join(text_lines).strip("\n")))
        text_lines.clear()

    def flush_table() -> None:
        if len(table_lines) >= 2:
            blocks.append(_Block("table", "\n".join(table_lines)))
        else:
            text_lines.extend(table_lines)
        table_lines.clear()

    for line in markdown.split("\n"):
        if code_lines is not None:
            if line.strip().startswith("```"):
                blocks.append(_Block("code", "\n".join(code_lines), code_language))
                code_lines = None
            else:
                code_lines.append(line)
            continue
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_table()
            flush_text()
            code_language = _LANGUAGE.sub("", stripped[3:].strip())
            code_lines = []
            continue
        if stripped.startswith("|"):
            table_lines.append(line)
            continue
        flush_table()
        if not stripped:
            flush_text()
        else:
            text_lines.append(line)
    if code_lines is not None:  # unclosed fence
        blocks.append(_Block("code", "\n".join(code_lines), code_language))
    flush_table()
    flush_text()
    return blocks


def _render_text(block_text: str) -> str:
    escaped = html.escape(block_text, quote=False)
    code_spans: list[str] = []

    def stash_code(match: re.Match) -> str:
        code_spans.append(match.group(1))
        return f"\x00{len(code_spans) - 1}\x00"

    working = _INLINE_CODE.sub(stash_code, escaped)
    working = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', working)
    working = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", working)
    working = _STRIKE.sub(r"<s>\1</s>", working)
    working = _ITALIC_STAR.sub(r"<i>\1</i>", working)
    working = _ITALIC_UNDER.sub(r"<i>\1</i>", working)
    lines = [
        f"<b>{heading.group(1)}</b>" if (heading := _HEADING.match(line)) else line
        for line in working.split("\n")
    ]
    working = "\n".join(lines)
    return re.sub(r"\x00(\d+)\x00",
                  lambda m: f"<code>{code_spans[int(m.group(1))]}</code>", working)


def _render_block(block: _Block) -> str:
    if block.kind == "text":
        return _render_text(block.text)
    escaped = html.escape(block.text, quote=False)
    if block.kind == "code" and block.language:
        return f'<pre><code class="language-{block.language}">{escaped}</code></pre>'
    return f"<pre>{escaped}</pre>"


def _block_source(block: _Block) -> str:
    if block.kind == "code":
        return f"```{block.language}\n{block.text}\n```"
    return block.text


def _chop_long_lines(lines: list[str]) -> list[str]:
    chopped: list[str] = []
    for line in lines:
        while len(line) > _MAX_SOURCE_LINE:
            chopped.append(line[:_MAX_SOURCE_LINE])
            line = line[_MAX_SOURCE_LINE:]
        chopped.append(line)
    return chopped


def _split_oversized(block: _Block) -> list[tuple[str, str]]:
    """Split one block whose rendering exceeds the target into several."""
    pieces: list[tuple[str, str]] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            sub_block = _Block(block.kind, "\n".join(current), block.language)
            pieces.append((_render_block(sub_block), _block_source(sub_block)))

    for line in _chop_long_lines(block.text.split("\n")):
        tentative = _Block(block.kind, "\n".join(current + [line]), block.language)
        if current and len(_render_block(tentative)) > _TARGET_LENGTH:
            flush()
            current = [line]
        else:
            current.append(line)
    flush()
    return pieces


def render_messages(markdown: str) -> list[RenderedMessage]:
    """Render a full agent turn into one or more sendable messages."""
    text = markdown.strip()
    if not text:
        return []
    pieces: list[tuple[str, str]] = []
    for block in _split_blocks(text):
        rendered = _render_block(block)
        if len(rendered) <= _TARGET_LENGTH:
            pieces.append((rendered, _block_source(block)))
        else:
            pieces.extend(_split_oversized(block))

    messages: list[RenderedMessage] = []
    html_parts: list[str] = []
    plain_parts: list[str] = []

    def flush() -> None:
        if html_parts:
            messages.append(RenderedMessage("\n\n".join(html_parts),
                                            "\n\n".join(plain_parts)))
            html_parts.clear()
            plain_parts.clear()

    length = 0
    for rendered, source in pieces:
        added = len(rendered) + (2 if html_parts else 0)
        if html_parts and length + added > _TARGET_LENGTH:
            flush()
            length = 0
            added = len(rendered)
        html_parts.append(rendered)
        plain_parts.append(source)
        length += added
    flush()
    return messages
