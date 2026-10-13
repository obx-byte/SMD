"""Excel / PDF exports of the Inspection Report.

Both exports render **only the records that match the active filters** and are
built without any third-party dependency (no openpyxl, no reportlab, no
Pillow):

* Excel — a spreadsheet-compatible HTML table served as ``.xls``.
* PDF — a real PDF written by :class:`PdfDocument` (Helvetica, WinAnsi
  encoding, multi-page tables).

The data handed in is already presentation-ready (see
``VisionInspection.services.inspection_records.export_rows``), so nothing is
computed or invented here.
"""

from datetime import datetime

from django.http import HttpResponse

from VisionInspection.services import inspection_records as records

EXCEL_CONTENT_TYPE = "application/vnd.ms-excel"
PDF_CONTENT_TYPE = "application/pdf"

# Report palette (same values as the shared admin theme).
_COLOR_TEXT = (0.07, 0.09, 0.18)
_COLOR_MUTED = (0.45, 0.49, 0.60)
_COLOR_HEADER = (0.396, 0.145, 0.961)
_COLOR_BORDER = (0.90, 0.89, 0.95)
_COLOR_ROW_ALT = (0.976, 0.969, 0.988)
TONE_COLORS = {
    "ok": (0.09, 0.56, 0.34),
    "not_ok": (0.82, 0.16, 0.24),
    "error": (0.79, 0.53, 0.10),
    "incomplete": (0.96, 0.65, 0.14),
}
TONE_HEX = {
    "ok": "#178f56",
    "not_ok": "#d12a3d",
    "error": "#c9861a",
    "incomplete": "#c9861a",
}


def _timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _file_stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# --------------------------------------------------------------------------- #
#  Excel
# --------------------------------------------------------------------------- #

def _escape_html(value):
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def excel_response(title, meta, headers, rows):
    """Spreadsheet-compatible workbook of the filtered inspection records."""
    body = [
        "<html xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:x=\"urn:schemas-microsoft-com:office:excel\">",
        "<head><meta charset=\"utf-8\">",
        f"<title>{_escape_html(title)}</title></head>",
        "<body>",
        f"<table border=\"0\" cellpadding=\"4\" cellspacing=\"0\">",
        f"<tr><td colspan=\"{len(headers)}\" style=\"font-size:16pt;font-weight:bold\">"
        f"{_escape_html(title)}</td></tr>",
    ]
    for line in meta:
        body.append(
            f"<tr><td colspan=\"{len(headers)}\" style=\"font-size:10pt;color:#555555\">"
            f"{_escape_html(line)}</td></tr>"
        )
    body.append("<tr><td colspan=\"%d\"></td></tr>" % len(headers))

    body.append("<tr>")
    for header in headers:
        body.append(
            "<td bgcolor=\"#6525F5\" style=\"color:#FFFFFF;font-weight:bold\">"
            f"{_escape_html(header)}</td>"
        )
    body.append("</tr>")

    for row in rows:
        body.append("<tr>")
        for text, tone in row:
            color = TONE_HEX.get(tone, "#11182F")
            weight = "bold" if tone else "normal"
            body.append(
                f"<td style=\"color:{color};font-weight:{weight}\">"
                f"{_escape_html(text)}</td>"
            )
        body.append("</tr>")

    body.append("</table></body></html>")
    content = "".join(body)

    response = HttpResponse(content, content_type=EXCEL_CONTENT_TYPE)
    response["Content-Disposition"] = (
        f'attachment; filename="Inspection_Report_{_file_stamp()}.xls"'
    )
    return response


# --------------------------------------------------------------------------- #
#  PDF — minimal writer
# --------------------------------------------------------------------------- #

