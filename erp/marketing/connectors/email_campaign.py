"""The `email_campaign` connector: one exact export of E-MAIL CAMPAIGN METRICS, one row per campaign send, daily grain, EUR.

The file (owner's sample: data_inbox/data2.csv, 128 rows) has exactly these 12 columns, in any order and no others:

    send_date brand country campaign_id segment delivered unique_opens unique_clicks orders revenue_eur unsubscribes spam_complaints

Owner-approved mapping:

    file column        -> fact / attrs                                   note
    send_date          -> event_date                                     ISO YYYY-MM-DD (a slash date is accepted only when unambiguous)
    campaign_id        -> campaign (dimension; key = slug, name = the id)
    delivered          -> impressions                                    "delivered", NOT an ad impression: an e-mail that reached an
                                                                         inbox. attrs.impressions_are = 'delivered' and attrs.delivered
                                                                         keeps the exact number
    unique_opens       -> reactions                                      "unique opens", NOT reactions in the social sense.
                                                                         attrs.reactions_are = 'unique_opens', attrs.unique_opens = exact number
    unique_clicks      -> clicks
    orders             -> conversions
    revenue_eur        -> revenue, currency EUR                          the header itself names the currency
    brand, country, segment, unsubscribes, spam_complaints -> attrs      brand / country / segment are also in attrs_idx (filterable
                                                                         through the scoped GIN index); the rest is readable, not indexed
    (channel)          -> `email`                                        a parameter, overridable with --channel
    spend, sessions    -> 0                                              the file has no cost and no visit count

SANITY RULES, checked per row, and a violating row is REJECTED (counted, with its reason in the run summary) - never loaded
quietly: unique_opens <= delivered, and unique_clicks <= unique_opens. Both hold for every row of the sample.

NATURAL KEY: (channel, send_date, campaign_id, brand, country, segment) with the occurrence index within an identical key,
exactly as in ad_performance.py. In the sample campaign_id is unique per row, so no collision occurs; a future export that
repeats a campaign on the same day and segment is kept row for row and reported, never overwritten.

Run:
    venv\\Scripts\\python.exe -m erp.marketing.ingest --connector email_campaign --csv data_inbox\\data2.csv --dry-run
"""
from __future__ import annotations

from erp.marketing import parse
from erp.marketing.connectors.strict_csv import StrictCsvConnector, natural_external_id
from erp.marketing.model import InvalidRecord, clean_text

HEADER = ("send_date", "brand", "country", "campaign_id", "segment", "delivered", "unique_opens", "unique_clicks", "orders",
          "revenue_eur", "unsubscribes", "spam_complaints")
REVENUE_COLUMN = "revenue_eur"
REVENUE_CURRENCY = "EUR"        # the column name IS the currency declaration; another currency is another header, not a guess


class EmailCampaignConnector(StrictCsvConnector):
    source_key = "email_campaign"
    HEADER = HEADER
    DATE_COLUMNS = ("send_date",)
    DEFAULT_CHANNEL = "email"

    def natural_parts(self, row: dict) -> tuple | None:
        try:
            day = self.date_of(row.get("send_date")).isoformat()
        except ValueError:
            day = clean_text(row.get("send_date")) or ""
        return (self.channel, day, clean_text(row.get("campaign_id")) or "", clean_text(row.get("brand")) or "",
                clean_text(row.get("country")) or "", clean_text(row.get("segment")) or "")

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

        try:
            day = self.date_of(raw.get("send_date"))
        except ValueError as exc:
            raise InvalidRecord(f"{where}: send_date: {exc}") from None
        campaign_id, brand, country, segment = text("campaign_id"), text("brand"), text("country"), text("segment")
        delivered, opens, clicks, orders = whole("delivered"), whole("unique_opens"), whole("unique_clicks"), whole("orders")
        unsubscribes, complaints = whole("unsubscribes"), whole("spam_complaints")
        try:
            revenue, cur = parse.parse_money(raw.get(REVENUE_COLUMN), currency_hint=REVENUE_CURRENCY)
        except ValueError as exc:
            raise InvalidRecord(f"{where}: {REVENUE_COLUMN}: {exc}") from None
        if cur is not None and cur != REVENUE_CURRENCY:
            raise InvalidRecord(f"{where}: {REVENUE_COLUMN} is written in {cur}, but that column is EUR")

        # sanity: a row that breaks these is a corrupted export line, not an odd day - reject it, do not load it quietly
        if opens > delivered:
            raise InvalidRecord(f"{where}: unique_opens ({opens:,}) is more than delivered ({delivered:,}) - impossible, row rejected")
        if clicks > opens:
            raise InvalidRecord(f"{where}: unique_clicks ({clicks:,}) is more than unique_opens ({opens:,}) - impossible, row rejected")

        parts = self.natural_parts(raw) or ()
        occurrence = getattr(raw, "occurrence", 0)
        return {
            "source_key": self.source_key, "event_date": day, "channel_key": self.channel, "event_type": "rollup",
            "campaign_key": campaign_id, "campaign_name": campaign_id,
            "impressions": delivered, "reactions": opens, "clicks": clicks, "conversions": orders,
            "revenue_micros": parse.to_micros_exact(revenue), "currency": REVENUE_CURRENCY,
            "external_id": natural_external_id(*parts, occurrence=occurrence),
            "natural_key": "|".join(parts), "occurrence": occurrence,
            "attrs": {
                "grain": "day", "brand": brand, "country": country, "segment": segment,
                "delivered": delivered, "unique_opens": opens,
                "impressions_are": "delivered", "reactions_are": "unique_opens",
                "unsubscribes": unsubscribes, "spam_complaints": complaints,
                "source_line": line or None,
            },
        }
