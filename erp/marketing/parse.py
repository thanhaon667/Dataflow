"""STRICT cell parsers for the source-specific marketing connectors (ad_performance, email_campaign).

model.py's `to_number` / `to_date` are deliberately forgiving: they serve the generic flat-file connector, which has to
cope with any export. These parsers are the opposite. A connector for one KNOWN export shape has no excuse to guess, so
every function here accepts exactly the forms that shape uses and RAISES `ValueError` (with a reason a person can read)
for anything else - the row is then rejected and counted, never loaded with a guessed value.

  * money      '475,401 ₫', 'VND 475,401', '1,249.13 €', '1000000' -> (Decimal amount, ISO currency or None).
               Nothing that depends on the reader's locale is accepted. VND/JPY/KRW have no decimals: any decimal part is
               refused and 475,401 / 475.401 / 1.234.567 are thousands groups. Other currencies: plain digits with an optional
               .dd, or comma thousands with a .dd ('1,249.13'); the ambiguous '1.249' / '1,249' and the European '1.249,13'
               are refused instead of being read one way or the other. '$' is refused too (USD? AUD?
               CAD? ...): a file that uses it must say the code.
  * whole numbers   '1,234', '1234', '12.0' -> int. A fraction, a negative number or text is refused.
  * dates      ISO 'YYYY-MM-DD' always; slash dates in ONE order for the whole file, decided by `detect_slash_order`,
               which REFUSES to guess: 6/16/2024 proves month-first (16 cannot be a month), and a file in which every
               slash date has both parts <= 12 (3/4/2024) is ambiguous - the caller must then be told the order
               explicitly (--date-format mdy | dmy) instead of silently picking one.
  * CSV        `read_csv` streams a file as UTF-8 (a leading byte-order mark is dropped), with the line number of
               every record so a problem can be quoted.

Run: imported by erp/marketing/connectors/*.py; nothing here runs on import. Standard library only.
"""
from __future__ import annotations

import csv
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator

# The only currency symbols a cell may carry. '$' (and '¥', 'kr', ...) are shared by several currencies, so they are NOT here.
CURRENCY_SYMBOLS = {"₫": "VND", "đ": "VND", "€": "EUR", "£": "GBP"}
AMBIGUOUS_SYMBOLS = ("$", "¥", "kr", "R$")
_ISO_CODE = re.compile(r"[A-Z]{3}")
# Every digit class is [0-9], never \d: in Python `\d` also matches fullwidth and Arabic-Indic digits, and int()/Decimal()
# would then read them, so a cell in another script would load as a number instead of being refused.
ZERO_DECIMAL_CURRENCIES = frozenset({"VND", "JPY", "KRW"})     # currencies with no minor unit: a fractional part is never valid
_PLAIN_DIGITS = re.compile(r"[0-9]+")
_COMMA_GROUPS = re.compile(r"[1-9][0-9]{0,2}(?:,[0-9]{3})+")            # 1,234  1,234,567
_DOT_GROUPS = re.compile(r"[1-9][0-9]{0,2}(?:\.[0-9]{3})+")             # 1.234  1.234.567
_DOT_DECIMAL = re.compile(r"[0-9]+\.[0-9]+")
_COMMA_GROUPS_DOT_DECIMAL = re.compile(r"([1-9][0-9]{0,2}(?:,[0-9]{3})+)\.([0-9]+)")   # 1,249.13
_EUROPEAN = re.compile(r"(?:[1-9][0-9]{0,2}(?:\.[0-9]{3})+|[0-9]+),[0-9]+")            # 1.249,13  1249,13
_INT = re.compile(r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.0+)?")
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_ISO_DAY = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_SLASH_DAY = re.compile(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})")
DATE_ORDERS = ("mdy", "dmy")


class AmbiguousDates(ValueError):
    """The slash dates of a file can be read two ways (or contradict each other): the connector refuses to guess."""


# --------------------------------------------------------------------------- numbers and money
def _squash(text) -> str:
    return "".join(str(text).split()).replace(" ", "") if text is not None else ""


