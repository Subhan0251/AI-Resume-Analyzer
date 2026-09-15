"""Readable, paginated PDF exports from trusted saved reports using PyMuPDF."""
from html import escape
from io import BytesIO
import pymupdf


def render_pdf(report):
    def text(value):
        return escape(str(value if value is not None else ""))
    card = report.get("scorecard", {})
    required = card.get("required", {})
    coverage = required.get("coverage")
    parts = ["<html><body>", '<div class="hero"><p class="brand">MATCHPOINT / YOUR NEXT CHAPTER</p>',
             '<h1>Requirements<br>coverage report</h1>',
             f'<p class="score">{text(coverage) + "%" if coverage is not None else "N/A"}</p>',
             f'<p>{text(required.get("supported", 0))} of {text(required.get("total", 0))} required criteria supported</p></div>',
             f'<p><b>{text(report.get("filename", "CV comparison"))}</b><br>Analysis date: {text(report.get("analysis_as_of", "Sample"))}</p>']
    if report.get("sample"):
        parts.append('<p class="notice">SAMPLE REPORT - illustrative data, not a personal assessment.</p>')
    parts.extend(['<h2>How to read this report</h2>', f'<p>{text(card.get("method", "Coverage uses documented evidence, not a hiring probability."))}</p>',
                  '<p>This assesses what your CV documents. It does not independently verify competence, credentials, or eligibility. AI interpretations can be incorrect; review the source evidence.</p>'])
    for importance in ("required", "preferred", "unspecified"):
        summary = card.get(importance, {})
        parts.append(f'<h3>{importance.title()} criteria</h3><p>{text(summary.get("supported", 0))} supported / {text(summary.get("total", 0))} total. '
                     f'Partial: {text(summary.get("partially_supported", 0))}; mentioned only: {text(summary.get("mentioned_only", 0))}; '
                     f'not evidenced: {text(summary.get("not_evidenced", 0))}; unclear: {text(summary.get("unclear", 0))}.</p>')
    parts.append('<h2>Evidence insights</h2>')
    for number, row in enumerate(card.get("criteria", []), 1):
        if number > 1:
            parts.append('<!-- block -->')
        parts.append(f'<div class="criterion"><p class="tag">{text(row.get("importance", ""))} / {text(row.get("category", ""))}</p>'
                     f'<h3>{number}. {text(row.get("requirement", ""))}</h3><p><b>{text(row.get("status", "unclear").replace("_", " ").title())}</b></p>'
                     f'<p>{text(row.get("reason", ""))}</p><p class="label">JOB DESCRIPTION - line {text(row.get("jd_evidence", {}).get("line", "?"))}</p>'
                     f'<blockquote>{text(row.get("jd_evidence", {}).get("quote", ""))}</blockquote>')
        for evidence in row.get("cv_evidence", []):
            parts.append(f'<p class="label">CV SOURCE - line {text(evidence.get("line", "?"))} / {text(evidence.get("kind", ""))}</p><blockquote>{text(evidence.get("quote", ""))}</blockquote>')
        if not row.get("cv_evidence"):
            parts.append('<p>No verified CV passage was identified for this criterion.</p>')
        for clause in row.get("unresolved_parts", []):
            parts.append(f'<p><b>Not established:</b> {text(clause)}</p>')
        parts.append('</div>')
    parts.append('<!-- section --><h2>All improvement suggestions</h2>')
    for number, gap in enumerate(report.get("gaps", []), 1):
        if number > 1:
            parts.append('<!-- block -->')
        parts.append(f'<h3>{number}. {text(gap.get("missing_skill", "Requirement"))}</h3><p>{text(gap.get("suggestion", ""))}</p>')
    if not report.get("gaps"):
        parts.append('<p>No gaps were identified in the assessed criteria. This is not a hiring guarantee.</p>')
    parts.append('<!-- section --><h2>Assessment rubric</h2>')
    for status, description in card.get("rubric", {}).items():
        parts.append(f'<p><b>{text(status.replace("_", " ").title())}:</b> {text(description)}</p>')
    parts.append('</body></html>')
    css = """
        body { font-family: sans-serif; font-size: 10pt; color: #20271d; }
        h1 { font-size: 30pt; line-height: 1.1; color: #11170c; margin: 12pt 0; }
        h2 { font-size: 20pt; color: #244400; margin-top: 24pt; page-break-after: avoid; }
        h3 { font-size: 12pt; margin: 12pt 0 6pt; page-break-after: avoid; }
        .brand { color: #426000; font-size: 9pt; }
        .score { color: #244400; font-size: 42pt; margin: 15pt 0; }
        .tag, .label { color: #4c662e; font-size: 8pt; }
        .quote { margin: 8pt 12pt; }
        .notice { color: #426000; padding: 0; }
    """
    # Independent stories prevent cover styles from bleeding across pages and
    # let us move a complete evidence entry to the next page when it fits there.
    html = "".join(parts).replace('<html><body>', '').replace('</body></html>', '')
    html = html.replace('<div class="criterion">', '').replace('<div class="hero">', '').replace('</div>', '')
    html = html.replace('<blockquote>', '<p class="quote">').replace('</blockquote>', '</p>')
    html = html.replace('<h2>Evidence insights</h2>', '<!-- section --><h2>Evidence insights</h2>')
    output = BytesIO()
    writer = pymupdf.DocumentWriter(output)
    device = None
    page_count = 0
    top, bottom = 60, 780
    y = top

    def new_page():
        nonlocal device, page_count, y
        if device is not None:
            writer.end_page()
            device = None
        if page_count >= 200:
            raise ValueError("Report exceeds the 200 page export limit")
        device = writer.begin_page(pymupdf.Rect(0, 0, 595, 842))
        page_count += 1
        y = top

    try:
        for section in html.split('<!-- section -->'):
            new_page()
            for block in section.split('<!-- block -->'):
                if bottom - y < 40:
                    new_page()
                story = pymupdf.Story(f'<html><body>{block}</body></html>', user_css=css)
                more, filled = story.place(pymupdf.Rect(44, y, 551, bottom))
                if (more or pymupdf.Rect(filled).y1 > bottom) and y > top:
                    story.reset()
                    new_page()
                    more, filled = story.place(pymupdf.Rect(44, y, 551, bottom))
                while True:
                    story.draw(device)
                    if not more:
                        y = pymupdf.Rect(filled).y1 + 12
                        if y >= bottom:
                            y = bottom
                        break
                    new_page()
                    more, filled = story.place(pymupdf.Rect(44, top, 551, bottom))
        if device is not None:
            writer.end_page()
            device = None
    finally:
        writer.close()
    with pymupdf.open(stream=output.getvalue(), filetype="pdf") as document:
        for index, page in enumerate(document):
            page.insert_text((44, 30), "MATCHPOINT  /  PRIVATE REPORT", fontsize=8, color=(0.3, 0.4, 0.2))
            page.draw_line((44, 42), (551, 42), color=(0.65, 0.9, 0.15), width=2)
            page.insert_text((44, 812), f"Document evidence, not a hiring prediction.   |   {index + 1} / {len(document)}", fontsize=8, color=(0.4, 0.4, 0.4))
        document.set_metadata({"title": "Matchpoint - Requirements coverage report", "author": "Matchpoint"})
        return document.tobytes(garbage=4, deflate=True)
