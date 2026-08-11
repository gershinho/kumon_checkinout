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


def safe(text) -> str:
    """Make a value safe to put inside a reportlab Paragraph.

    Paragraph parses reportlab's own mini-HTML, and student names go into one.
    A name containing "<b" raised ValueError from deep inside the parser, which
    took down the WHOLE day's report - the dashboard download, the email button,
    and the nightly cron all 500'd, for one name. An unrecognised tag was
    quieter and worse: "Lee <foo>" built fine and silently rendered as "Lee".

    Escaping rather than stripping, so a name is always shown exactly as the
    instructor typed it. & must be replaced first or it would double-escape the
    entities introduced after it.
    """
    return (str(text if text is not None else "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def generate_report_pdf(report_date, visits, output_path, center_name="Kumon Center"):
    """
    report_date: a date object for the 24-hour period this report covers.
    visits: list of row-like dicts with keys:
        name, check_in_time, work_done_time, check_out_time, email_status,
        auto_closed
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

    story.append(Paragraph(f"{safe(center_name)} — Check-In / Check-Out Report", title_style))
    story.append(Paragraph(fmt_date(report_date), subtitle_style))

    total = len(visits)
    checked_out = sum(1 for v in visits if v["check_out_time"])
    still_in = total - checked_out
    auto_closed = sum(1 for v in visits if v["auto_closed"])
    finished = sum(1 for v in visits if v["work_done_time"])
    emails_sent = sum(1 for v in visits if v["email_status"] == "sent")
    email_issues = sum(1 for v in visits
                       if v["email_status"] in ("failed", "partial", "not_configured", "no_address"))
    # Counts the footnote can state without contradicting the tiles above it.
    # "Never told us they finished AND was never emailed" is the only group the
    # "nobody emailed their parent" sentence is true of. Visits from before the
    # three-step flow existed have no work_done_time but were emailed at
    # check-out, and the old wording called those parents un-emailed on the same
    # page that counted them under Parent Emails Sent.
    never_finished_unemailed = sum(1 for v in visits
                                   if not v["work_done_time"] and not v["email_status"])
    pre_upgrade = sum(1 for v in visits if not v["work_done_time"] and v["email_status"])

    def fmt_duration(check_in, check_out):
        if not check_in or not check_out:
            return "—"
        try:
            delta = datetime.fromisoformat(check_out) - datetime.fromisoformat(check_in)
        except (TypeError, ValueError):
            return "—"
        minutes = int(delta.total_seconds() // 60)
        # A clock change or a hand-corrected row can put check-out before
        # check-in. That printed "-90m", which is not a duration and which also
        # skipped the hours branch, so it was wrong and oddly formatted at once.
        if minutes < 0:
            return "—"
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
    # Same words as the table below. Two names for one step in a single report
    # reads as two different things being counted.
    summary_cells = [
        ("Total Visits", total),
        ("Work Completed", finished),
        ("Parent Checked Out", checked_out - auto_closed),
        ("Not Checked Out", auto_closed),
        ("Parent Emails Sent", emails_sent),
        ("Email Issues", email_issues),
    ]
    summary_data = [
        [Paragraph(label, head_style) for label, _ in summary_cells],
        [Paragraph(str(value), value_style) for _, value in summary_cells],
    ]
    summary_table = Table(summary_data, colWidths=[7.0 / len(summary_cells) * inch] * len(summary_cells))
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

    # Headers are Paragraphs, not plain strings, for the same reason the summary
    # table's are: a plain string in a reportlab cell does not wrap, it just
    # runs over the top of the next column. These labels name the three steps in
    # full - "Parent Checked In" rather than "Checked In" - so they no longer
    # fit on one line, and a header that silently overprints its neighbour is a
    # worse outcome than one that takes two lines.
    detail_head_style = ParagraphStyle(
        "DetailHead", parent=styles["Normal"], fontName="Helvetica-Bold",
        fontSize=9, leading=11, textColor=colors.white,
    )
    headers = ["Student", "Parent Checked In", "Work Completed", "Parent Checked Out",
               "Duration", "Parent Email"]
    table_data = [[Paragraph(h, detail_head_style) for h in headers]]
    status_label = {
        "sent": "Sent",
        "partial": "Partly sent (one address refused)",
        "failed": "Failed",
        "not_configured": "Not sent (email not configured)",
        "no_address": "Not sent (no email on file)",
        None: "—",
    }
    # Names and status text go in Paragraphs so long values wrap in their column.
    cell_style = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=9, leading=11)
    for v in sorted(visits, key=lambda r: r["check_in_time"]):
        # A blank email status on a visit that never reached "done with work" is
        # expected rather than a problem: that is the tap that sends the email.
        email_text = status_label.get(v["email_status"], v["email_status"] or "—")
        if not v["email_status"]:
            # A finished visit with no recorded status is not the same as one
            # that never reached that step. It means the send was interrupted -
            # nobody knows whether the parent got it - and a bare em dash reads
            # as "not applicable" rather than "worth looking into".
            email_text = ("Not sent (never marked done)" if not v["work_done_time"]
                          else "Unknown — send was interrupted")
        table_data.append(
            [
                Paragraph(safe(v["name"]), cell_style),
                fmt_time(v["check_in_time"]),
                fmt_time(v["work_done_time"]),
                fmt_time(v["check_out_time"]),
                fmt_duration(v["check_in_time"], v["check_out_time"]),
                Paragraph(safe(email_text), cell_style),
            ]
        )

    if len(table_data) == 1:
        story.append(Paragraph("No check-ins were recorded in this period.", styles["Normal"]))
    else:
        # Six columns across the 7 inches between the margins. Widths are set by
        # the headers, not the values: the times are all about 0.6in, but every
        # column has to be wide enough to break its label between words. At
        # 0.65in "Duration" came out as "Duratio / n".
        detail_table = Table(
            table_data,
            colWidths=[1.45 * inch, 0.95 * inch, 0.95 * inch, 1.0 * inch, 0.8 * inch, 1.85 * inch],
            repeatRows=1,
        )
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
            f"{auto_closed} student(s) were not checked out by a parent and were closed out "
            f"automatically at the center's closing time. Their check-out times are the "
            f"center's closing time, not an observed pick-up.",
            footer_style,
        ))
        story.append(Spacer(1, 6))
    if never_finished_unemailed:
        story.append(Paragraph(
            f"{never_finished_unemailed} student(s) never marked their work as done, so their "
            f"parents were not emailed. The parent email is sent at that step, not at check-out.",
            footer_style,
        ))
        story.append(Spacer(1, 6))
    if pre_upgrade:
        story.append(Paragraph(
            f"{pre_upgrade} visit(s) predate the three-step kiosk, so they have no "
            f"\"work completed\" time. Their parents were emailed at check-out, which is how "
            f"the system worked at the time.",
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
