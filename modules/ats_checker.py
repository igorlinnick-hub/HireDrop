"""ATS compliance checker for uploaded resume PDFs.

Analyzes a PDF for formatting issues that prevent ATS (Applicant Tracking
Systems) from parsing the resume correctly. Returns a score (0-100) and a
list of detected issue keys.

Score deductions:
  columns     -30  Multi-column layout (side-by-side content)
  tables      -25  HTML/PDF tables
  images      -20  Embedded images or graphics
  text_boxes  -15  Floating text boxes / overlapping text areas
  special_chars -10 Non-standard symbols (arrows, checkboxes, icons)
"""

import io
import unicodedata

import pdfplumber


_ISSUE_LABELS = {
    "columns": "Multi-column layout detected — ATS systems read left-to-right and may jumble columns",
    "tables": "Tables detected — ATS often can't parse table cells correctly",
    "images": "Images or graphics detected — ATS ignores non-text content",
    "text_boxes": "Floating text boxes or complex layout detected",
    "special_chars": "Special symbols or icons detected — may render as garbage characters in ATS",
}

_DEDUCTIONS = {
    "columns": 30,
    "tables": 25,
    "images": 20,
    "text_boxes": 15,
    "special_chars": 10,
}

_SAFE_UNICODE_CATEGORIES = {"L", "N", "P", "Z", "M"}
_ALLOWED_SYMBOLS = set("•–—…""''")


def _detect_columns(page) -> bool:
    """Heuristic: text chars cluster in 2+ distinct horizontal zones."""
    chars = page.chars
    if not chars:
        return False

    page_width = float(page.width)
    margin = page_width * 0.1

    x_positions = sorted({int(c["x0"]) for c in chars if c["x0"] > margin})
    if len(x_positions) < 4:
        return False

    gaps = [x_positions[i + 1] - x_positions[i] for i in range(len(x_positions) - 1)]
    max_gap = max(gaps)
    return max_gap > page_width * 0.28


def _detect_special_chars(text: str) -> bool:
    """Detect symbols outside standard text use (icons, decorative chars)."""
    count = 0
    for ch in text:
        if ch in _ALLOWED_SYMBOLS or ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if cat[0] not in _SAFE_UNICODE_CATEGORIES:
            count += 1
            if count >= 5:
                return True
    return False


def check_ats_compliance(pdf_bytes: bytes) -> dict:
    """Analyse PDF bytes for ATS issues.

    Returns:
        {
            "score": int (0-100),
            "issues": list[str],           # keys from _ISSUE_LABELS
            "issue_labels": list[str],     # human-readable descriptions
            "page_count": int,
        }
    """
    issues: set[str] = set()
    full_text_parts: list[str] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = len(pdf.pages)

        for page in pdf.pages:
            if page.images:
                issues.add("images")

            tables = page.extract_tables()
            if tables and any(len(t) > 0 for t in tables):
                issues.add("tables")

            if _detect_columns(page):
                issues.add("columns")

            rects = page.rects or []
            if len(rects) > 8:
                issues.add("text_boxes")

            text = page.extract_text() or ""
            full_text_parts.append(text)

    full_text = "\n".join(full_text_parts)
    if _detect_special_chars(full_text):
        issues.add("special_chars")

    score = 100 - sum(_DEDUCTIONS[i] for i in issues)
    score = max(0, score)

    return {
        "score": score,
        "issues": sorted(issues),
        "issue_labels": [_ISSUE_LABELS[i] for i in sorted(issues)],
        "page_count": page_count,
    }
