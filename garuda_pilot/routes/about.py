"""About / Help route — explains how garuda-pilot works."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request

router = APIRouter()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_README_PATH = _PROJECT_ROOT / "README.md"


def _render_markdown_to_html(md: str) -> str:
    """Minimal Markdown-to-HTML converter for the README.

    Handles: headings, paragraphs, code blocks (fenced), inline code,
    bold, italic, links, unordered lists, ordered lists, tables,
    and horizontal rules. No external dependencies.
    """
    lines = md.split("\n")
    html_parts: list[str] = []
    in_code_block = False
    in_list = False
    in_ol = False
    in_table = False
    i = 0

    while i < len(lines):
        line = lines[i]

        # Fenced code blocks
        if line.strip().startswith("```"):
            if in_code_block:
                html_parts.append("</code></pre>")
                in_code_block = False
            else:
                lang = line.strip()[3:].strip()
                cls = f' class="lang-{lang}"' if lang else ""
                html_parts.append(f"<pre><code{cls}>")
                in_code_block = True
            i += 1
            continue

        if in_code_block:
            escaped = (line.replace("&", "&amp;")
                          .replace("<", "&lt;")
                          .replace(">", "&gt;"))
            html_parts.append(escaped)
            i += 1
            continue

        stripped = line.strip()

        # Close list if we're no longer in one
        if in_list and not stripped.startswith("- ") and not stripped.startswith("* "):
            html_parts.append("</ul>")
            in_list = False
        if in_ol and not re.match(r"^\d+\.\s", stripped):
            html_parts.append("</ol>")
            in_ol = False

        # Table detection
        if "|" in stripped and not in_table:
            # Check if next line is a separator
            if i + 1 < len(lines) and re.match(r"^\|[-\s|:]+\|$", lines[i + 1].strip()):
                in_table = True
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                html_parts.append("<table><thead><tr>")
                for cell in cells:
                    html_parts.append(f"<th>{_inline(cell)}</th>")
                html_parts.append("</tr></thead><tbody>")
                i += 2  # skip header + separator
                continue

        if in_table:
            if "|" in stripped and stripped.startswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                html_parts.append("<tr>")
                for cell in cells:
                    html_parts.append(f"<td>{_inline(cell)}</td>")
                html_parts.append("</tr>")
                i += 1
                continue
            else:
                html_parts.append("</tbody></table>")
                in_table = False

        # Empty line
        if not stripped:
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^[-*_]{3,}$", stripped):
            html_parts.append("<hr>")
            i += 1
            continue

        # Headings
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            level = len(m.group(1))
            text = _inline(m.group(2))
            html_parts.append(f"<h{level}>{text}</h{level}>")
            i += 1
            continue

        # Unordered list
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            text = _inline(stripped[2:])
            html_parts.append(f"<li>{text}</li>")
            i += 1
            continue

        # Ordered list
        m = re.match(r"^(\d+)\.\s+(.*)", stripped)
        if m:
            if not in_ol:
                html_parts.append("<ol>")
                in_ol = True
            text = _inline(m.group(2))
            html_parts.append(f"<li>{text}</li>")
            i += 1
            continue

        # Paragraph
        html_parts.append(f"<p>{_inline(stripped)}</p>")
        i += 1

    # Close any open elements
    if in_list:
        html_parts.append("</ul>")
    if in_ol:
        html_parts.append("</ol>")
    if in_table:
        html_parts.append("</tbody></table>")
    if in_code_block:
        html_parts.append("</code></pre>")

    return "\n".join(html_parts)


def _inline(text: str) -> str:
    """Convert inline markdown: bold, italic, code, links."""
    # Escape HTML entities (but preserve already-valid tags)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # Italic
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    # Links
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank">\1</a>', text)
    return text


def _load_readme() -> str:
    """Load and render README.md as HTML."""
    if _README_PATH.exists():
        md = _README_PATH.read_text(encoding="utf-8")
        return _render_markdown_to_html(md)
    return "<p>README.md not found.</p>"


@router.get("/about")
async def about_page(request: Request):
    templates = request.app.state.templates

    readme_html = _load_readme()

    return templates.TemplateResponse("about.html", {
        "request": request,
        "active_page": "about",
        "readme_html": readme_html,
    })
