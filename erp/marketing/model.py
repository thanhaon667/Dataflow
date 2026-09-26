"""The common intermediate shape of the marketing ingestion pipeline: what every connector maps to.

A connector never talks to the database and never decides how anything is stored. It turns one
raw record of its own source into a plain dict of the keys below; everything after that - typing,
validation, dedupe keys, which extras get indexed, the SQL - is this package's job and is shared by
every source. Adding a source later therefore means writing one small connector, not new pipeline logic.

The common shape (all keys optional unless marked):

    source_key      str   REQUIRED  which system this came from ('flat_file', 'facebook_ads')
    event_date      any   REQUIRED  the day the interaction happened (date, datetime or a parsable string)
    event_at        any             the exact moment, when the source knows it
    channel_key     str             marketing channel ('facebook', 'google_ads', 'organic'); 'unknown' when absent
    channel_medium  str             paid_social | search | email | organic | referral | unknown
    campaign_key    str             the source's campaign id, else a slug of its name
    campaign_name   str
    creative_key    str             the source's ad / creative id, else a slug of its name
    creative_name   str
    creative_format str
    event_type      str             one of EVENT_TYPES; anything else becomes 'other'
    external_id     str             the source's own id for this record (makes dedupe exact)
    identity_type   str             email | phone | click_id | external_user_id | session_id
    identity_value  str             the raw value; e-mail / phone are hashed here and never stored in the clear
    impressions / clicks / reactions / sessions / conversions   number
    spend / revenue str|number      money in the source's own unit; stored as integer micros
    spend_micros / revenue_micros int   the same, already exact (a strict connector's Decimal arithmetic); wins over spend / revenue
    currency        str             ISO 4217 code of spend / revenue (three letters); DEFAULT_CURRENCY when absent. Never converted.
    natural_key     str             the source's own row identity, when the connector knows it (reported on a collision)
    occurrence      int             0 for the first row with this natural key in the batch, 1 for the second ... (see dedupe_key)
    attrs           dict            everything else the source sent

Run: this module is imported, not executed (see erp/marketing/ingest.py for the entry point).
"""
from __future__ import annotations

import hashlib
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

# The event vocabulary. 'rollup' is the honest word for a row that a source already aggregated
# (a daily CSV line with impressions AND clicks AND conversions is not one interaction, it is a day).
EVENT_TYPES = ("impression", "click", "reaction", "session", "conversion", "rollup", "other")

# Personal identifiers are hashed before they are stored (see db/sql/07_marketing_schema.sql):
# the marketing tables must never become a second copy of the customer list.
HASHED_IDENTITY_TYPES = {"email": "email_sha256", "phone": "phone_sha256"}
IDENTITY_TYPES = ("email_sha256", "phone_sha256", "click_id", "external_user_id", "session_id")

# The documented allowlist behind the scoped GIN index on interaction_fact.attrs_idx.
# Adding a key here means new rows become queryable by it; older rows need a backfill.
# Keep this list SHORT - every key costs index size and write time on every row that has it.
# brand and segment were added with the e-mail connector (a mailing is filtered by them); the fact held no rows then,
# so no backfill was needed and the GIN index (which covers the whole attrs_idx column) did not change.
INDEXED_ATTR_KEYS = ("utm_source", "utm_medium", "utm_campaign", "placement", "device", "country", "brand", "segment")

# Money has a currency and is never converted: 1,000,000 VND is about 35 EUR, so a sum across currencies means nothing.
# The default is what the Channels page always labelled money before the column existed (db/sql/10_marketing_currency.sql).
DEFAULT_CURRENCY = "USD"
GRAINS = ("day", "week")       # the period a row's numbers cover; 'week' rows sit on the week's START date (see attrs)

MICROS = 1_000_000            # money is stored as an integer number of millionths
MAX_ATTR_KEYS = 300           # a record with more keys than this is almost certainly not a record
MAX_TEXT = 200                # the width of the key columns in the schema
MAX_NAME = 300

