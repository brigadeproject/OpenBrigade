from __future__ import annotations

import html
import re

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def render_markdown_html(text: str) -> str:
    lines = text.splitlines()
    parts: list[str] = []
    in_code = False
    code_lines: list[str] = []
    in_list = False

    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("```"):
            if not in_code:
                in_code = True
                code_lines = []
            else:
                parts.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                in_code = False
            continue
        if in_code:
            code_lines.append(raw)
            continue

        if not stripped:
            if in_list:
                parts.append("</ul>")
                in_list = False
            continue

        if stripped.startswith(("- ", "* ")):
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append("<li>" + _render_inline(stripped[2:].strip()) + "</li>")
            continue
        if in_list:
            parts.append("</ul>")
            in_list = False

        if stripped.startswith("### "):
            parts.append("<h3>" + _render_inline(stripped[4:]) + "</h3>")
            continue
        if stripped.startswith("## "):
            parts.append("<h2>" + _render_inline(stripped[3:]) + "</h2>")
            continue
        if stripped.startswith("# "):
            parts.append("<h1>" + _render_inline(stripped[2:]) + "</h1>")
            continue
        if stripped.startswith("> "):
            parts.append("<blockquote>" + _render_inline(stripped[2:]) + "</blockquote>")
            continue
        parts.append("<p>" + _render_inline(stripped) + "</p>")

    if in_list:
        parts.append("</ul>")
    if in_code:
        parts.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
    return "\n".join(parts)


def _render_inline(text: str) -> str:
    escaped = html.escape(text)
    escaped = _LINK.sub(r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', escaped)
    escaped = _INLINE_CODE.sub(r"<code>\1</code>", escaped)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    escaped = _ITALIC.sub(r"<em>\1</em>", escaped)
    return escaped
