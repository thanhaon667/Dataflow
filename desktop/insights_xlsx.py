"""Channels page, EXCEL writer: turns sheet specs (a header of (title, kind) and rows of plain values) into an .xlsx workbook with
openpyxl. Pure: no database, no request. desktop/insights_data.py prepares the rows.

Rules of the file: numbers are real numeric cells with a number format (never text); dates are date cells; the first row of every
sheet is a styled header and is frozen; column widths fit the content; a TEXT value that starts with = + - @ (or a tab / carriage
return) gets a leading apostrophe so a spreadsheet can never run it as a formula (the same rule as the CSV); characters XML cannot
carry are removed; money cells take their number format from the row's currency (no decimals for VND, JPY ...); a blank cell means
unknown, never 0. The workbook holds aggregates only.
"""
from __future__ import annotations

import io
from itertools import islice
from dataclasses import dataclass, field
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from desktop.channels_money import ZERO_DECIMAL_CURRENCIES
from erp import typography

INK, LINE, BG = "1C1D22", "E7E2D8", "F5F3EF"
FMT_INT, FMT_PCT, FMT_DATE = "#,##0", "0.0%", "yyyy-mm-dd"
FMT_MONEY2, FMT_MONEY0, FMT_NUM = "#,##0.00", "#,##0", "General"
TEXT_MAX = 32_000
COL_MIN, COL_MAX = 10, 60
ROWS_MAX = 20_000                  # data rows per sheet: enforced HERE, before a single cell is created (a huge set cannot blow memory)
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class Sheet:
    name: str
    header: list                       # [(title, kind)]
    rows: object                       # [[value, ...]]: a list, or any iterable (only the first ROWS_MAX rows are ever read)
    currency_col: int | None = None    # index of the column whose text decides a money cell's number format
    notes: list = field(default_factory=list)


def safe_text(v) -> str:
    """Text for a cell: XML-illegal characters removed, capped, and a leading = + - @ tab CR neutralised with an apostrophe."""
    s = ILLEGAL_CHARACTERS_RE.sub("", str(v))[:TEXT_MAX]
    return "'" + s if s.startswith(FORMULA_STARTS) else s


def kind_of_channel_column(h: str) -> str:
    if h in ("period_from", "period_to"):
        return "date"
    if h in ("sessions", "clicks", "impressions", "conversions", "days_with_data"):
        return "int"
    if h.endswith("_pct"):
        return "pct"
    if h in ("spend", "revenue", "cost_per_conversion"):
        return "money"
    if h == "low_volume":
        return "bool"
    return "text"


def _money_fmt(currency) -> str:
    return FMT_MONEY0 if str(currency or "").strip() in ZERO_DECIMAL_CURRENCIES else FMT_MONEY2


def _write(ws, r: int, c: int, value, kind: str, currency=None):
    cell = ws.cell(row=r, column=c)
    if value is None or value == "":
        return cell
    if isinstance(value, bool):
        cell.value = "yes" if value else "no"
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:
            return cell
        cell.value = value
        cell.number_format = {"int": FMT_INT, "int_plain": "0", "pct": FMT_PCT, "money": _money_fmt(currency)}.get(kind, FMT_NUM)
        if kind == "auto":
            cell.number_format = FMT_INT if isinstance(value, int) else _money_fmt(currency) if currency else "0.0%" if 0 <= value <= 1 and isinstance(value, float) and not currency else FMT_NUM
    elif isinstance(value, (date, datetime)):
        cell.value = value
        cell.number_format = FMT_DATE
    else:
        cell.value = safe_text(value)
        cell.data_type = "s"
    return cell


def build_workbook(sheets: list[Sheet]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    wb.properties.creator = "ERP Desk (erp_support)"
    wb.properties.title = "Channels report"
    font = typography.FONT_NAME
    head_font = Font(name=font, bold=True, color="FFFFFF", size=10)
    body_font = Font(name=font, size=10)
    head_fill = PatternFill("solid", fgColor=INK)
    edge = Border(bottom=Side(style="thin", color=LINE))
    for sh in sheets:
        ws = wb.create_sheet(title=sh.name[:31])
        widths = []
        for i, (title, _k) in enumerate(sh.header, start=1):
            c = ws.cell(row=1, column=i, value=safe_text(title))
            c.font, c.fill = head_font, head_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
            widths.append(len(str(title)) + 2)
        ws.row_dimensions[1].height = 30
        rows = list(islice(sh.rows, ROWS_MAX + 1))               # cap BEFORE building cells: at most ROWS_MAX + 1 rows are ever taken
        cut = len(rows) > ROWS_MAX
        rows = rows[:ROWS_MAX]
        for ri, row in enumerate(rows, start=2):
            cur = row[sh.currency_col] if sh.currency_col is not None and sh.currency_col < len(row) else None
            for ci, (val, (_t, kind)) in enumerate(zip(row, sh.header), start=1):
                cell = _write(ws, ri, ci, val, kind, cur)
                cell.font, cell.border = body_font, edge
                if kind == "text_wide" or (kind == "auto" and isinstance(val, str)):
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                if ri < 202 and val is not None:
                    widths[ci - 1] = max(widths[ci - 1], min(len(str(val)), COL_MAX) + 2)
        for i, w in enumerate(widths, start=1):
            kind = sh.header[i - 1][1]
            ws.column_dimensions[get_column_letter(i)].width = COL_MAX if kind == "text_wide" else max(COL_MIN, min(COL_MAX, w))
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(sh.header))}{len(rows) + 1}"
        if cut:                                                    # the visible truncation note, inside the file (L-211)
            note = ws.cell(row=len(rows) + 3, column=1, value=f"Note: this sheet was cut at {ROWS_MAX:,} rows; narrow the filters to see the rest.")
            note.font = Font(name=font, size=10, italic=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