# Upper bounds for the measure/money columns, which are BIGINT (max ~9.22e18) in
# db/sql/07_marketing_schema.sql. to_count()/to_micros() are otherwise unbounded (a typo'd or corrupted
# CSV cell such as '999999999999999999999999999999' parses to a float far beyond BIGINT range), and a
# value that big only ever fails at the INSERT itself - too late to isolate just that one row from the
# rest of its chunk. Reject it here instead, at the row-level validation stage where L-044 applies, so
# the "one malformed row costs that row, never the batch" promise (pipeline.py, connectors/base.py,
# docs/marketing-data-architecture.md) is actually true for this failure mode, not just documented.
# Both bounds are generous relative to any real value at this project's target scale (Facebook-Ads-scale
# or bigger) and comfortably below BIGINT max, leaving headroom before the column itself would overflow.
MAX_COUNT = 10 ** 12           # 1 trillion of a single event type in one row - already absurd for a count
MAX_MONEY_MICROS = 10 ** 16    # 10 billion currency units of spend/revenue in one row, as integer micros. Raised from 10**15
                               # (1 billion units) once a VND export arrived: 1 billion VND is only about 40,000 USD, so the old bound
                               # was a per-currency assumption. Still 900x below the BIGINT ceiling of the fact column.

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y%m%d", "%d.%m.%Y")


class InvalidRecord(ValueError):
    """One record cannot be turned into an interaction. Counted and skipped - never fatal for the batch (L-044)."""


@dataclass(frozen=True)
class Interaction:
    """One validated, typed interaction: exactly what a fact row holds, before ids are resolved."""
    source_key: str
    event_date: date
    event_type: str
    dedupe_key: uuid.UUID
    channel_key: str
    event_at: datetime | None = None
    channel_medium: str | None = None
    campaign_key: str | None = None
    campaign_name: str | None = None
    creative_key: str | None = None
    creative_name: str | None = None
    creative_format: str | None = None
    identity_type: str | None = None
    identity_value: str | None = None
    external_id: str | None = None
    impressions: int = 0
    clicks: int = 0
    reactions: int = 0
    sessions: int = 0
    conversions: int = 0
    spend_micros: int = 0
    revenue_micros: int = 0
    attrs: dict = field(default_factory=dict)
    attrs_idx: dict = field(default_factory=dict)
    currency: str = DEFAULT_CURRENCY
    natural_key: str | None = None
    occurrence: int = 0


# --------------------------------------------------------------------------- typing helpers
def clean_text(value, limit: int = MAX_TEXT) -> str | None:
    """Anything -> a trimmed one-line string of at most `limit` characters, or None."""
    if value is None:
        return None
    s = str(value).replace("\x00", "").strip()
    if not s or s.lower() in ("none", "null", "nan", "n/a", "-", "--"):
        return None
    s = " ".join(s.split())
    return s[:limit]


def slug(value, limit: int = 80) -> str | None:
    """'Summer Sale 2026!' -> 'summer_sale_2026'. The normal form of every dimension KEY."""
    s = clean_text(value, limit * 4)
    if s is None:
        return None
    s = _SLUG_STRIP.sub("_", s.casefold()).strip("_")
    return s[:limit] or None


_CURRENCY = re.compile(r"[A-Za-z]{3}")


def to_currency(value) -> str | None:
    """'vnd' / ' EUR ' -> 'VND'. None for anything that is not three letters (the caller decides what that means)."""
    s = clean_text(value, 8)
    return s.upper() if s and _CURRENCY.fullmatch(s) else None


def to_date(value) -> date | None:
    """date / datetime / epoch seconds / a string in one of the usual export formats -> date."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        try:                                   # epoch seconds (what an ad API hands you)
            return datetime.fromtimestamp(float(value), tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    s = clean_text(value, 40)
    if not s:
        return None
    head = s.replace("T", " ").split(" ")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def to_datetime(value) -> datetime | None:
    """The same inputs, but keeping the time when there is one. Naive values are read as UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return None                            # a bare date carries no moment - event_date already has it
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = clean_text(value, 64)
    if not s or len(s) <= 10:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_count(value) -> int:
    """A metric cell -> a non-negative whole number. '1,234', '1 234', '12.0', '' and junk are all handled."""
    n = to_number(value)
    if n is None or n < 0:
        return 0
    return int(round(n))


