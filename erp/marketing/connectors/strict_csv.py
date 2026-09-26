"""Shared machinery of the source-specific CSV connectors (ad_performance, email_campaign): an EXACT header, an explicit
date order, and the occurrence index of rows that share a natural key.

A generic connector (flat_file.py) recognises columns by a fuzzy name match and keeps whatever it cannot place. That is
right for "any export" and wrong for an export whose exact shape is known: a fuzzy match can mis-map a column without
anyone noticing. A `StrictCsvConnector` therefore

  * REFUSES a file whose header is not exactly the declared columns (a missing one, an extra one, a duplicate, a
    different spelling) and names the difference - before a single row is read;
  * REFUSES to guess a slash-date order (see parse.detect_slash_order) and says which flag settles it;
  * reads UTF-8 with or without a byte-order mark;
  * numbers the rows that share a NATURAL KEY, in file order. The number is part of the row's identity
    (see model.dedupe_key): two different rows under one key are BOTH kept, and re-importing the same file finds
    the same numbers, so the second import updates the same rows and adds none. The trade-off, stated plainly: the
    number is a position, so it is only stable while the export's row order is stable. If the same rows arrive in a
    different order, rows under an identical key can swap their metrics with each other - none is lost and no total
    changes, but the individual pairing does. The same position-based identity has two more consequences the run
    summary does NOT warn about: a row REMOVED from a duplicate group renumbers the later twins (A, B, C ->
    B, C: B overwrites A's stored row, C overwrites B's, and C's old row stays behind, stale); a row ADDED to a group
    is harmless when appended, but inserted before its twins every row takes its predecessor's number and overwrites
    that stored row, and the last twin becomes a new row; and two files that partly overlap pair rows by position, not by
    content. Only the count of restated rows whose stored numbers CHANGED is reported ("restated changed").
  * refuses a header whose column names carry leading or trailing spaces (they would pass a stripped comparison while
    the cells stayed under the padded name and were silently ignored).

Run: imported by the connectors and by erp/marketing/ingest.py; nothing here runs on import.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Iterable, Iterator

from erp.marketing import parse
from erp.marketing.connectors.base import Connector, ConnectorError
from erp.marketing.model import slug

DATE_FORMATS = ("auto", "mdy", "dmy")
MAX_EXTERNAL_ID = 190          # the landing/fact identity column is 200 wide: longer ids are replaced by their digest


def natural_external_id(*parts, occurrence: int = 0) -> str:
    """The exact identity of one row: its natural key parts plus the occurrence index within an identical key.

    Readable while it fits the identity column; a digest when it would not (a truncated id could merge two rows)."""
    text = "|".join(" ".join(str(p if p is not None else "").split()).casefold() for p in parts) + f"#{int(occurrence)}"
    if len(text) <= MAX_EXTERNAL_ID:
        return text
    return "h:" + hashlib.blake2b(text.encode("utf-8"), digest_size=20).hexdigest()


class StrictCsvConnector(Connector):
    """Base class of an export with one known shape. Subclasses set the class attributes and implement
    `natural_parts(row)` and `map_record(row)`."""

    HEADER: tuple[str, ...] = ()           # exactly these columns, any order, no others
    DATE_COLUMNS: tuple[str, ...] = ()     # the columns scanned to decide the slash-date order
    DEFAULT_CHANNEL: str = "unknown"

    def __init__(self, rows: Iterable[dict], date_order: str | None = None, channel: str | None = None,
                 origin_label: str | None = None) -> None:
        self._rows = rows
        self.date_order = date_order                     # 'mdy' | 'dmy' | None (= ISO dates only)
        self.channel = slug(channel, 80) or self.DEFAULT_CHANNEL
        self._origin = origin_label

    # -- construction ---------------------------------------------------------
    @classmethod
    def check_header(cls, header: list[str]) -> None:
        """Raise ConnectorError naming every difference between the file's header and the declared one."""
        padded = [h for h in header if isinstance(h, str) and h != h.strip()]
        if padded:
            # read_csv keys each record by the header AS WRITTEN, so a padded name would pass a stripped comparison and its
            # cells would then be silently ignored. A strict connector does not guess: fix the header instead.
            raise ConnectorError(
                f"the header of this file is not the {cls.source_key} export (column names with leading or trailing spaces: "
                f"{', '.join(repr(h) for h in padded)}). This connector never guesses a column; remove the spaces from the header.")
        names = list(header)
        dup = sorted({h for h in names if names.count(h) > 1})
        missing = [h for h in cls.HEADER if h not in names]
        extra = [h for h in names if h not in cls.HEADER]
        if dup or missing or extra:
            bits = []
            if missing:
                bits.append("missing: " + ", ".join(repr(h) for h in missing))
            if extra:
                bits.append("not part of this export: " + ", ".join(repr(h) for h in extra))
            if dup:
                bits.append("repeated: " + ", ".join(repr(h) for h in dup))
            raise ConnectorError(
                f"the header of this file is not the {cls.source_key} export ({'; '.join(bits)}). This connector maps one exact "
                f"shape and never guesses a column; expected exactly: {', '.join(cls.HEADER)}. "
                f"For any other file use the generic flat_file connector.")

    @classmethod
    def decide_date_order(cls, values: Iterable, date_format: str = "auto") -> str | None:
        """The file's slash-date order. 'auto' detects it and REFUSES when it cannot; 'mdy'/'dmy' force it, but a
        file whose dates prove the opposite is still refused."""
        if date_format not in DATE_FORMATS:
            raise ConnectorError(f"date format must be one of {', '.join(DATE_FORMATS)}")
        values = list(values)
        try:
            detected = parse.detect_slash_order(values)
        except parse.AmbiguousDates as exc:
            if date_format == "auto":
                raise ConnectorError(str(exc)) from None
            return date_format                            # ambiguous, and the operator said which one: their call
        except ValueError as exc:
            raise ConnectorError(str(exc)) from None
        if date_format != "auto" and detected and detected != date_format:
            raise ConnectorError(f"you asked for --date-format {date_format}, but the dates in this file can only be read "
                                 f"{detected} (for example a part above 12 sits in the other position). Refusing to load it.")
        return date_format if date_format != "auto" else detected

    @classmethod
    def from_csv(cls, path, channel: str | None = None, date_format: str = "auto", delimiter: str = ",") -> "StrictCsvConnector":
        p = Path(path)
        try:
            header = parse.read_header(p, delimiter)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ConnectorError(f"cannot read {p.name}: {exc}") from None
        cls.check_header(header)
        try:
            order = cls.decide_date_order(
                (row.get(col) for row in parse.read_csv(p, delimiter) for col in cls.DATE_COLUMNS), date_format)
        except UnicodeDecodeError as exc:
            raise ConnectorError(f"{p.name} is not valid UTF-8 ({exc.reason} at byte {exc.start})") from None
        return cls(parse.read_csv(p, delimiter), date_order=order, channel=channel, origin_label=p.name)

    @classmethod
    def from_rows(cls, rows: list[dict], channel: str | None = None, date_format: str = "auto") -> "StrictCsvConnector":
        """The same checks for rows already in memory (tests, another reader). The header is the first row's keys."""
        rows = [r for r in rows if isinstance(r, dict)]
        if rows:
            cls.check_header(list(rows[0].keys()))
        order = cls.decide_date_order((r.get(c) for r in rows for c in cls.DATE_COLUMNS), date_format)
        return cls(rows, date_order=order, channel=channel)

    # -- Connector contract ---------------------------------------------------
    def origin(self) -> str | None:
        return self._origin

    def natural_parts(self, row: dict) -> tuple | None:  # pragma: no cover - overridden
        """The values that identify a row in the SOURCE (a tuple of text), or None when they cannot be read."""
        raise NotImplementedError

    def records(self) -> Iterator[dict]:
        """The rows in file order, each tagged with its occurrence index within an identical natural key."""
        seen: Counter = Counter()
        for raw in self._rows:
            if not isinstance(raw, dict):
                continue
            row = raw if isinstance(raw, parse.SourceRow) else parse.SourceRow(raw)
            try:
                parts = self.natural_parts(row)
            except Exception:  # noqa: BLE001 - an unreadable row simply has no natural key; map_record rejects it by name
                parts = None
            if parts is not None:
                key = tuple(" ".join(str(x).split()).casefold() for x in parts)
                row.occurrence = seen[key]
                seen[key] += 1
            yield row

    def date_of(self, text):
        """Parse one date cell with this file's decided order (raises ValueError with the reason)."""
        return parse.parse_date(text, self.date_order)
