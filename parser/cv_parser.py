"""
Parse the master CV from DOCX to plain text.
Preserves structure (headings, bullets) as readable plain text.
"""
from pathlib import Path
from docx import Document


def parse_master_cv(cv_path: str) -> str:
    path = Path(cv_path)
    if not path.exists():
        raise FileNotFoundError(f"Master CV not found at: {path.resolve()}")

    doc = Document(str(path))
    lines = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            lines.append("")
            continue

        style = para.style.name.lower()
        if "heading 1" in style:
            lines.append(f"\n# {text}")
        elif "heading 2" in style:
            lines.append(f"\n## {text}")
        elif "heading 3" in style:
            lines.append(f"\n### {text}")
        elif "list" in style or para.paragraph_format.left_indent:
            lines.append(f"- {text}")
        else:
            lines.append(text)

    return "\n".join(lines).strip()
