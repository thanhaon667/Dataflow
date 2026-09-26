"""The one example connector: a generic, source-agnostic FLAT FILE (CSV / list of dicts with arbitrary columns).

It exists to prove the connector pattern end to end without inventing a fake Facebook-specific mapping
that would only be guesswork until the owner's real credentials arrive (Phase 2). Give it any table-shaped
export and it will:

  * recognise the columns it can - a date, a channel / platform name, an event or action, a campaign, a
    creative, the usual metric names (impressions, clicks, reactions, sessions, conversions, spend, revenue)
    and anything that identifies a person (e-mail, phone, click id, user id, session id);
  * put EVERYTHING it does not recognise into `attrs`, so no column is ever silently dropped;
  * read the currency of spend / revenue from a `currency` column when the file has one, else from the connector's
    `currency_default` (--currency on the command line), else model.DEFAULT_CURRENCY ('USD'); a value that is not a
    three-letter code rejects the row. Nothing is ever converted.
    BEHAVIOUR CHANGE, stated plainly: a `currency` column is now CONSUMED as the row's currency (it no longer lands in
    `attrs`) and a row with an invalid value there is REJECTED; before, it was just another attrs entry;
  * decide the event type honestly: a row that carries several metric columns is a `rollup` (a day the
    source already aggregated), a row with exactly one metric column is that metric's own event.

Recognition is by column NAME, matched on the slug of the header ('Amount spent (USD)' -> 'amount_spent_usd'),
first match wins, and a column that has been used for a real field is not repeated in `attrs`.

Run (through the pipeline's entry point):
    venv\\Scripts\\python.exe -m erp.marketing.ingest --csv path\\to\\export.csv --source flat_file
"""
from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Iterator

from erp.marketing.connectors.base import Connector
from erp.marketing.model import clean_text, slug

logger = logging.getLogger(__name__)

CSV_SAMPLE_BYTES = 8192
MAX_COLUMNS = 400          # a "row" with more columns than this is not a record, it is a mistake

# --- what a column name may be called. First match wins; the slug of the header is compared. --------
DATE_FIELDS = ("event_date", "date", "day", "date_start", "date_stop", "reporting_date", "report_date",
               "occurred_at", "created_at", "timestamp", "datetime", "event_time", "ngay")
TIME_FIELDS = ("event_at", "event_time", "occurred_at", "created_at", "timestamp", "datetime")
CHANNEL_FIELDS = ("channel", "source", "platform", "network", "publisher", "media_source", "traffic_source",
                  "publisher_platform", "kenh")
CURRENCY_FIELDS = ("currency", "currency_code", "iso_currency")
MEDIUM_FIELDS = ("medium", "channel_medium", "channel_group", "channel_grouping", "default_channel_group")
EVENT_FIELDS = ("event", "event_type", "event_name", "action", "action_type", "interaction", "interaction_type")
CAMPAIGN_KEY_FIELDS = ("campaign_id", "campaignid", "campaign_key")
CAMPAIGN_NAME_FIELDS = ("campaign", "campaign_name", "campaign_title", "chien_dich")
CREATIVE_KEY_FIELDS = ("ad_id", "adid", "creative_id", "creative_key", "adset_id")
CREATIVE_NAME_FIELDS = ("ad_name", "creative", "creative_name", "ad", "banner", "adset_name")
CREATIVE_FORMAT_FIELDS = ("ad_format", "creative_format", "format", "media_type")
EXTERNAL_ID_FIELDS = ("external_id", "event_id", "record_id", "row_id", "interaction_id", "id")

# identity: (common-shape identity_type, the column names that carry it)
IDENTITY_FIELDS = (
    ("email", ("email", "e_mail", "user_email", "contact_email", "lead_email")),
    ("phone", ("phone", "phone_number", "mobile", "msisdn", "contact_phone")),
    ("click_id", ("click_id", "clickid", "fbclid", "gclid", "ttclid")),
    ("external_user_id", ("user_id", "visitor_id", "customer_id", "external_user_id", "psid", "client_id")),
    ("session_id", ("session_id", "sessionid")),
)

# metric name in the common shape -> the column names that mean it, and the event type ONE such
# column on its own implies (used when the file has no event/action column).
METRIC_FIELDS = (
    ("impressions", ("impressions", "impression", "impr", "views", "video_views", "plays"), "impression"),
    ("clicks", ("clicks", "click", "link_clicks", "outbound_clicks", "taps"), "click"),
    ("reactions", ("reactions", "reaction", "likes", "engagements", "engagement", "post_engagement"), "reaction"),
    ("sessions", ("sessions", "session", "visits", "visit", "pageviews"), "session"),
    ("conversions", ("conversions", "conversion", "results", "purchases", "leads", "signups"), "conversion"),
    ("spend", ("spend", "cost", "amount_spent", "amount_spent_usd", "spend_usd", "chi_phi"), "other"),
    ("revenue", ("revenue", "conversion_value", "purchase_value", "sales", "value", "doanh_thu"), "conversion"),
)

# the source's own word for an event -> this project's vocabulary (model.EVENT_TYPES)
EVENT_WORDS = {
    "click": "click", "clicks": "click", "link_click": "click", "tap": "click",
    "impression": "impression", "impressions": "impression", "view": "impression", "video_view": "impression",
    "reaction": "reaction", "like": "reaction", "engagement": "reaction", "comment": "reaction", "share": "reaction",
    "session": "session", "visit": "session", "pageview": "session", "page_view": "session",
    "conversion": "conversion", "purchase": "conversion", "lead": "conversion", "signup": "conversion",
    "sign_up": "conversion", "subscribe": "conversion", "order": "conversion",
    "rollup": "rollup", "daily": "rollup", "summary": "rollup", "aggregate": "rollup",
}


