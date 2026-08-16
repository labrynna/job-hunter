"""
Render tailored CV and cover letter markdown to PDF via Playwright.

Each document is rendered separately (two files per application) and each
uses its own stylesheet — a CV reads as a structured document with section
rules and bullet lists; a cover letter reads as plain business-letter prose.
"""
import re
from pathlib import Path
import markdown2
from bs4 import BeautifulSoup


def _fix_lazy_lists(md: str) -> str:
    """
    markdown2 (like most Markdown parsers) only starts a new list if a blank
    line precedes the first '-'/'*' item. Content generated inline (e.g. an
    LLM writing "Role – Company | Dates" immediately followed by bullets with
    no blank line between) triggers "lazy paragraph continuation" instead —
    the bullets get swallowed into the preceding paragraph as literal hyphens.
    Insert the missing blank line automatically so this can't recur.
    """
    lines = md.split("\n")
    out = []
    for i, line in enumerate(lines):
        is_bullet = re.match(r"^(\s*)[-*]\s", line) is not None
        prev_nonblank = out and out[-1].strip() != ""
        prev_is_bullet = out and re.match(r"^(\s*)[-*]\s", out[-1]) is not None
        if is_bullet and prev_nonblank and not prev_is_bullet:
            out.append("")
        out.append(line)
    return "\n".join(out)


_CV_CSS = """
@page { margin: 0; }
body {
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.4;
    color: #1a1a1a;
    margin: 1.4cm 2cm;
}
h1 {
    font-size: 22pt;
    font-weight: 700;
    margin: 0 0 4px 0;
    color: #16324f;
    letter-spacing: 0.2px;
}
h1 + p {
    margin-top: 0;
    font-size: 9.5pt;
    color: #444;
}
h2 {
    font-size: 11.5pt;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: #16324f;
    border-bottom: 1.5px solid #16324f;
    margin-top: 13px;
    margin-bottom: 6px;
    padding-bottom: 2px;
}
h3 {
    font-size: 10.5pt;
    font-weight: 700;
    margin-bottom: 0px;
    margin-top: 8px;
    color: #111;
}
h3 + p.role-meta {
    margin-top: 1px;
    margin-bottom: 3px;
    font-size: 9.8pt;
    color: #333;
}
h3 + p.role-meta em {
    font-style: italic;
    font-weight: 400;
}
h3 + p.role-meta .role-date {
    font-style: normal;
    color: #333;
}
p { margin: 3px 0; }
ul { margin: 2px 0 6px 0; padding-left: 18px; }
li { margin: 1px 0; padding-left: 2px; }
strong { font-weight: 700; }
em { font-style: italic; }
hr { border: none; border-top: 1px solid #ccc; margin: 8px 0; }
"""

_COVER_LETTER_CSS = """
@page { margin: 0; }
body {
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.5;
    color: #1a1a1a;
    margin: 2cm 2.8cm;
}
h1 {
    font-size: 16pt;
    font-weight: 700;
    margin: 0 0 4px 0;
    color: #16324f;
}
h1 + p {
    margin-top: 0;
    font-size: 9.5pt;
    color: #444;
    margin-bottom: 18px;
}
h2 {
    font-size: 12pt;
    font-weight: 600;
    color: #16324f;
    margin-top: 4px;
    margin-bottom: 18px;
}
p { margin: 0 0 11px 0; text-align: left; }
strong { font-weight: 700; }
em { font-style: italic; }
"""


def _format_role_meta_lines(body_html: str) -> str:
    """
    Enforce a consistent look for job entries regardless of exactly how the
    source Markdown phrased the meta line: job title bold (h3), then on its
    own line the company/location in italics with the date next to it in
    normal weight — instead of trusting freeform Markdown to come out the
    same way every time. Expects the convention '### Title' immediately
    followed by 'Company – Location | Date range'; the pipe splits the two
    styled halves. If a paragraph after an h3 doesn't match that shape, it's
    left untouched rather than mangled.
    """
    soup = BeautifulSoup(body_html, "html.parser")
    for h3 in soup.find_all("h3"):
        sibling = h3.find_next_sibling()
        if sibling is None or sibling.name != "p":
            continue
        text = sibling.get_text()
        if "|" not in text:
            continue
        left, _, right = text.partition("|")
        left, right = left.strip(), right.strip()
        if not left or not right:
            continue
        sibling.clear()
        sibling["class"] = sibling.get("class", []) + ["role-meta"]
        em = soup.new_tag("em")
        em.string = left
        sibling.append(em)
        sibling.append(" ")
        date_span = soup.new_tag("span", **{"class": "role-date"})
        date_span.string = right
        sibling.append(date_span)
    return str(soup)


def _md_to_html(md: str, css: str, format_role_meta: bool = False) -> str:
    md = _fix_lazy_lists(md)
    body = markdown2.markdown(md, extras=["fenced-code-blocks", "tables", "strike"])
    if format_role_meta:
        body = _format_role_meta_lines(body)
    return f"<html><head><meta charset='utf-8'><style>{css}</style></head><body>{body}</body></html>"


async def _render_pdf(html: str, output_path: Path):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html, wait_until="domcontentloaded")
        await page.pdf(path=str(output_path), format="A4", margin={
            "top": "0", "bottom": "0", "left": "0", "right": "0"
        }, print_background=True)
        await browser.close()


async def render_cv(markdown: str, output_dir: str, filename_stem: str) -> str:
    """Render CV markdown to PDF. Returns the absolute path as a string."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    pdf_path = out / f"{filename_stem}_CV.pdf"
    html = _md_to_html(markdown, _CV_CSS, format_role_meta=True)
    await _render_pdf(html, pdf_path)
    return str(pdf_path)


async def render_cover_letter(markdown: str, output_dir: str, filename_stem: str) -> str:
    """Render cover letter markdown to PDF. Returns the absolute path as a string."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    pdf_path = out / f"{filename_stem}_CoverLetter.pdf"
    html = _md_to_html(markdown, _COVER_LETTER_CSS)
    await _render_pdf(html, pdf_path)
    return str(pdf_path)
