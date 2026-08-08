import os
from datetime import datetime

from time_utils import fmt_date, fmt_datetime, fmt_time, local_now

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

KUMON_BLUE = colors.HexColor("#00539C")
LIGHT_BLUE = colors.HexColor("#EAF3FB")


def generate_report_pdf(report_date, visits, output_path, center_name="Kumon Center"):
    """
    report_date: a date object for the 24-hour period this report covers.
    visits: list of sqlite3.Row-like dicts with keys:
        name, check_in_time, check_out_time, email_status
    output_path: full path to write the PDF to.
    """
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleBlue", parent=styles["Title"], textColor=KUMON_BLUE, spaceAfter=4
    )
    subtitle_style = ParagraphStyle(
        "Subtitle", parent=styles["Normal"], textColor=colors.grey, spaceAfter=18
    )

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
    )
    story = []

    story.append(Paragraph(f"{center_name} — Check-In / Check-Out Report", title_style))
    story.append(Paragraph(fmt_date(report_date), subtitle_style))

    total = len(visits)
    checked_out = sum(1 for v in visits if v["check_out_time"])
    still_in = total - checked_out
    auto_closed = sum(1 for v in visits if v["email_status"] == "auto_closed")
    emails_sent = sum(1 for v in visits if v["email_status"] == "sent")
    email_issues = sum(1 for v in visits if v["email_status"] in ("failed", "not_configured", "no_address"))

    def fmt_duration(check_in, check_out):
        if not check_out:
            return "—"
        delta = datetime.fromisoformat(check_out) - datetime.fromisoformat(check_in)
        minutes = int(delta.total_seconds() // 60)
        return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"

    # Header cells are Paragraphs so long labels wrap instead of overflowing.
    head_style = ParagraphStyle(
        "SummaryHead", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=8.5, leading=10.5, alignment=1, textColor=colors.white,
    )
    value_style = ParagraphStyle(
        "SummaryValue", parent=styles["Normal"], fontSize=13, leading=15, alignment=1,
    )
    # "Still Checked In" isn't shown: the nightly job closes everyone out before
    # the report is written, so it would always read 0. In the unexpected case
    # that a visit is still open, it's called out in a footnote instead.
    summary_cells = [
        ("Total Visits", total),
        ("Checked Out", checked_out - auto_closed),
        ("Forgot to Check Out", auto_closed),
        ("Parent Emails Sent", emails_sent),
        ("Email Issues", email_issues),
    ]
    summary_data = [
        [Paragraph(label, head_style) for label, _ in summary_cells],
        [Paragraph(str(value), value_style) for _, value in summary_cells],
    ]
    summary_table = Table(summary_data, colWidths=[1.4 * inch] * 5)
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), KUMON_BLUE),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BACKGROUND", (0, 1), (-1, 1), LIGHT_BLUE),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.white),
            ]
        )
    )
    story.append(summary_table)
    story.append(Spacer(1, 24))

    story.append(Paragraph("Visit Detail", styles["Heading2"]))
    story.append(Spacer(1, 6))

    table_data = [["Student", "Check-In", "Check-Out", "Duration", "Parent Email"]]
    status_label = {
        "sent": "Sent",
        "failed": "Failed",
        "not_configured": "Not sent (email not configured)",
        "no_address": "Not sent (no email on file)",
        "auto_closed": "Not sent (forgot to check out)",
        None: "—",
    }
    # Names and status text go in Paragraphs so long values wrap in their column.
    cell_style = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=9, leading=11)
    for v in sorted(visits, key=lambda r: r["check_in_time"]):
        table_data.append(
            [
                Paragraph(v["name"], cell_style),
                fmt_time(v["check_in_time"]),
                fmt_time(v["check_out_time"]),
                fmt_duration(v["check_in_time"], v["check_out_time"]),
                Paragraph(status_label.get(v["email_status"], v["email_status"] or "—"), cell_style),
            ]
        )

    if len(table_data) == 1:
        story.append(Paragraph("No check-ins were recorded in this period.", styles["Normal"]))
    else:
        detail_table = Table(table_data, colWidths=[1.9 * inch, 1.0 * inch, 1.0 * inch, 0.9 * inch, 2.2 * inch], repeatRows=1)
        detail_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), KUMON_BLUE),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_BLUE]),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(detail_table)

    story.append(Spacer(1, 24))
    footer_style = ParagraphStyle("footer", parent=styles["Normal"], textColor=colors.grey, fontSize=8)
    if auto_closed:
        story.append(Paragraph(
            f"{auto_closed} student(s) forgot to check out and were closed out automatically "
            f"at the center's closing time. No email was sent to those parents.",
            footer_style,
        ))
        story.append(Spacer(1, 6))
    if still_in:
        story.append(Paragraph(
            f"{still_in} student(s) were still checked in when this report was written.",
            footer_style,
        ))
        story.append(Spacer(1, 6))
    story.append(Paragraph(f"Generated {fmt_datetime(local_now())}", footer_style))

    doc.build(story)
    return output_path