def _amount(raw: str, currency: str | None) -> Decimal:
    """The digits of a money cell (symbol and code already removed) -> Decimal, or ValueError. Nothing whose meaning
    depends on the reader's locale is accepted: a form that could be a decimal OR a thousands group is refused."""
    if currency in ZERO_DECIMAL_CURRENCIES:
        # No decimals exist here, so a dot or comma followed by exactly three digits can only be a thousands group -
        # but only in the consistent grouped form. '475.401' is 475401, '475.40' or '475,4' are refused.
        if _PLAIN_DIGITS.fullmatch(raw):
            return Decimal(raw)
        if _COMMA_GROUPS.fullmatch(raw):
            return Decimal(raw.replace(",", ""))
        if _DOT_GROUPS.fullmatch(raw):
            return Decimal(raw.replace(".", ""))
        raise ValueError(f"{raw!r} is not a valid {currency} amount: {currency} has no decimals, so a decimal part is refused. "
                         f"Accepted: plain digits (475401) or groups of three with one separator kind (475,401 or 1.234.567)")
    accepted = "plain digits with an optional .dd decimal (1249.13), or comma thousands with a .dd decimal (1,249.13)"
    if _DOT_GROUPS.fullmatch(raw) or _COMMA_GROUPS.fullmatch(raw):
        if raw.count(",") + raw.count(".") == 1:
            sep = "," if "," in raw else "."
            raise ValueError(f"{raw!r} is ambiguous: with one {sep!r} and three digits after it, it could be {raw.replace(sep, '.')} or "
                             f"{raw.replace(sep, '')}. Refused, not guessed. Accepted: {accepted}")
        if "." in raw:
            raise ValueError(f"{raw!r} groups thousands with dots (the European style) and is refused. Accepted: {accepted}")
        return Decimal(raw.replace(",", ""))                  # 1,234,567: two or more comma groups can only be thousands
    if _EUROPEAN.fullmatch(raw):
        raise ValueError(f"{raw!r} is the European form (dot thousands, comma decimal) and is refused, not guessed. Accepted: {accepted}")
    m = _COMMA_GROUPS_DOT_DECIMAL.fullmatch(raw)
    if m:
        return Decimal(m[1].replace(",", "") + "." + m[2])
    if _PLAIN_DIGITS.fullmatch(raw):
        return Decimal(raw)
    if _DOT_DECIMAL.fullmatch(raw):
        return Decimal(raw)
    raise ValueError(f"{raw!r} is not a plain amount (no sign). Accepted: {accepted}")


def parse_money(text, currency_hint: str | None = None, require_currency: bool = False) -> tuple[Decimal, str | None]:
    """'475,401 ₫' -> (Decimal('475401'), 'VND'); '1249.13' -> (Decimal('1249.13'), None). Raises ValueError otherwise.

    The returned currency is the symbol or the ISO code written on the cell (prefix or suffix), None when the cell has none.
    `currency_hint` is the currency the caller already knows the cell is in (the row's other money cell, a column that
    is defined as EUR); it only chooses the parsing rules, it is never returned. `require_currency` refuses a cell that names none
    (checked before the digits, so the message says what is really missing). A zero-decimal currency (VND, JPY, KRW)
    refuses any decimal part; any other currency (and an unknown one) refuses a form that could be a decimal or a thousands
    group ('1.249', '1,249') and the European '1.249,13'. A negative amount is refused: these exports carry spend and
    revenue, and a minus sign is a corrupted cell or a refund the connector was not built to interpret."""
    raw = _squash(text)
    if not raw:
        raise ValueError("empty money cell")
    for sym in AMBIGUOUS_SYMBOLS:
        if sym in raw:
            raise ValueError(f"currency symbol {sym!r} is ambiguous (USD, AUD, CAD ...) - write the ISO code instead")
    currency = None
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in raw:
            currency = code
            raw = raw.replace(sym, "", 1)          # only the first: a doubled symbol leaves one behind and the amount check refuses it
            break
    else:
        m = re.fullmatch(r"([A-Za-z]{3})(.+)", raw) or re.fullmatch(r"(.+?)([A-Za-z]{3})", raw)
        if m:
            code = next(g for g in m.groups() if re.fullmatch(r"[A-Za-z]{3}", g))
            if not _ISO_CODE.fullmatch(code):
                raise ValueError(f"currency code {code!r} must be written in capital letters")
            currency = code
            raw = next(g for g in m.groups() if g != code)
    if require_currency and currency is None:
        raise ValueError(f"{raw!r} names no currency (a symbol such as the dong sign, or an ISO code)")
    try:
        return _amount(raw, currency or currency_hint), currency
    except InvalidOperation:  # pragma: no cover - every branch above only builds a Decimal from validated digits
        raise ValueError(f"{raw!r} is not a number") from None


def parse_int(text) -> int:
    """'1,234' -> 1234; '12.0' -> 12. Raises ValueError for a fraction, a sign, a symbol or text."""
    raw = _squash(text)
    if not raw:
        raise ValueError("empty number cell")
    if not _INT.fullmatch(raw):
        raise ValueError(f"{raw!r} is not a whole number")
    return int(Decimal(raw.replace(",", "")))


def parse_decimal(text) -> Decimal:
    """'2.06' -> Decimal('2.06'): a plain non-negative decimal with a dot (used for the non-additive ratios)."""
    raw = _squash(text)
    if not raw or not _DECIMAL.fullmatch(raw):
        raise ValueError(f"{raw!r} is not a plain decimal number")
    return Decimal(raw)


