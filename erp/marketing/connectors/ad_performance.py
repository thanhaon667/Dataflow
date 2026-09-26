"""The `ad_performance` connector: one exact export of PAID-SOCIAL AD PERFORMANCE, weekly rows, money written like `475,401 ₫`.

The file (owner's sample: data_inbox/data1.csv, 118 rows) has exactly these 19 columns, in any order and no others:

    Start End Month Period Campaign Adset "Ad name" Device Impression Reach Freq Spent "Link click" "LP view" Eng Lead
    Purchase Revenue/pur Revenue

Each row is ONE AD on ONE DEVICE for ONE WEEK (Start..End, 7 days). The owner approved this mapping:

    file column          -> fact / attrs                                  note
    Start                -> event_date                                    the row sits on its START day; M/D/YYYY (or ISO)
    End                  -> attrs.period_end  (+ attrs.grain = 'week')    must be exactly Start + 6 days, else the row is rejected
    Impression           -> impressions
    Link click           -> clicks
    Eng                  -> reactions                                     engagements, whatever the platform counts as one
    LP view              -> sessions                                      landing-page views
    Purchase             -> conversions
    Lead                 -> attrs.leads                                   NOT added to conversions (a lead is not a sale)
    Spent                -> spend, currency                               `475,401 ₫` -> 475401 VND -> integer micros; the symbol names the currency
    Revenue/pur x Purchase -> revenue                                     DERIVED, see below
    Campaign             -> campaign (dimension)
    Ad name              -> creative (dimension)
    Adset, Device        -> attrs.adset, attrs.device                     Adset and Device are part of the row's natural key
    Period, Month        -> attrs.period, attrs.month
    Reach, Freq          -> attrs.reach, attrs.frequency                  NOT additive (a person reached in two weeks is one
                                                                          person): kept for reading, never summed by anything

REVENUE IS DERIVED, and is marked so. The export's own `Revenue` column is EMPTY in this file, so revenue is computed as
Purchase x `Revenue/pur` and every such row carries attrs.revenue_derived = true and attrs.revenue_source = 'derived:
Purchase x Revenue/pur'. The rollup keeps the derived part in its own column (revenue_derived_micros) and the Channels page
says so. If a later export fills the `Revenue` column, that reported number is used instead and the row says
attrs.revenue_derived = false.

THE CHANNEL is a parameter (the file names no platform): default `paid_social`, overridable with --channel.

NATURAL KEY AND DUPLICATES. A row is identified by (channel, Start, Campaign, Adset, Ad name, Device). The sample has 6 PAIRS
of rows under one such key with DIFFERENT metrics (impressions 768 vs 341, ...). The generic dedupe would call the second a
restatement and overwrite the first: silent data loss. This connector numbers rows within an identical key (0, 1, ...) in file
order and builds an exact external id `<key>#<occurrence>`, so both rows are kept and the pipeline REPORTS every such
collision in its run summary. Re-importing the same file finds the same numbers, so it is idempotent. The number is a
position: it is only stable while the export's row order is stable (strict_csv.py explains the trade-off).

Run:
    venv\\Scripts\\python.exe -m erp.marketing.ingest --connector ad_performance --csv data_inbox\\data1.csv --dry-run
    venv\\Scripts\\python.exe -m erp.marketing.ingest --connector ad_performance --csv data_inbox\\data1.csv --channel facebook
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from erp.marketing import parse
from erp.marketing.connectors.strict_csv import StrictCsvConnector, natural_external_id
from erp.marketing.model import InvalidRecord, clean_text

HEADER = ("Start", "End", "Month", "Period", "Campaign", "Adset", "Ad name", "Device", "Impression", "Reach", "Freq",
          "Spent", "Link click", "LP view", "Eng", "Lead", "Purchase", "Revenue/pur", "Revenue")
WEEK_DAYS = 7
REVENUE_DERIVED_BASIS = "derived: Purchase x Revenue/pur"
REVENUE_REPORTED_BASIS = "reported: Revenue column"


def _num(value: Decimal):
    """Decimal -> a JSON number without a spurious .0 (an int when it is whole), for attrs."""
    return int(value) if value == value.to_integral_value() else float(value)


def _month(value):
    """The export's own Month column as a number when it is one (it is only a label: the fact is dated by Start)."""
    try:
        return parse.parse_int(value)
    except ValueError:
        return None