class PdfDocument:
    """Minimal PDF writer: regular + bold Helvetica, vector rectangles, pages.

    Only the PDF features the report needs are implemented; the output is a
    plain, standards-conformant PDF 1.4 file (no compression, no external
    objects), so it can be verified and opened anywhere.
    """

    PAGE_SIZES = {
        "a4-portrait": (595.0, 842.0),
        "a4-landscape": (842.0, 595.0),
    }
    FONT_REGULAR = "F1"
    FONT_BOLD = "F2"
    MARGIN = 28.0

    def __init__(self, page_size="a4-landscape"):
        self.page_width, self.page_height = self.PAGE_SIZES[page_size]
        self.pages = []
        self._ops = []
        self.pages.append(self._ops)

    # -- pages ---------------------------------------------------------- #

    def new_page(self):
        self._ops = []
        self.pages.append(self._ops)
        return len(self.pages)

    @property
    def page_count(self):
        return len(self.pages)

    # -- primitives ----------------------------------------------------- #

    def _baseline(self, top):
        return self.page_height - top

    def _emit(self, ops, command):
        ops.append(command)

    def text(self, x, top, value, size=9, bold=False, color=_COLOR_TEXT,
             width=None, align="left"):
        """Draw *value* with its baseline at *top* (distance from page top)."""
        text = "" if value is None else str(value)
        if not text:
            return
        if width is not None:
            text = _fit(text, width, size)
            if not text:
                return
            if align == "center":
                x += (width - _text_width(text, size)) / 2.0
            elif align == "right":
                x += width - _text_width(text, size)
        r, g, b = color
        font = self.FONT_BOLD if bold else self.FONT_REGULAR
        self._emit(
            self._ops,
            "BT /%s %s Tf %.3f %.3f %.3f rg 1 0 0 1 %.2f %.2f Tm (%s) Tj ET\n"
            % (font, _fmt(size), r, g, b, x, self._baseline(top), _escape_pdf(text)),
        )

    def fill_rect(self, x, top, width, height, color):
        y = self.page_height - top - height
        r, g, b = color
        self._emit(
            self._ops,
            "%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f\n"
            % (r, g, b, x, y, width, height),
        )

    def stroke_rect(self, x, top, width, height, color=_COLOR_BORDER,
                    line_width=0.5):
        y = self.page_height - top - height
        r, g, b = color
        self._emit(
            self._ops,
            "%.3f w %.3f %.3f %.3f RG %.2f %.2f %.2f %.2f re S\n"
            % (line_width, r, g, b, x, y, width, height),
        )

    def line(self, x1, top1, x2, top2, color=_COLOR_BORDER, line_width=0.5):
        r, g, b = color
        self._emit(
            self._ops,
            "%.3f w %.3f %.3f %.3f RG %.2f %.2f m %.2f %.2f l S\n"
            % (
                line_width, r, g, b, x1, self._baseline(top1),
                x2, self._baseline(top2),
            ),
        )

    # -- output --------------------------------------------------------- #

    def render(self):
        """Serialise the document to PDF bytes."""
        objects = {}
        numbers = {}
        next_number = 5
        page_numbers = []

        for ops in self.pages:
            stream = "".join(ops).encode("latin-1", "replace")
            content_number = next_number
            next_number += 1
            objects[content_number] = (
                b"<< /Length " + str(len(stream)).encode("ascii")
                + b" >>\nstream\n" + stream + b"\nendstream"
            )
            page_number = next_number
            next_number += 1
            objects[page_number] = (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s %s] "
                "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                "/Contents %d 0 R >>"
                % (_fmt(self.page_width), _fmt(self.page_height), content_number)
            ).encode("ascii")
            page_numbers.append(page_number)

        info_number = next_number
        objects[info_number] = (
            "<< /Title (Vision Inspection Report) /Producer (SMD Vision "
            "Inspection) /Creator (SMD Vision Inspection) >>"
        ).encode("latin-1", "replace")

        kids = " ".join("%d 0 R" % number for number in page_numbers)
        objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
        objects[2] = (
            "<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_numbers), kids)
        ).encode("ascii")
        objects[3] = (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"
        )
        objects[4] = (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>"
        )

        output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = {}
        for number in sorted(objects):
            offsets[number] = len(output)
            output += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"

        start_xref = len(output)
        size = len(objects) + 1
        output += b"xref\n0 %d\n" % size
        output += b"0000000000 65535 f \n"
        for number in range(1, size):
            if number in offsets:
                output += b"%010d 00000 n \n" % offsets[number]
            else:  # pragma: no cover - every number is written above
                output += b"0000000000 65535 f \n"
        output += (
            b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n"
            b"%%%%EOF\n" % (size, info_number, start_xref)
        )
        return bytes(output)


def _fmt(value):
    text = ("%.3f" % float(value)).rstrip("0").rstrip(".")
    return text if text else "0"


def _text_width(text, size):
    """Approximate Helvetica width (average glyph width ≈ 0.5 em)."""
    return len(text) * size * 0.5


def _fit(text, width, size):
    """Truncate *text* so it fits inside *width* points."""
    limit = int(width / (size * 0.5)) - 1
    if len(text) <= limit:
        return text
    if limit <= 1:
        return ""
    return text[: max(limit - 3, 1)] + "..."


def _escape_pdf(value):
    text = "" if value is None else str(value)
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


# --------------------------------------------------------------------------- #
#  PDF — report layout
# --------------------------------------------------------------------------- #

_TOP = PdfDocument.MARGIN
_BOTTOM = 40.0
_HEADER_HEIGHT = 20.0
_ROW_HEIGHT = 17.0
_TABLE_TOP = 86.0