def to_number(value) -> float | None:
    """'$1,234.56' / '1.234,56' / 12 / None -> float or None. Never raises."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    s = clean_text(value, 40)
    if not s:
        return None
    s = s.replace(" ", "").replace(" ", "").replace("'", "")
    # Decide what the separators MEAN before parsing: '1,234' is a thousand, '1234,56' is a decimal comma,
    # '1.234,56' and '1,234.56' are the two ways the world writes the same number.
    if "," in s and "." in s:
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if decimal == "," else ",", "")
        s = s.replace(",", ".") if decimal == "," else s
    elif "," in s:
        head, _, tail = s.rpartition(",")
        if s.count(",") > 1:                   # 1,234,567 - only a thousands separator repeats
            s = s.replace(",", "")
        else:
            s = head + tail if (len(tail) == 3 and tail.isdigit()) else s.replace(",", ".")
    elif s.count(".") > 1:                     # 1.234.567 - dots used as thousands separators
        s = s.replace(".", "")
    m = _NUMBER.search(s)
    if not m:
        return None
    try:
        f = float(m.group(0))
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def to_micros(value) -> int:
    """Money -> integer micros (1.0 -> 1000000). Negative amounts (a refund, a credit) are kept."""
    n = to_number(value)
    if n is None:
        return 0
    return int(round(n * MICROS))


def _exact_micros(mapped: dict, name: str) -> int:
    """`<name>_micros` (an int a strict connector computed with Decimal, no float on the way) wins over `<name>`."""
    exact = mapped.get(f"{name}_micros")
    if isinstance(exact, int) and not isinstance(exact, bool):
        return exact
    return to_micros(mapped.get(name))


def hash_identity(identity_type: str | None, value) -> tuple[str, str] | None:
    """(type, value) -> the pair to store. E-mail / phone are sha256-hashed here and never stored in the clear.

    Returns None when there is nothing usable. An unknown type is kept as 'external_user_id'
    rather than thrown away, because losing the ability to join back to a lead is worse than a loose label.
    """
    v = clean_text(value, 400)
    if not v:
        return None
    t = (clean_text(identity_type, 40) or "external_user_id").casefold()
    if t in HASHED_IDENTITY_TYPES:
        norm = v.casefold().strip() if t == "email" else re.sub(r"[^\d+]", "", v)
        if not norm:
            return None
        return HASHED_IDENTITY_TYPES[t], hashlib.sha256(norm.encode("utf-8")).hexdigest()
    if t not in IDENTITY_TYPES:
        t = "external_user_id"
    return t, v[:MAX_TEXT]


def dedupe_key(source_key: str, external_id: str | None, grain: tuple) -> uuid.UUID:
    """The identity of one interaction, as a UUID (16 bytes in the fact's primary key).

    With an external id from the source the key is exact. Without one it is the GRAIN of the row
    (source, day, channel, campaign, creative, event type, identity), so re-importing the same export
    - or a restated version of it - updates that row instead of adding a second one.

    A source whose export can hold two DIFFERENT rows under one natural key (the ad_performance export has 6 such
    pairs: same week, campaign, adset, ad and device, different metrics) must not go through the grain path: the
    second row would be read as a restatement and silently overwrite the first. Its connector builds an exact
    external id that ends in the OCCURRENCE index of the row within its identical natural key
    (connectors/ad_performance.py, `natural_external_id`), so both rows are kept and a re-import of the same file
    is still idempotent. The currency is deliberately NOT part of any key: a restatement that changes a row's
    currency updates that row instead of adding a second one.
    """
    if external_id:
        raw = f"{source_key}\x1f{external_id}"
    else:
        raw = "\x1f".join([source_key, *("" if p is None else str(p) for p in grain)])
    return uuid.UUID(bytes=hashlib.blake2b(raw.encode("utf-8"), digest_size=16).digest())


def split_attrs(attrs) -> tuple[dict, dict]:
    """(everything, the indexed projection). Keys on INDEXED_ATTR_KEYS are copied - not moved - into attrs_idx.

    The duplication is deliberate: `attrs` stays the complete record of what the source sent, and
    `attrs_idx` stays small enough to GIN-index. It costs a few dozen bytes a row.
    """
    if not isinstance(attrs, dict):
        return {}, {}
    full: dict = {}
    for k, v in list(attrs.items())[:MAX_ATTR_KEYS]:
        key = slug(k, 120)
        if not key or v is None:
            continue
        full[key] = v if isinstance(v, (int, float, bool)) else clean_text(v, 500)
    full = {k: v for k, v in full.items() if v is not None}
    idx = {k: str(full[k]).casefold()[:120] for k in INDEXED_ATTR_KEYS if k in full}
    return full, idx


# --------------------------------------------------------------------------- validation
def build_interaction(mapped: dict) -> Interaction:
    """The common shape (a plain dict from a connector) -> a validated, typed Interaction.

    Raises InvalidRecord with a short, quotable reason. The caller counts it and moves on to the
    next record: one malformed row never ends a batch (lesson L-044).
    """
    if not isinstance(mapped, dict):
        raise InvalidRecord(f"not a record but a {type(mapped).__name__}")

    source_key = slug(mapped.get("source_key"), 60)
    if not source_key:
        raise InvalidRecord("no source_key")

    event_date = to_date(mapped.get("event_date"))
    if event_date is None:
        raise InvalidRecord(f"no usable date (got {clean_text(mapped.get('event_date'), 40)!r})")
    if not date(2000, 1, 1) <= event_date <= date(2100, 1, 1):
        raise InvalidRecord(f"date {event_date.isoformat()} is outside 2000-2100 - almost certainly a parse error")

    event_at = to_datetime(mapped.get("event_at"))
    if event_at is not None and event_at.date() != event_date:
        event_at = None                        # the two disagree: keep the day, drop the unreliable moment

    event_type = (slug(mapped.get("event_type"), 40) or "other")
    if event_type not in EVENT_TYPES:
        event_type = "other"

    channel_key = slug(mapped.get("channel_key"), 80) or "unknown"
    campaign_key = slug(mapped.get("campaign_key"), 200) or slug(mapped.get("campaign_name"), 200)
    creative_key = slug(mapped.get("creative_key"), 200) or slug(mapped.get("creative_name"), 200)
    if creative_key and not campaign_key:
        campaign_key = "unknown"               # a creative always hangs off a campaign in the schema

    if mapped.get("currency") is None or clean_text(mapped.get("currency")) is None:
        currency = DEFAULT_CURRENCY
    else:
        currency = to_currency(mapped.get("currency"))
        if currency is None:
            raise InvalidRecord(f"currency {clean_text(mapped.get('currency'), 20)!r} is not a three-letter ISO 4217 code")
    try:
        occurrence = max(0, int(mapped.get("occurrence") or 0))
    except (TypeError, ValueError):
        raise InvalidRecord("occurrence is not a whole number") from None

    identity = hash_identity(mapped.get("identity_type"), mapped.get("identity_value"))
    external_id = clean_text(mapped.get("external_id"), MAX_TEXT)
    attrs, attrs_idx = split_attrs(mapped.get("attrs"))

    measures = {name: to_count(mapped.get(name))
                for name in ("impressions", "clicks", "reactions", "sessions", "conversions")}
    for name, value in measures.items():
        if value > MAX_COUNT:
            raise InvalidRecord(f"{name}={value} is beyond any real value ({MAX_COUNT}) - "
                                 f"almost certainly a corrupted cell, not a real interaction")
    spend_micros = _exact_micros(mapped, "spend")
    revenue_micros = _exact_micros(mapped, "revenue")
    for name, value in (("spend", spend_micros), ("revenue", revenue_micros)):
        if abs(value) > MAX_MONEY_MICROS:
            raise InvalidRecord(f"{name} is beyond any real value ({MAX_MONEY_MICROS} micros) - "
                                 f"almost certainly a corrupted cell, not a real interaction")
    if not any(measures.values()) and not spend_micros and not revenue_micros and event_type == "other":
        raise InvalidRecord("no event type and no measure - there is nothing to record")

    grain = (event_date.isoformat(), channel_key, campaign_key, creative_key, event_type,
             identity[1] if identity else None)
    return Interaction(
        source_key=source_key, event_date=event_date, event_at=event_at, event_type=event_type,
        dedupe_key=dedupe_key(source_key, external_id, grain),
        channel_key=channel_key, channel_medium=slug(mapped.get("channel_medium"), 40),
        campaign_key=campaign_key, campaign_name=clean_text(mapped.get("campaign_name"), MAX_NAME),
        creative_key=creative_key, creative_name=clean_text(mapped.get("creative_name"), MAX_NAME),
        creative_format=slug(mapped.get("creative_format"), 40),
        identity_type=identity[0] if identity else None, identity_value=identity[1] if identity else None,
        external_id=external_id, spend_micros=spend_micros, revenue_micros=revenue_micros,
        attrs=attrs, attrs_idx=attrs_idx, currency=currency,
        natural_key=clean_text(mapped.get("natural_key"), 400), occurrence=occurrence, **measures,
    )