class AdPerformanceConnector(StrictCsvConnector):
    source_key = "ad_performance"
    HEADER = HEADER
    DATE_COLUMNS = ("Start", "End")
    DEFAULT_CHANNEL = "paid_social"

    def natural_parts(self, row: dict) -> tuple | None:
        """(channel, Start as an ISO day, Campaign, Adset, Ad name, Device). A date that cannot be read falls back to its text."""
        try:
            start = self.date_of(row.get("Start")).isoformat()
        except ValueError:
            start = clean_text(row.get("Start")) or ""
        return (self.channel, start, clean_text(row.get("Campaign")) or "", clean_text(row.get("Adset")) or "",
                clean_text(row.get("Ad name")) or "", clean_text(row.get("Device")) or "")

    def map_record(self, raw: dict) -> dict | None:
        if not isinstance(raw, dict):
            return None
        line = getattr(raw, "line", 0)
        where = f"line {line}" if line else "row"
        if "_extra_columns" in raw or "_missing_columns" in raw:
            got = len(HEADER) + len(raw.get("_extra_columns", ())) - int(raw.get("_missing_columns", 0))
            raise InvalidRecord(f"{where} has {got} cells but the export has {len(HEADER)}")

        def text(name: str) -> str:
            v = clean_text(raw.get(name), 300)
            if v is None:
                raise InvalidRecord(f"{where}: {name} is empty")
            return v

        def whole(name: str) -> int:
            try:
                return parse.parse_int(raw.get(name))
            except ValueError as exc:
                raise InvalidRecord(f"{where}: {name}: {exc}") from None

        def when(name: str):
            try:
                return self.date_of(raw.get(name))
            except ValueError as exc:
                raise InvalidRecord(f"{where}: {name}: {exc}") from None

        start, end = when("Start"), when("End")
        if end - start != timedelta(days=WEEK_DAYS - 1):
            raise InvalidRecord(f"{where}: Start {start} to End {end} is not one {WEEK_DAYS}-day week - not a weekly row")
        campaign, adset, ad, device = text("Campaign"), text("Adset"), text("Ad name"), text("Device")
        impressions, reach, clicks = whole("Impression"), whole("Reach"), whole("Link click")
        lp_views, engagements, leads, purchases = whole("LP view"), whole("Eng"), whole("Lead"), whole("Purchase")
        try:
            frequency = parse.parse_decimal(raw.get("Freq"))
        except ValueError as exc:
            raise InvalidRecord(f"{where}: Freq: {exc}") from None
        try:
            spent, currency = parse.parse_money(raw.get("Spent"), require_currency=True)
        except ValueError as exc:
            raise InvalidRecord(f"{where}: Spent: {exc}") from None
        if currency is None:
            raise InvalidRecord(f"{where}: Spent {clean_text(raw.get('Spent'), 30)!r} names no currency (a symbol such as the dong sign, or an ISO code)")

        def money(name: str) -> Decimal | None:
            cell = raw.get(name)
            if clean_text(cell) is None:
                return None
            try:
                amount, cur = parse.parse_money(cell, currency_hint=currency)
            except ValueError as exc:
                raise InvalidRecord(f"{where}: {name}: {exc}") from None
            if cur is not None and cur != currency:
                raise InvalidRecord(f"{where}: {name} is in {cur} but Spent is in {currency} - one row, one currency")
            return amount

        per_purchase = money("Revenue/pur")
        reported = money("Revenue")
        if reported is not None:
            revenue, derived, basis = reported, False, REVENUE_REPORTED_BASIS
        elif per_purchase is not None:
            revenue, derived, basis = per_purchase * purchases, True, REVENUE_DERIVED_BASIS
        else:
            revenue, derived, basis = Decimal(0), False, "none: the row has neither Revenue nor Revenue/pur"

        natural = "|".join(self.natural_parts(raw) or ())
        attrs = {
            "grain": "week", "period_end": end.isoformat(), "period_days": WEEK_DAYS,
            "adset": adset, "device": device, "period": clean_text(raw.get("Period"), 40), "month": _month(raw.get("Month")),
            "leads": leads,                                     # NOT conversions
            "reach": reach, "frequency": _num(frequency),       # NOT additive: never summed
            "revenue_derived": derived, "revenue_source": basis,
            "source_line": line or None,
        }
        if per_purchase is not None:
            attrs["revenue_per_purchase"] = _num(per_purchase)
        return {
            "source_key": self.source_key, "event_date": start, "channel_key": self.channel, "event_type": "rollup",
            "campaign_name": campaign, "creative_name": ad,
            "impressions": impressions, "clicks": clicks, "reactions": engagements, "sessions": lp_views, "conversions": purchases,
            "spend_micros": parse.to_micros_exact(spent), "revenue_micros": parse.to_micros_exact(revenue), "currency": currency,
            "external_id": natural_external_id(*(self.natural_parts(raw) or ()), occurrence=getattr(raw, "occurrence", 0)),
            "natural_key": natural, "occurrence": getattr(raw, "occurrence", 0),
            "attrs": attrs,
        }
