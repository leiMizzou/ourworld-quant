"""通用 HTML 渲染 helper: 安全 Markdown 子集、金额/百分比格式化、带 data-label 的表格行。

被 server.py 与 learning.py 共用; 本模块只依赖 stdlib, 不得 import 应用内其他模块。
"""
from __future__ import annotations

import re
from html import escape


def markdown_inline(text: str) -> str:
    parts = str(text or "").split("`")
    rendered: list[str] = []
    for idx, part in enumerate(parts):
        if idx % 2:
            rendered.append(f"<code>{escape(part)}</code>")
            continue
        safe = escape(part)
        safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
        rendered.append(safe)
    return "".join(rendered)



def render_markdown(text: str) -> str:
    """Render a small safe Markdown subset produced by the AI coach."""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    html: list[str] = []
    list_type = ""

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            html.append(f"</{list_type}>")
            list_type = ""

    def table_cells(raw: str) -> list[str]:
        return [cell.strip() for cell in raw.strip().strip("|").split("|")]

    def is_table_line(raw: str) -> bool:
        line = raw.strip()
        return line.startswith("|") and line.endswith("|") and "|" in line.strip("|")

    def is_table_delimiter(raw: str) -> bool:
        cells = table_cells(raw)
        return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)

    idx = 0
    while idx < len(lines):
        raw = lines[idx]
        line = raw.strip()
        if not line:
            close_list()
            idx += 1
            continue
        if is_table_line(line) and idx + 1 < len(lines) and is_table_delimiter(lines[idx + 1]):
            close_list()
            headers = table_cells(line)
            idx += 2
            rows: list[list[str]] = []
            while idx < len(lines) and is_table_line(lines[idx].strip()):
                rows.append(table_cells(lines[idx]))
                idx += 1
            col_count = max(1, len(headers))
            head_html = "".join(f"<th>{markdown_inline(cell)}</th>" for cell in headers)
            row_html = ""
            for row in rows:
                normalized = (row + [""] * col_count)[:col_count]
                row_html += labeled_table_row([(headers[pos], markdown_inline(cell)) for pos, cell in enumerate(normalized)])
            html.append(f'<div class="markdown-table"><table class="learning-mobile-table markdown-mobile-table"><thead><tr>{head_html}</tr></thead><tbody>{row_html}</tbody></table></div>')
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            close_list()
            level = 3 if len(heading.group(1)) <= 3 else 4
            html.append(f"<h{level}>{markdown_inline(heading.group(2))}</h{level}>")
            idx += 1
            continue
        quote_match = re.match(r"^>\s*(.+)$", line)
        if quote_match:
            close_list()
            html.append(f"<blockquote>{markdown_inline(quote_match.group(1))}</blockquote>")
            idx += 1
            continue
        if re.fullmatch(r"[-*_]\s*[-*_]\s*[-*_](?:\s*[-*_])*", line):
            close_list()
            html.append("<hr>")
            idx += 1
            continue
        numbered = re.match(r"^\d+[\.)]\s+(.+)$", line)
        if numbered:
            if list_type != "ol":
                close_list()
                html.append("<ol>")
                list_type = "ol"
            html.append(f"<li>{markdown_inline(numbered.group(1))}</li>")
            idx += 1
            continue
        bulleted = re.match(r"^[-*]\s+(.+)$", line)
        if bulleted:
            if list_type != "ul":
                close_list()
                html.append("<ul>")
                list_type = "ul"
            html.append(f"<li>{markdown_inline(bulleted.group(1))}</li>")
            idx += 1
            continue
        close_list()
        html.append(f"<p>{markdown_inline(line)}</p>")
        idx += 1
    close_list()
    return "".join(html) or '<p class="muted">暂无内容</p>'


def money(value: float) -> str:
    return f"{value:,.2f}"



def pct(value: float) -> str:
    if abs(value) < 0.005:
        value = 0.0
    return f"{value:+.2f}%"


def labeled_table_row(cells: list[tuple[str, str]]) -> str:
    return "<tr>" + "".join(f'<td data-label="{escape(label, quote=True)}">{value}</td>' for label, value in cells) + "</tr>"



def labeled_empty_row(message: str, colspan: int, label: str = "状态") -> str:
    return f'<tr><td data-label="{escape(label, quote=True)}" colspan="{colspan}" class="muted">{escape(message)}</td></tr>'