def to_micros_exact(amount: Decimal) -> int:
    """Decimal amount -> integer micros with no float on the way (475401 -> 475401000000)."""
    return int((amount * 1_000_000).to_integral_value())


# --------------------------------------------------------------------------- dates
def parse_iso_date(text) -> date:
    m = _ISO_DAY.fullmatch(_squash(text))
    if not m:
        raise ValueError(f"{_squash(text)!r} is not an ISO date (YYYY-MM-DD)")
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        raise ValueError(f"{_squash(text)!r} is not a real calendar date") from None


def detect_slash_order(values: Iterable) -> str | None:
    """Decide month-first ('mdy') or day-first ('dmy') for the slash dates in `values`, or refuse.

    Evidence: a first part above 12 can only be a day, a second part above 12 can only be a day. Both kinds of evidence
    in one file, or neither, is an error (AmbiguousDates) - never a guess. None when no value is a slash date at all
    (the file is ISO only, so there is nothing to decide). ISO values are ignored."""
    day_first = month_first = seen = 0
    example = None
    for v in values:
        m = _SLASH_DAY.fullmatch(_squash(v))
        if not m:
            continue
        seen += 1
        a, b = int(m[1]), int(m[2])
        if a > 12 and b > 12:
            raise ValueError(f"{_squash(v)!r} is not a date in either order")
        if a > 12:
            day_first += 1
            example = example or _squash(v)
        elif b > 12:
            month_first += 1
            example = example or _squash(v)
    if not seen:
        return None
    if day_first and month_first:
        raise AmbiguousDates("the slash dates contradict each other: some can only be day/month/year and others only "
                             "month/day/year. Fix the file or split it.")
    if day_first:
        return "dmy"
    if month_first:
        return "mdy"
    raise AmbiguousDates(f"all {seen} slash dates have both parts 12 or below (for example 3/4/2024), so month/day/year and "
                         f"day/month/year cannot be told apart and this connector will not guess. Say which one the file uses: "
                         f"--date-format mdy  or  --date-format dmy.")


def parse_slash_date(text, order: str) -> date:
    """'6/16/2024' with order 'mdy' -> 2024-06-16. Raises ValueError when it does not fit that order."""
    if order not in DATE_ORDERS:
        raise ValueError(f"date order must be one of {DATE_ORDERS}")
    m = _SLASH_DAY.fullmatch(_squash(text))
    if not m:
        raise ValueError(f"{_squash(text)!r} is not a slash date (M/D/YYYY or D/M/YYYY)")
    a, b, y = int(m[1]), int(m[2]), int(m[3])
    month, day = (a, b) if order == "mdy" else (b, a)
    try:
        return date(y, month, day)
    except ValueError:
        raise ValueError(f"{_squash(text)!r} is not a real calendar date read as {'month/day' if order == 'mdy' else 'day/month'}/year") from None


def parse_date(text, order: str | None) -> date:
    """ISO always; a slash date in the file's decided `order` (None = the file has no slash date, so one is an error)."""
    s = _squash(text)
    if _ISO_DAY.fullmatch(s):
        return parse_iso_date(s)
    if order is None:
        raise ValueError(f"{s!r} is not an ISO date (YYYY-MM-DD)")
    return parse_slash_date(s, order)


# --------------------------------------------------------------------------- csv
class SourceRow(dict):
    """One CSV record as a plain dict (so it is landed unchanged as JSON) that also remembers where it came from."""
    line: int = 0            # 1-based line of the file where the record starts (the header is line 1)
    occurrence: int = 0      # how many records with the same natural key came before it in the file

    def __init__(self, data=(), line: int = 0, occurrence: int = 0) -> None:
        super().__init__(data)
        self.line = line
        self.occurrence = occurrence


def read_header(path: Path, delimiter: str = ",") -> list[str]:
    """The first line of the file, split, with a leading BOM removed. Raises ValueError for an empty file."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        try:
            return next(csv.reader(fh, delimiter=delimiter))
        except StopIteration:
            raise ValueError("the file is empty") from None


def read_csv(path: Path, delimiter: str = ",") -> Iterator[SourceRow]:
    """Stream the records of a UTF-8 (with or without BOM) CSV as SourceRow dicts. A record with the wrong number of
    cells is yielded with the keys it has plus `_extra_columns`, and the connector rejects it by name - it is
    never silently padded."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration:
            return
        for row in reader:
            if not row or all(not c.strip() for c in row):
                continue                                   # a blank line is not a record
            data = dict(zip(header, row))
            if len(row) > len(header):
                data["_extra_columns"] = row[len(header):]
            elif len(row) < len(header):
                data["_missing_columns"] = len(header) - len(row)
            yield SourceRow(data, line=reader.line_num)