def _pick(row: dict, names, used: set) -> tuple[str | None, object]:
    """First column of `row` whose slugged name is in `names` and that has not been used yet."""
    for n in names:
        if n in row and n not in used and row[n] is not None and str(row[n]).strip() != "":
            return n, row[n]
    return None, None


def normalise_row(raw: dict) -> dict:
    """{'Amount spent (USD)': '12,30', ...} -> {'amount_spent_usd': '12,30', ...}; unusable keys dropped."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for k, v in list(raw.items())[:MAX_COLUMNS]:
        key = slug(k, 120)
        if key and key not in out:
            out[key] = v
    return out


class FlatFileConnector(Connector):
    """Any table-shaped marketing export: a CSV file, or a list of dicts already in memory."""

    def __init__(self, rows, source_key: str = "flat_file", origin_label: str | None = None,
                 channel_default: str | None = None, currency_default: str | None = None) -> None:
        self._rows = rows
        self.source_key = slug(source_key, 60) or "flat_file"
        self._origin = origin_label
        self._channel_default = slug(channel_default, 80)
        # the currency of spend / revenue for rows whose file has no currency column: DEFAULT_CURRENCY unless told otherwise
        self._currency_default = (currency_default or "").strip().upper() or None

    # -- where the raw records come from -------------------------------------
    @classmethod
    def from_csv(cls, path, source_key: str = "flat_file", delimiter: str | None = None,
                 channel_default: str | None = None, currency_default: str | None = None) -> "FlatFileConnector":
        p = Path(path)
        return cls(_csv_rows(p, delimiter), source_key=source_key, origin_label=p.name,
                   channel_default=channel_default, currency_default=currency_default)

    def origin(self) -> str | None:
        return self._origin

    def records(self) -> Iterator[dict]:
        for raw in self._rows:
            if isinstance(raw, dict):
                yield raw

    # -- raw record -> the pipeline's common shape ---------------------------
    def map_record(self, raw: dict) -> dict | None:
        row = normalise_row(raw)
        if not row:
            return None
        used: set = set()

        def take(names) -> object:
            col, value = _pick(row, names, used)
            if col:
                used.add(col)
            return value

        date_value = take(DATE_FIELDS)
        if date_value is None:
            return None                       # no date at all: not an interaction (a totals line, a header repeat)
        time_value = take(TIME_FIELDS)

        channel = clean_text(take(CHANNEL_FIELDS), 80) or self._channel_default
        medium = take(MEDIUM_FIELDS)
        currency = take(CURRENCY_FIELDS)
        event_word = take(EVENT_FIELDS)
        campaign_key = take(CAMPAIGN_KEY_FIELDS)
        campaign_name = take(CAMPAIGN_NAME_FIELDS)
        creative_key = take(CREATIVE_KEY_FIELDS)
        creative_name = take(CREATIVE_NAME_FIELDS)
        creative_format = take(CREATIVE_FORMAT_FIELDS)
        external_id = take(EXTERNAL_ID_FIELDS)

        identity_type = identity_value = None
        for kind, names in IDENTITY_FIELDS:
            value = take(names)
            if value is not None and identity_value is None:
                identity_type, identity_value = kind, value

        measures: dict = {}
        implied: list[str] = []
        for field_name, names, event_type in METRIC_FIELDS:
            value = take(names)
            if value is not None:
                measures[field_name] = value
                implied.append(event_type)

        mapped = {
            "source_key": self.source_key,
            "event_date": date_value,
            "event_at": time_value,
            "channel_key": channel,
            "channel_medium": medium,
            "currency": currency if currency is not None else self._currency_default,   # None -> model.DEFAULT_CURRENCY
            "event_type": _event_type(event_word, implied),
            "campaign_key": campaign_key,
            "campaign_name": campaign_name,
            "creative_key": creative_key,
            "creative_name": creative_name,
            "creative_format": creative_format,
            "external_id": external_id,
            "identity_type": identity_type,
            "identity_value": identity_value,
            "attrs": {k: v for k, v in row.items() if k not in used},
            **measures,
        }
        return mapped


def _event_type(event_word, implied: list[str]) -> str:
    """What this row IS. The source's own word wins; otherwise the metric columns decide."""
    word = slug(event_word, 40)
    if word:
        return EVENT_WORDS.get(word, word)    # an unknown word is passed on; model.build_interaction maps it to 'other'
    real = [e for e in implied if e != "other"]
    if len(implied) >= 2:
        return "rollup"                        # several metric columns = a pre-aggregated line, not one interaction
    return real[0] if real else "other"


def _csv_rows(path: Path, delimiter: str | None) -> Iterator[dict]:
    """Stream a CSV (or TSV / semicolon file - the delimiter is sniffed) as dicts. utf-8-sig for Excel exports."""
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        if not delimiter:
            sample = fh.read(CSV_SAMPLE_BYTES)
            fh.seek(0)
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except (csv.Error, io.UnsupportedOperation):
                delimiter = ","
                logger.info("could not sniff the delimiter of %s - assuming a comma", path.name)
        for row in csv.DictReader(fh, delimiter=delimiter, restkey="_extra_columns"):
            yield {k: v for k, v in row.items() if k is not None}
