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
    table_rows: list[list[str]] = []

    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("```"):
            if table_rows:
                parts.append(_render_table(table_rows))
                table_rows = []
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
            if table_rows:
                parts.append(_render_table(table_rows))
                table_rows = []
            if in_list:
                parts.append("</ul>")
                in_list = False
            continue

        if _is_table_row(stripped):
            cells = _split_table_row(stripped)
            if not _is_table_separator(cells):
                table_rows.append(cells)
            continue
        if table_rows:
            parts.append(_render_table(table_rows))
            table_rows = []

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
    if table_rows:
        parts.append(_render_table(table_rows))
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


def _is_table_row(text: str) -> bool:
    return text.startswith("|") and text.endswith("|") and text.count("|") >= 2


def _split_table_row(text: str) -> list[str]:
    return [cell.strip() for cell in text.strip("|").split("|")]


def _is_table_separator(cells: list[str]) -> bool:
    if not cells:
        return False
    return all(cell and set(cell) <= {"-", ":"} and "-" in cell for cell in cells)


def _render_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header = rows[0]
    body = rows[1:]
    parts = ["<table>", "<thead><tr>"]
    for cell in header:
        parts.append("<th>" + _render_inline(cell) + "</th>")
    parts.append("</tr></thead>")
    if body:
        parts.append("<tbody>")
        for row in body:
            parts.append("<tr>")
            for index in range(len(header)):
                cell = row[index] if index < len(row) else ""
                parts.append("<td>" + _render_inline(cell) + "</td>")
            parts.append("</tr>")
        parts.append("</tbody>")
    parts.append("</table>")
    return "".join(parts)