def _column_widths(document, weights):
    usable = document.page_width - 2 * PdfDocument.MARGIN
    total = float(sum(weights))
    return [usable * weight / total for weight in weights]


def pdf_response(title, meta, headers, weights, rows):
    """PDF document of the filtered inspection records."""
    document = PdfDocument("a4-landscape")
    columns = _column_widths(document, weights)
    x = PdfDocument.MARGIN

    document.text(x, _TOP, title, size=15, bold=True)
    document.text(
        x, _TOP + 16, "View and export Airbag Stitch Inspection records.",
        size=9.5, color=_COLOR_MUTED,
    )
    for index, line in enumerate(meta):
        document.text(x, _TOP + 30 + index * 11, line, size=8.5,
                      color=_COLOR_MUTED)

    y = _TABLE_TOP
    header_height = _HEADER_HEIGHT

    def draw_header(y_top):
        document.fill_rect(x, y_top, sum(columns), header_height, _COLOR_HEADER)
        cursor = x
        for label, width in zip(headers, columns):
            document.text(
                cursor + 5, y_top + header_height / 2.0 + 3.0, label,
                size=8.5, bold=True, color=(1, 1, 1), width=width - 10,
            )
            cursor += width

    def draw_row(y_top, row):
        cursor = x
        for (text, tone), width in zip(row, columns):
            document.text(
                cursor + 5, y_top + _ROW_HEIGHT / 2.0 + 3.0, text,
                size=8.5,
                color=TONE_COLORS.get(tone, _COLOR_TEXT),
                width=width - 10,
            )
            cursor += width
        document.stroke_rect(
            x, y_top, sum(columns), _ROW_HEIGHT, _COLOR_BORDER, 0.4
        )

    draw_header(y)
    y += header_height

    for index, row in enumerate(rows):
        if y + _ROW_HEIGHT > document.page_height - _BOTTOM:
            document.new_page()
            y = _TOP + 24
            draw_header(y)
            y += header_height
        if index % 2 == 1:
            document.fill_rect(x, y, sum(columns), _ROW_HEIGHT, _COLOR_ROW_ALT)
        draw_row(y, row)
        y += _ROW_HEIGHT

    if not rows:
        document.text(
            x + 10, y + 22, "No inspection records match the selected filters.",
            size=9.5, color=_COLOR_MUTED,
        )
        y += _ROW_HEIGHT
        document.stroke_rect(
            x, y, sum(columns), _ROW_HEIGHT, _COLOR_BORDER, 0.4
        )

    # Page footers — added to every page once the layout is complete.
    total_pages = document.page_count
    for index, ops in enumerate(document.pages, start=1):
        document._emit(
            ops,
            "BT /F1 8 Tf 0.45 0.49 0.60 rg 1 0 0 1 %.2f %.2f Tm (%s) Tj ET\n"
            % (
                document.page_width / 2.0 - 30,
                document._baseline(document.page_height - 18),
                _escape_pdf("Page %d of %d" % (index, total_pages)),
            ),
        )

    response = HttpResponse(document.render(), content_type=PDF_CONTENT_TYPE)
    response["Content-Disposition"] = (
        f'attachment; filename="Inspection_Report_{_file_stamp()}.pdf"'
    )
    return response


def meta_lines(filters, summary, total_records):
    """Human-readable summary of the active selection (used by both exports)."""
    parts = ["Generated: %s" % _timestamp()]
    parts.append("Records exported: %d" % total_records)
    parts.append(
        "Totals — Inspections: %(total)d · OK: %(ok)d (%(ok_pct)s%%) · "
        "NOT OK: %(not_ok)d (%(not_ok_pct)s%%) · Incomplete / Error: "
        "%(incomplete_error)d (%(incomplete_error_pct)s%%)" % summary
    )
    if filters.get("date_from") or filters.get("date_to"):
        parts.append(
            "Date: %s to %s"
            % (filters.get("date_from") or "start", filters.get("date_to") or "today")
        )
    if filters.get("part_number"):
        parts.append("Part: %s" % filters["part_number"])
    if filters.get("part_name"):
        parts.append("Part name contains: %s" % filters["part_name"])
    if filters.get("employee"):
        parts.append("Employee: %s" % filters["employee"])
    if filters.get("unique_number"):
        parts.append("Unique number contains: %s" % filters["unique_number"])
    if filters.get("result"):
        parts.append("Result: %s" % records.result_text(filters["result"]))
    return parts


COLUMN_WEIGHTS = [1, 4, 3, 5, 5, 3.5, 3, 3, 3]
