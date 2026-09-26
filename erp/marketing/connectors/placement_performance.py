"""The `placement_performance` connector: one exact export of DISPLAY-NETWORK PLACEMENT PERFORMANCE, daily rows.

A placement is WHERE a display ad ran: a website (domain), a marketplace app (app id) or a banner slot. The export has exactly
these 14 columns, in any order and no others (every one of them must be in the header; some cells may be blank, see below):

    Date Campaign "Ad group" Placement "Placement type" "Ad size" Position Device Impressions Clicks Cost Conversions
    "Conversion value" "Viewable impressions"

Each row is ONE PLACEMENT x AD SIZE x POSITION x DEVICE for ONE AD GROUP on ONE DAY. Mapping:

    file column           -> fact / attrs                                note
    Date                  -> event_date                              day grain (ISO, or slash dates in ONE order for the file)
    Campaign              -> campaign (dimension)                    REQUIRED
    Placement             -> creative (dimension) + attrs.placement  REQUIRED; sanitised (below). One creative row per campaign x placement:
                                                                     the existing star schema is unchanged and there is NO new dimension table
    Placement type        -> attrs.placement_type                    REQUIRED, one of website | app | video | other (any letter case)
    Ad size               -> attrs.ad_size                           WIDTHxHEIGHT such as 300x250 or 320x50; blank or "unknown" = unknown (stored blank)
    Position              -> attrs.position                          above_fold | below_fold | unknown; blank = unknown ("above fold", "above-fold" read as the same)
    Ad group, Device      -> attrs.ad_group, attrs.device            OPTIONAL: a blank cell means unknown, never a guess
    Impressions, Clicks   -> impressions, clicks                     REQUIRED whole numbers; clicks above impressions is impossible: row rejected
    Cost                  -> spend, currency                         REQUIRED; the currency is written on the cell ('12.50 EUR', 'USD 12.50', the euro
                                                                     sign, the dong sign) or comes from the connector's `currency` (--currency); a cell that
                                                                     names none, with no --currency, is rejected; a cell that names a currency other than --currency is rejected
    Conversions           -> conversions                             REQUIRED whole number ('3', '3.0'); a fraction such as 2.5 is rejected (the fact holds whole
                                                                     conversions: export whole ones or round them at the source)
    Conversion value      -> revenue                                 OPTIONAL: a blank cell = no value reported (stored 0, marked attrs.revenue_source = 'none: ...',
                                                                     and the run summary counts it as UNKNOWN, not zero)
    Viewable impressions  -> attrs.viewable_impressions              OPTIONAL: blank = not measured (attribute left OUT, never 0). NON-ADDITIVE in meaning: it is
                                                                     the part of the impressions a measurement service could judge and saw as viewable, so the
                                                                     viewability RATE is viewable / the impressions of the rows that reported it - never viewable /
                                                                     all impressions. Kept beside the counts, never mixed into `impressions`

THE CHANNEL is a parameter (the file names none): default `display`, overridable with --channel.

SANITISED TEXT. Placement strings come from third parties and end up on a page and in a spreadsheet, so a placement is stripped, its whitespace collapsed
to single spaces and it is cut at MAX_PLACEMENT (200) characters (attrs.placement_truncated marks a cut); a placement that contains a control or
invisible-formatting character (NUL, ESC, a zero-width or right-to-left override character ...) is REFUSED, not cleaned: the row is rejected with the reason.
Every other text cell goes through the same clean_text() the other connectors use.

NATURAL KEY AND DUPLICATES. A row is identified by (channel, Date, Campaign, Ad group, Placement, Placement type, Ad size, Position, Device). Two different rows
under one such key are BOTH kept and numbered (0, 1, ...) in file order, exactly as ad_performance does (strict_csv.py explains the trade-off), and the run
summary reports every collision.

THE PLACEMENT AS A CREATIVE KEY. marketing_creative keys are slugs, and a slug drops punctuation ('a.b.com' and 'a-b.com' would be one row). This connector
therefore sends creative_key = slug(placement) + '_' + 8 hex digits of a hash of the case-folded placement text, so two placements never share a creative row
by accident.

Run:
    venv\\Scripts\\python.exe -m erp.marketing.ingest --connector placement_performance --csv data_inbox\\placements.csv --dry-run
    venv\\Scripts\\python.exe -m erp.marketing.ingest --connector placement_performance --csv data_inbox\\placements.csv --currency EUR
    venv\\Scripts\\python.exe -m erp.marketing.sample_placements --rows 5000      (a synthetic file to try it with)
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

from erp.marketing import parse
from erp.marketing.connectors.strict_csv import StrictCsvConnector, natural_external_id
from erp.marketing.model import InvalidRecord, clean_text, slug, to_currency

HEADER = ("Date", "Campaign", "Ad group", "Placement", "Placement type", "Ad size", "Position", "Device", "Impressions", "Clicks",
          "Cost", "Conversions", "Conversion value", "Viewable impressions")
PLACEMENT_TYPES = ("website", "app", "video", "other")
POSITIONS = ("above_fold", "below_fold", "unknown")
MAX_PLACEMENT = 200          # the width of marketing_creative.creative_key
_SIZE = re.compile(r"([0-9]{1,4})[xX]([0-9]{1,4})")
REVENUE_REPORTED_BASIS = "reported: Conversion value column"
REVENUE_NONE_BASIS = "none: Conversion value is blank"


def sanitise_placement(value) -> tuple[str, bool]:
    """A raw placement cell -> (clean text, was it cut). Raises ValueError (with the reason) for an empty cell or one holding a
    control / invisible-formatting character. Whitespace (spaces, tabs, line breaks) is collapsed to single spaces BEFORE the check, so an
    embedded line break is not 'a control character': it is a space."""
    if value is None:
        raise ValueError("Placement is empty")
    collapsed = " ".join(str(value).split())
    if not collapsed or collapsed.casefold() in ("none", "null", "nan", "n/a", "-", "--"):
        raise ValueError("Placement is empty")
    for ch in collapsed:
        if unicodedata.category(ch).startswith("C"):
            raise ValueError(f"Placement contains a control or invisible character (U+{ord(ch):04X}): refused, not cleaned")
    if len(collapsed) > MAX_PLACEMENT:
        return collapsed[:MAX_PLACEMENT].rstrip(), True
    return collapsed, False


def creative_key_of(placement: str) -> str:
    """Readable slug + 8 hex digits of the case-folded FULL text: two placements never collide on punctuation."""
    digest = hashlib.blake2b(placement.casefold().encode("utf-8"), digest_size=4).hexdigest()
    return f"{(slug(placement, 150) or 'placement')}_{digest}"


def normalise_size(value) -> str | None:
    """'300x250' / '300X250' -> '300x250'; blank / 'unknown' -> None. Raises ValueError for anything else."""
    text = clean_text(value, 40)
    if text is None or text.casefold() == "unknown":
        return None
    m = _SIZE.fullmatch(text.replace(" ", ""))
    if not m or int(m[1]) == 0 or int(m[2]) == 0:
        raise ValueError(f"Ad size {text!r} is not WIDTHxHEIGHT (for example 300x250 or 320x50) or blank")
    return f"{int(m[1])}x{int(m[2])}"


def normalise_position(value) -> str:
    text = (clean_text(value, 40) or "unknown").casefold().replace("-", "_").replace(" ", "_")
    if text not in POSITIONS:
        raise ValueError(f"Position {clean_text(value, 40)!r} is not one of above_fold, below_fold, unknown (or blank)")
    return text


def normalise_type(value) -> str:
    text = (clean_text(value, 40) or "").casefold()
    if text not in PLACEMENT_TYPES:
        raise ValueError(f"Placement type {clean_text(value, 40)!r} is not one of website, app, video, other")
    return text


class PlacementPerformanceConnector(StrictCsvConnector):
    source_key = "placement_performance"
    HEADER = HEADER
    DATE_COLUMNS = ("Date",)
    DEFAULT_CHANNEL = "display"

    def __init__(self, rows, date_order=None, channel=None, origin_label=None, currency: str | None = None) -> None:
        super().__init__(rows, date_order=date_order, channel=channel, origin_label=origin_label)
        self.currency = to_currency(currency) if currency else None
        if currency and self.currency is None:
            raise ValueError(f"currency {currency!r} is not a three-letter ISO 4217 code")

    @classmethod
    def from_csv(cls, path, channel=None, date_format="auto", delimiter=",", currency: str | None = None):
        conn = super().from_csv(path, channel=channel, date_format=date_format, delimiter=delimiter)
        conn.currency = to_currency(currency) if currency else None
        if currency and conn.currency is None:
            raise ValueError(f"currency {currency!r} is not a three-letter ISO 4217 code")
        return conn

    def natural_parts(self, row: dict) -> tuple | None:
        """(channel, Date as an ISO day, Campaign, Ad group, Placement, Placement type, Ad size, Position, Device)."""
        try:
            day = self.date_of(row.get("Date")).isoformat()
        except ValueError:
            day = clean_text(row.get("Date")) or ""
        try:
            placement = sanitise_placement(row.get("Placement"))[0]
        except ValueError:
            placement = " ".join(str(row.get("Placement") or "").split())
        return (self.channel, day, clean_text(row.get("Campaign")) or "", clean_text(row.get("Ad group")) or "", placement,
                (clean_text(row.get("Placement type")) or "").casefold(), (clean_text(row.get("Ad size")) or "").casefold().replace(" ", ""),
                (clean_text(row.get("Position")) or "").casefold().replace("-", "_").replace(" ", "_"), clean_text(row.get("Device")) or "")

    def map_record(self, raw: dict) -> dict | None:
        if not isinstance(raw, dict):
            return None
        line = getattr(raw, "line", 0)
        where = f"line {line}" if line else "row"
        if "_extra_columns" in raw or "_missing_columns" in raw:
            got = len(HEADER) + len(raw.get("_extra_columns", ())) - int(raw.get("_missing_columns", 0))
            raise InvalidRecord(f"{where} has {got} cells but the export has {len(HEADER)}")

        def need(fn, name: str, *args):
            try:
                return fn(raw.get(name), *args)
            except ValueError as exc:
                raise InvalidRecord(f"{where}: {name}: {exc}") from None

        try:
            day = self.date_of(raw.get("Date"))
        except ValueError as exc:
            raise InvalidRecord(f"{where}: Date: {exc}") from None
        campaign = clean_text(raw.get("Campaign"), 300)
        if campaign is None:
            raise InvalidRecord(f"{where}: Campaign is empty")
        placement, truncated = need(sanitise_placement, "Placement")
        ptype = need(normalise_type, "Placement type")
        size = need(normalise_size, "Ad size")
        position = need(normalise_position, "Position")
        ad_group, device = clean_text(raw.get("Ad group"), 300), clean_text(raw.get("Device"), 100)
        impressions, clicks, conversions = need(parse.parse_int, "Impressions"), need(parse.parse_int, "Clicks"), need(parse.parse_int, "Conversions")
        if clicks > impressions:
            raise InvalidRecord(f"{where}: Clicks ({clicks:,}) is more than Impressions ({impressions:,}) - impossible, row rejected")
        viewable = None
        if clean_text(raw.get("Viewable impressions")) is not None:
            viewable = need(parse.parse_int, "Viewable impressions")
            if viewable > impressions:
                raise InvalidRecord(f"{where}: Viewable impressions ({viewable:,}) is more than Impressions ({impressions:,}) - impossible, row rejected")
        try:
            cost, currency = parse.parse_money(raw.get("Cost"), currency_hint=self.currency)
        except ValueError as exc:
            raise InvalidRecord(f"{where}: Cost: {exc}") from None
        if currency is None:
            currency = self.currency
        elif self.currency is not None and currency != self.currency:
            raise InvalidRecord(f"{where}: Cost is written in {currency} but --currency says {self.currency} - one file, one currency")
        if currency is None:
            raise InvalidRecord(f"{where}: Cost {clean_text(raw.get('Cost'), 30)!r} names no currency (write the ISO code or a symbol on the cell, or pass --currency)")
        value_cell = raw.get("Conversion value")
        revenue = None
        if clean_text(value_cell) is not None:
            try:
                revenue, cur2 = parse.parse_money(value_cell, currency_hint=currency)
            except ValueError as exc:
                raise InvalidRecord(f"{where}: Conversion value: {exc}") from None
            if cur2 is not None and cur2 != currency:
                raise InvalidRecord(f"{where}: Conversion value is in {cur2} but Cost is in {currency} - one row, one currency")

        parts = self.natural_parts(raw) or ()
        occurrence = getattr(raw, "occurrence", 0)
        attrs = {
            "grain": "day", "placement_row": True, "placement": placement, "placement_type": ptype, "ad_size": size, "position": position,
            "ad_group": ad_group, "device": device, "viewable_impressions": viewable,
            "revenue_source": REVENUE_REPORTED_BASIS if revenue is not None else REVENUE_NONE_BASIS,
            "revenue_derived": False, "placement_truncated": True if truncated else None, "source_line": line or None,
        }
        return {
            "source_key": self.source_key, "event_date": day, "channel_key": self.channel, "event_type": "rollup",
            "campaign_name": campaign, "creative_key": creative_key_of(placement), "creative_name": placement,
            "impressions": impressions, "clicks": clicks, "conversions": conversions,
            "spend_micros": parse.to_micros_exact(cost), "revenue_micros": parse.to_micros_exact(revenue) if revenue is not None else 0,
            "currency": currency,
            "external_id": natural_external_id(*parts, occurrence=occurrence), "natural_key": "|".join(parts), "occurrence": occurrence,
            "attrs": attrs,
        }
