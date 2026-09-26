# Marketing / channel interaction data — architecture (Phases 1 and 4, plus the real-file work of section 9)

Status: **Phases 1 and 4 of 4 are built** — schema + ingestion pipeline + scale verification (Phase 1, sections
1-7) and the daily rollup layer + partition maintenance (Phase 4, section 8). The ERP Desk report page (Phase 3, the Channels
page, `desktop/channels_data.py`) is built on top of them. **Section 9** records what it took to load the owner's two real export
files correctly (a currency on every money row, duplicate natural keys kept and reported, weekly grain, two strict source-specific
connectors) and what was verified on them. Still not built: any real external API connector (Phase 2). Nothing schedules the ingest or the rollup refresh
(Task Scheduler is the owner's). This document
explains the design, why it is shaped this way, what was verified at scale and the real numbers, and the
boundary of each phase. See `README.md` ("Marketing / channel interaction data") for how to run it and
`PROJECT_NOTES.md` §5 for where it sits among the other tables.

## 1. Why a star schema, not one wide table and not pure EAV

The requirement is to hold clicks, reactions, sessions, traffic and conversions from many heterogeneous
sources — Facebook-Ads-scale or bigger, potentially 100+ attributes per source, at a few-million-row scale
in the largest table. Three shapes were on the table:

- **One wide table** (a column per possible attribute across every source) does not survive a second
  source: Facebook's ~100 columns and Google Ads' own ~100 columns barely overlap, so the table either
  grows a new column for every source forever, or every source leaves most columns `NULL`. Neither serves
  "add a source" as a small, safe change.
- **Pure EAV** (`interaction_id, attribute_name, attribute_value` rows) is maximally flexible but wrong for
  this project's own stated access pattern: every report page in this codebase (`desktop/*_data.py`) filters
  by a date range and a channel, then aggregates a handful of measures. EAV turns that one-row-per-interaction
  aggregate into a self-join or a pivot per query, which is exactly the kind of query this project's read-only
  timeout/single-flight pattern (`erp/db.read_connect`, `desktop/today_data.arm_timeouts`) is built to keep
  fast and boundable — EAV fights that goal at the row counts this phase targets.
- **A star schema** keeps the two real needs separate: a narrow **fact** table with the handful of measures
  every source can share (impressions, clicks, reactions, sessions, conversions, spend, revenue) plus a JSONB
  `attrs` column for the rest, and small **dimension** tables for the things that repeat (channel, campaign,
  creative, source system, identity). This is the shape the existing `leads`/`v_leads_summary` reporting
  already assumes (a fact-ish table plus small reference data), so a channel-performance report page (Phase 3)
  reads the same way analysts already read the rest of this project.

## 2. The three layers

```
raw export / API  →  marketing_landing (raw, append-only)  →  validate/type/dedupe  →  dimensions + interaction_fact
                                                                                              ↑
                                                                                     marketing_identity.lead_id
                                                                                     (points at the EXISTING leads table)
```

1. **Landing** (`marketing_landing`) — append-only, one row per record exactly as the connector received it
   (`payload JSONB` + `source`, `external_id`, `event_type`, `received_at`). This mirrors the project's own
   `leads.raw_payload` pattern. If a mapping turns out wrong, the fact rows of a batch can be rebuilt from
   here without going back to the external API. `marketing_ingest_run` is the companion journal: one row per
   batch (read/landed/invalid/duplicate/loaded counts, status, first error) so "did it run, and what
   happened" is a five-row query instead of a scan over millions of fact rows. Caveat: land and load share one transaction, so a row isolated at the SQL stage
   rolls back with its landing row and is not kept; it is only counted and its reason logged.
2. **Dimensions** (`marketing_source`, `marketing_channel`, `marketing_campaign`, `marketing_creative`,
   `marketing_identity`) — small, slowly-changing (SCD type 1: updated in place, `first_seen_at`/
   `last_seen_at` tracked). `marketing_identity` is the **one** place that knows about this project's existing
   identities: it `REFERENCES leads(id)` instead of copying a name/e-mail/phone, and a personal identifier
   (e-mail, phone) is stored as a **sha256 hash**, computed by the pipeline, never the raw value — this table
   can never become a second copy of the customer list. `erp.marketing.pipeline.Pipeline._resolve_leads()`
   matches a chunk's identities against `leads` by the same hash, so an interaction is linked to a known lead
   only when it can be proven, not guessed.
3. **Fact** (`interaction_fact`) — one row per interaction. Deliberately few columns: a measure gets a column
   only when *every* channel can mean something by it. Everything source-specific goes into `attrs JSONB`.
   No surrogate `id`: the natural key `(event_date, dedupe_key)` is the primary key (required anyway — a
   partitioned table's primary key must contain the partition column), so a `BIGSERIAL` cost (8 bytes/row +
   sequence traffic on every bulk load) is avoided and nothing needs to reference the fact by a synthetic id.
   Money is stored as integer **micros** (1.00 = 1,000,000), never float, so aggregation never drifts, and every row carries its
   **currency** (`db/sql/10_marketing_currency.sql`): money is never converted and never added across currencies (section 9.1).

## 3. Partitioning and indexes — and why

`interaction_fact` is **`PARTITION BY RANGE (event_date)`**, monthly, created lazily by the pipeline
(`erp.marketing.schema.ensure_partitions`) right before a batch that needs a new month is loaded. Every
report query this project's other pages already run starts with a date range, so monthly partitioning turns
that range filter into **partition pruning**: a report for "the last 30 days" touches 1–2 partitions, never
all of them, regardless of how many months of history accumulate. There is deliberately **no DEFAULT
partition** — a default partition has to be scanned by any query the planner cannot prove excludes it, which
is exactly the pruning this design exists for; a row with no partition raises an error instead of silently
landing somewhere that slows every future report, and the pipeline creates the partitions a batch needs
before writing to it, so a real load never hits that error mid-batch.

Indexes on the fact (they propagate to every partition automatically):

| Index | Why |
|---|---|
| `btree (channel_id, event_date)` | The filter every report page in this project starts with: a channel (or a few, `= ANY(...)`) inside a date range. Channel first (equality), date second (range). |
| `btree (source_id, event_date)` | The same question per source system ("what did the Facebook connector bring in?"). |
| `btree (identity_id) WHERE identity_id IS NOT NULL` | "Every interaction of this lead" (a drill-down). Partial: most interactions are anonymous, so a full index would mostly index `NULL`. |
| `BRIN (loaded_at)` | Rows are appended in load order, so a BRIN over the append-only load timestamp answers "what did the last ingest add" for a few dozen kB per partition instead of a multi-MB btree. |
| `GIN (attrs_idx jsonb_path_ops)` | See below — a **scoped** GIN, not one over the whole `attrs` blob. |

**The scoped GIN, and why it is scoped.** `attrs` is the complete, source-specific blob and can hold 100+
keys per row (a real Facebook Ads export). GIN-indexing all of it would index every key of every row: large,
slow to write, and almost none of it ever queried. `attrs_idx` is a **second, small JSONB column** the
pipeline writes alongside `attrs`, containing **only** the keys on a documented allowlist —
`INDEXED_ATTR_KEYS` in `erp/marketing/model.py`: `utm_source, utm_medium, utm_campaign, placement, device,
country, brand, segment`. The first six were chosen because they are the cross-source "slice by" attributes a channel-performance
report would filter on, they exist (under some spelling) in nearly every ad/analytics export, and none of
them deserves its own column yet because they are sparse and still source-specific. `jsonb_path_ops` (not the
default `jsonb_ops`) is used because it is about half the size and supports exactly the query this is for —
containment (`attrs_idx @> '{"device": "mobile"}'`). Anything in `attrs` outside the allowlist is still fully
readable, just not indexed — an honest trade stated here and in the DDL: a filter on an unlisted key costs a
date-range scan, not a table scan, and never a silent full-table scan. To add a key later: add it to
`INDEXED_ATTR_KEYS`, backfill `attrs_idx` for the partitions that need it, and update this table.

**The `leads` fix, while in this area.** Every Leads/Sources/Today query in this project (`desktop/today_data.py`,
`leads_data.py`, `sources_data.py`) filters `leads` by a date range and/or `source`, yet `leads` had indexes
only on `email` and `phone`. `db/sql/08_leads_indexes.sql` adds `idx_leads_created_at (created_at DESC)` and
`idx_leads_source_created_at (source, created_at DESC)` — additive, changes no query's result, only the plan
the planner may choose. At today's row count PostgreSQL still (correctly) prefers a sequential scan; the
point of these indexes is the shape `leads` is growing into, proven at 1,000,000 rows below.

## 4. The connector pattern — how to add a new source

```
   any source's raw records  →  Connector.map_record()  →  common intermediate shape  →  shared pipeline
                                 (the ONE thing you write)     (erp/marketing/model.py)     (never touched again)
```

A `Connector` (`erp/marketing/connectors/base.py`) implements exactly two methods:

- `records()` — yield the source's raw records, one dict each, in whatever shape the source uses. They are
  landed unchanged.
- `map_record(raw)` — turn **one** raw record into the pipeline's common intermediate shape (a plain dict:
  `source_key`, `event_date`, `channel_key`, `event_type`, the measure names, `attrs`, …; see the module
  docstring of `erp/marketing/model.py` for the full list). Returning `None` says "not an interaction" (a
  header row, a totals line).

Everything after that — typing/coercion, validation, dedupe-key construction, dimension upserts, partition
creation, bulk loading, the ingest journal — is the **shared** pipeline (`erp/marketing/pipeline.py`) and is
never reimplemented per source. Adding a real source later (Phase 2: Facebook Ads, Google Ads, GA4, …) means
writing one small connector module that maps that API's own JSON shape to the common shape — not new pipeline
logic, not a new table, not a new loader.

**The first connector built** (two strict, source-specific ones followed in section 9: `ad_performance` and `email_campaign`) is a generic, source-agnostic **flat-file / CSV** connector
(`erp/marketing/connectors/flat_file.py`). It exists to prove the pattern end to end without inventing a
fake Facebook-specific mapping that would just be guesswork until the owner's real API credentials arrive
(Phase 2). It recognises columns by the **slug of their header** (`"Amount spent (USD)"` → `amount_spent_usd`),
first match wins: a date, a channel/platform name, an event/action, a campaign, a creative, the usual metric
names (impressions, clicks, reactions, sessions, conversions, spend, revenue), and anything that looks like a
person (e-mail, phone, click id, user id, session id). It decides the event type honestly: a row carrying
several metric columns at once is labelled `rollup` (a day a source already pre-aggregated, not one
interaction), a row with exactly one metric column is that metric's own event. Every column it does not
recognise is kept in `attrs` — nothing is ever silently dropped.

**Validation and malformed rows (lesson L-044) — two stages, both row-level.** `erp/marketing/model.build_interaction()`
types and validates one mapped record and raises `InvalidRecord` with a short, quotable reason when it cannot
become an interaction: no usable date, a date outside 2000–2100, nothing to record, or — the gap a review round
found and closed — a measure or money value whose magnitude is beyond any real interaction (`MAX_COUNT` /
`MAX_MONEY_MICROS`, comfortably below the fact table's `BIGINT` columns). The pipeline's `_validate()` and
`_load_chunk()` catch this — and any exception a connector itself raises on one record — count it, keep a
sample of the reason, and **move on to the next record**.

That bound catches the common cause (a corrupted CSV cell) before it ever reaches SQL, but Python validation
cannot anticipate every way a *type-correct* value can still fail at the database. So there is a second,
SQL-level stage: `Pipeline._load_chunk_isolating()` runs the chunk's bulk load inside one transaction as
before, but if that transaction itself raises, it does not discard the whole chunk — it **bisects** the chunk
and retries each half, recursing until either a half succeeds (and its rows are counted normally) or it is
down to the single offending row (counted invalid, its database error kept as the reason). Concretely:
before this existed, one CSV cell whose number overflowed `BIGINT` failed the whole chunk's `INSERT` and
discarded every other row in it — up to `MARKETING_BATCH_SIZE` (5,000 by default) good rows lost to one bad
cell. The one exception is a **broken database connection**, which is not a per-row problem bisection can
fix: `_is_connection_error()` recognises it (`psycopg2.OperationalError`/`InterfaceError`) and lets it
propagate, ending the run outright rather than replaying the same outage down to thousands of single-row
retries. Two more guards stop a **systematic** SQL failure (missing privilege, missing partition, schema
mismatch), which fails every row and would otherwise cost about 2N transactions per chunk, for every chunk:
a per-run budget of isolated rows (`MARKETING_MAX_ISOLATED_ROWS`, default 50), and a streak of 8 single-row
failures that repeat their whole chunk's exact error with no success anywhere in that chunk. Either one
aborts the run with status `failed` and a reason naming the cause; per-row warnings are logged only for the
first 10 rows. `_is_connection_error()` also unwraps SQLAlchemy's `DBAPIError` (as raised by
`conn.execute(text(...))`), so a dead connection is recognised however the call was made. Either way, **a malformed or SQL-rejected row costs that row, never the batch** — provably so: a
pure scenario table in `tests/marketing_scenarios.py` covers both stages (typing edge cases, thousands/decimal
separators in different locales, a connector that raises mid-batch, a record that maps to `None`, a record
that fails validation, the numeric-overflow bound, and — with `_load_chunk` monkeypatched so no database is
needed — a simulated SQL-level failure on one row of a chunk, a chunk that is entirely bad, and a simulated
dropped connection) — see §6 for what is regression-tested versus one-off.

**Dedupe.** `erp/marketing/model.dedupe_key()` turns a record into a stable UUID: exact (from the source's own
`external_id`) when one exists, otherwise a hash of the record's **grain** (source, day, channel, campaign,
creative, event type, identity) — so re-importing the same export, or a restated version of it, updates that
row instead of duplicating it. Dedupe happens twice: inside a chunk (in Python, counted), and across chunks
and across runs (in the database: the fact's primary key is `(event_date, dedupe_key)` and the load is
`INSERT ... ON CONFLICT (event_date, dedupe_key) DO UPDATE`).

**Loading is bulk, never row-by-row.** Every write in the pipeline (landing, each dimension, the fact) is one
`psycopg2.extras.execute_values` call per chunk (default 5,000 records, `MARKETING_BATCH_SIZE`), which itself
batches into pages of 1,000 rows per actual `INSERT` statement — never a Python loop issuing one `INSERT` per
record.

## 5. What was verified at scale, and the real numbers

Run with `erp/marketing/perf_check.py`: builds synthetic data in a **throwaway schema** (`perf_test`, name
enforced to match `perf_[a-z0-9_]+` so it can never be `public`), runs real `EXPLAIN (ANALYZE, BUFFERS)` on
the query shapes this project's report pages would actually run, and drops the schema again (even on
failure/interrupt). It never reads, writes or alters anything in `public`. Machine: the owner's own dev
machine, PostgreSQL 18.3, `erp_support`. Real, complete run, 2026-09-24: **431 s total** (`venv\Scripts\python.exe
-u -m erp.marketing.perf_check --yes`, default sizes: 3,000,000 fact rows, 1,000,000 leads, 12 months), throwaway
schema dropped cleanly at the end (disk back to 5.8 GB free). The numbers below are copied verbatim from that run;
re-run the command yourself (`--out <file>` saves the full plans) to reproduce or update them.

### 5.1 `leads` at 1,000,000 rows — do the two new indexes get used?

1,000,000 synthetic leads built in 10.4 s; both new indexes built on top of them in 3.3 s.

| Query (the exact shape a report page runs) | Before | After | Plan after |
|---|---:|---:|---|
| Today: "new leads today" (`created_at >= today`) | 363.97 ms | **19.26 ms** (19x) | `Index Only Scan` on `idx_leads_created_at` |
| Leads page: one page of a 30-day range, `ORDER BY created_at DESC LIMIT 25` | 208.13 ms | **0.08 ms** (~2,600x) | `Index Scan` on `idx_leads_created_at` (the `LIMIT` only walks the index tail) |
| Sources tab: `source = ANY('{facebook,google_ads}')` + 30-day range, `GROUP BY source` | 139.37 ms | 135.10 ms (unchanged) | still `Parallel Seq Scan` |
| Leads page shape: the same 30-day filter inside a CTE over `v_leads_summary`, a bare `count(*)` | 184.29 ms | 206.45 ms (unchanged, within noise) | still `Parallel Seq Scan` |

Honest reading: the two indexes are a clear, large win for the queries this project's UI actually runs on a
landing page or a paged table (a narrow date filter, or a date filter plus a small `LIMIT`) — both switched
from a sequential scan to an index scan and got one to three orders of magnitude faster. They did **not**
change the plan for the other two shapes: PostgreSQL's planner correctly judged that reading ~8% of a
1,000,000-row table (a 30-day slice of a 360-day spread) via a non-covering index is not obviously cheaper
than one sequential pass, so it kept the sequential scan for an aggregate that still has to touch a large
fraction of the table. This is not a defect in the indexes — it is the planner doing its job — and it is
reported here rather than only showing the queries that improved, per this project's own truthfulness rule
(a UI/document must never claim more than reality).

### 5.2 `interaction_fact` at 3,019,960 rows — partition pruning and index usage

DDL installed in 0.2 s (8/8 tables present); 13 monthly partitions created by the pipeline's own
`ensure_partitions()`; 400,000 landing rows and 3,000,000 fact rows built (~25 s/month, ~298 s total for the
fact table); `ANALYZE` over the whole schema: 70.2 s. On-disk size of the whole schema: **2,842 MB** (`leads`
itself 236 MB; each monthly `interaction_fact` partition, fully indexed, 164 MB — the 13 partitions and their
indexes are the great majority of the 2.8 GB).

| Query shape | Time | Plan | Fact partitions touched (of 13) |
|---|---:|---|---:|
| Date range (30 days) + channel filter — the filter every report page starts with | 420.9 ms | `Bitmap Heap/Index Scan` on `idx_interaction_fact_channel_date` | **2** |
| Rollup: per channel per day over the same range (a channel report's chart) | 245.0 ms | same index, `GroupAggregate` | **2** |
| One day, one channel (the narrowest drill-down slice) | 15.1 ms | `Bitmap Index Scan`, same index | **1** |
| Scoped GIN: an indexed `attrs_idx` key inside the date range | 292.6 ms | `Bitmap Index Scan` on the GIN index (`..._attrs_idx_idx`) | **2** |
| An `attrs` key that is **not** on the GIN allowlist, same date range | 168.8 ms | `Parallel Seq Scan` (no index can help) | **2** |
| Drill-down: every interaction of one known lead, no date filter at all | 1.4 ms exec / 57.4 ms **planning** | `Index Scan` on `idx_interaction_fact_identity`, `Nested Loop` | **13** (all — nothing to prune with) |
| BRIN: what landed in `marketing_landing` in the last 2 days | 3.9 ms | `Bitmap Index Scan` on the BRIN index | n/a (landing, not the fact) |
| **Full-table rollup with NO date filter** — what a report must never do at this size | **3,363.6 ms** | `Parallel Seq Scan` over every partition | **13 (all)** |

**Partition pruning is proven, not just present**: every query that filters by date touches only the 1–2
partitions its range actually needs, out of 13 — regardless of whether it also filters by channel. The one
query with nothing to prune (a lead's whole interaction history) correctly falls back to scanning all 13
partitions, and its 57 ms of *planning* time (versus 1.4 ms to *execute*) is the honest, visible cost of a
partitioned table when a query cannot exclude any partition: the planner has to consider each one. The
**full-table rollup is 3.36 seconds** — this project's own worst case, included on purpose (see §7, the Phase 4
rollup this number motivates) — versus 245–421 ms for the same kind of aggregate once a date range prunes the
partitions down to two. One honest surprise: the **scoped GIN query (293 ms) was not faster than the
unindexed-key query (169 ms)** in this run, because both are already confined by partition pruning to the same
two ~730,000-row partitions, where a parallel sequential scan of that much smaller slice is competitive with a
GIN bitmap lookup; the GIN index's real payoff is avoiding that same lookup degrading into the 3.36-second
full-table case once a query has no date range to prune with, or at a scope larger than two partitions — not a
uniform win at every scope, and this document says so rather than only reporting the case that flatters the
design.

### 5.3 The real Python pipeline, end to end

20,000 synthetic CSV rows (with 40 deliberately malformed, 0.2%) pushed through the actual
`FlatFileConnector` + `Pipeline`, twice:

```
first load : 20000 read, 20000 landed, 40 invalid, 0 duplicate in batch, 19960 written (19960 new, 0 restated) in 11.5s
throughput : 1,728 fact rows/s end to end (read CSV -> land -> validate -> dedupe -> load)
same file again: 20000 read, 20000 landed, 40 invalid, 0 duplicate in batch, 19960 written (0 new, 19960 restated) in 9.0s
-> dedupe holds: 0 NEW rows on the second load (19960 restated in place)
-> malformed rows: 40 of 20000 skipped and counted, batch still ok
```

This proves, end to end and against the real database (not a mock): the connector pattern (§4) really produces
loadable rows; malformed rows are counted and skipped, never fatal (0.2% rejected, the batch still finished
`ok`); and re-importing the identical export is idempotent — the second run wrote the **same** 19,960 rows
back as restatements, zero new ones, proving the dedupe key (§4) does what it claims across separate runs, not
only inside one batch. Final row counts (`SELECT count(*)`, section 5 of the run): `leads` 1,000,000,
`marketing_landing` 440,000, `interaction_fact` 3,019,960, `marketing_channel` 13, `marketing_campaign` 700,
`marketing_creative` 2,600, `marketing_identity` 100,000, `marketing_ingest_run` 2.

### 5.4 A bug this verification actually caught

Running the real pipeline against the real, **partitioned** `interaction_fact` table (not the pure-Python
scenario table, which never executes SQL) surfaced a genuine defect that no amount of unit testing would have
caught: the original `Pipeline._load_fact()` used `INSERT ... ON CONFLICT DO UPDATE ... RETURNING (xmax = 0)
AS inserted` to tell a freshly-inserted row from a restated one. PostgreSQL refuses to return a system column
(`xmax`) from an `INSERT` whose target is a **partitioned** table ("cannot retrieve a system column in this
context") — the parent relation has no storage of its own, so there is no `xmax` to read from it; only the
child partition the row actually lands in has one. This made every real load into the partitioned fact table
fail outright. The fix replaces the `xmax` trick with a bulk, pre-insert lookup of which `(event_date,
dedupe_key)` pairs already exist (`Pipeline._existing_fact_keys()`) — one extra bulk `SELECT`, still never
row-by-row, and it works identically on a partitioned or unpartitioned target. The numbers in §5.3 are from the
run made **after** this fix (confirmed first on a small isolated throwaway schema, then in the full run
reported here: two loads, 19,960 rows each, "N new, M restated" reported correctly both times). This is exactly
the argument for running the scale verification against the real partitioned schema instead of trusting a
smaller, unpartitioned dev copy: the bug was invisible at zero rows and would have been invisible in the
pure-Python scenario table forever.

## 6. What is one-off verification versus permanent regression coverage

- **Permanent, run every gate cycle** (`tests/marketing_scenarios.py`, `tests/smoke.py` check 4): the
  pipeline's pure logic — typing/coercion (dates in several formats, thousands vs. decimal separators, money
  to integer micros), validation and its exact rejection reasons, malformed-row handling (a connector that
  raises, a record that maps to `None`, a record that fails validation — L-044), dedupe-key stability and
  uniqueness, the scoped GIN allowlist (`split_attrs`), the flat-file connector's column recognition and
  rollup-vs-single-metric decision, the schema helpers (partition naming, month arithmetic including
  December, the identifier guard), the numeric-overflow bound (`MAX_COUNT`/`MAX_MONEY_MICROS`) that rejects a
  corrupted cell before it reaches SQL, the SQL-level bisection that isolates a row which fails only at the
  database (simulated with `_load_chunk` monkeypatched — no real database involved), and that the DDL file
  and the Python that writes into it have not drifted apart (column names, index definitions, the GIN
  allowlist named in both places). No database, costs a fraction of a second (see `tests/marketing_scenarios.py`
  for the current count, which changes as scenarios are added — not restated here per lesson L-032).
- **One-off, reported here, re-run by hand when the schema or the pipeline changes**
  (`erp/marketing/perf_check.py`): the few-million-row scale build, the real `EXPLAIN ANALYZE` plans and
  timings, the `leads` before/after index comparison, and the real end-to-end pipeline throughput. It costs
  several minutes and a few GB of disk (dropped again at the end), so it is not part of the merge gate; the
  numbers above are this project's record of the last real run.
- **Phase 4.** Permanent: the day-window arithmetic, range planning (changed days merged, split at months, an
  open-ended window), the full-rebuild ranges, the partition look-ahead and month math, the retention-candidate
  selection, the exact detach/drop command text, a structural guard that `partitions.py` executes no `DROP` or
  `DETACH`, the CLI's argument handling and exit codes (failing steps, missing tables, a swallowed `MemoryError`), and
  that the rollup DDL and the job agree (`tests/marketing_scenarios.py`). One-off, numbers in §8.5: the 3M-row build,
  the `EXPLAIN ANALYZE` comparison, the incremental-touch proof, idempotency and correctness against a direct
  `GROUP BY` (`erp/marketing/rollup_check.py`, a throwaway `perf_rollup` schema).

## 7. Explicitly out of scope for Phase 1 (Phase 4 has since been built, section 8)

- **Phase 2 — a real connector.** The only connector in this phase is the generic flat-file/CSV reader
  (§4). A real API connector (Facebook Ads, Google Ads, GA4, …) needs the owner's own API credentials and is
  a small, separate module that maps that source's JSON shape to the same common intermediate shape — no
  change to the pipeline, the schema, or this document's design.
- **Phase 3 — an ERP Desk "channel performance" report page.** Built after Phase 4 as ERP Desk's Channels page (`Ctrl+7`,
  `desktop/channels_data.py`): it reads only the two rollup tables and never `interaction_fact` (§8.7). Original note: this phase
  deliberately stopped at the database and the pipeline. The Data Flow map
  (`desktop/flow_definition.py`) shows the ingest node honestly as **manual** — nothing arms it automatically
  today, since Phase 2's real connector (a scheduled pull, a webhook) does not exist yet (lesson L-030).
- **Phase 4 — a rollup layer.** §5's "full-table rollup with no date filter" number (3.36 s) is the honest worst
  case Phase 1 did not try to make fast. Phase 4 built it: a daily rollup table, an incremental refresh job and
  partition maintenance, with real numbers. See §8 (this bullet used to say "designed, deliberately not built").

### Run status

`marketing_ingest_run.status` is `ok` (nothing rejected at load), `partial` (some rows were loaded and a few
were isolated at the SQL stage; validation-stage rejections alone do not make a run partial), or `failed`
(nothing was loaded, or the run aborted as described above). Note that a row isolated at the SQL stage also
loses its landing row, because landing and loading share one transaction; only its count and error remain.

## 8. Phase 4 — the rollup layer and partition maintenance (built)

Status: **built and verified at 3,000,000 fact rows in a throwaway schema; not installed in `public`, not scheduled.**
`db/sql/09_marketing_rollup.sql` is additive and has to be run by the owner (it needs 07 first); nothing in the
project schedules the refresh, because registering a Task Scheduler entry is a system setting only the owner changes.
The Data Flow map shows the node as "recommended, not set up" and never armed (lesson L-030).

### 8.1 What exists

| Object | Grain | Purpose |
|---|---|---|
| `interaction_daily_rollup` | one row per **day x channel x campaign x currency x grain** (`campaign_id` 0 = the day's facts had no campaign; currency and grain: section 9) | the campaign drill-down, "top campaigns", any slice that needs a campaign |
| `interaction_daily_channel_rollup` | one row per **day x channel x currency x grain**, summed from the campaign rows by the same refresh in the same transaction | the channel-performance chart and headline tiles, "all history" queries: about 4,000 rows per year for 12 channels |
| `marketing_rollup_run` | one row per run | did it run, which ranges, how many rows, the error if it failed; also the **watermark** for changed-day detection |

Measures (both rollup tables carry the same columns, all additive): `events_total` and the per-type row counts
`events_impression / click / reaction / session / conversion / other` (`other` = every event type that is not one of
the five, so the parts always add up to the total), and the sums of the fact measures `impressions, clicks, reactions,
sessions, conversions, spend_micros, revenue_micros`, plus `refreshed_at`. The event counts (fact *rows* by type) and
the measures (numbers carried *on* the rows) are different things on purpose: a daily export row of type `rollup`
carries 40 clicks in one row.

**Derived rates are never stored.** A stored CTR is an average of averages and is wrong the moment two rows are
combined (a week, a month, a channel). Every ratio is `sum(numerator) / nullif(sum(denominator), 0)` computed by the
report query over whatever rows it selected:

```sql
SELECT channel_id, currency, sum(impressions) AS impressions, sum(clicks) AS clicks, sum(conversions) AS conversions,
       sum(spend_micros) / 1000000.0                       AS spend,
       sum(clicks)::numeric      / nullif(sum(impressions), 0) AS ctr,
       sum(spend_micros) / 1000000.0 / nullif(sum(clicks), 0)  AS cpc,
       sum(conversions)::numeric / nullif(sum(clicks), 0)      AS cvr
  FROM interaction_daily_channel_rollup
 WHERE event_date >= :from AND event_date < :to AND channel_id = ANY(:channels)
 GROUP BY channel_id, currency;      -- money is summed per currency, never across them (section 9.1)
```

The unique key (primary key) is `(event_date, channel_id, campaign_id, currency, grain)` / `(event_date, channel_id, currency, grain)`; the
secondary index `(channel_id, event_date)` on both serves "one channel over a date range" (the primary key already
serves "every channel over a date range"). `campaign_id` is `NOT NULL` with 0 for "none" because a nullable column
cannot be in a primary key and a `UNIQUE` index treats NULLs as distinct, which would break idempotency. There is no
foreign key from the rollup to the dimensions: the values come straight from the fact (which has them) and a
per-row FK check on a table that is rewritten daily protects nothing.

### 8.2 Table, not a materialized view — the decision

A **real table refreshed incrementally by day range** was chosen over a materialized view:

- `REFRESH MATERIALIZED VIEW` re-runs the whole defining query over **every partition, every time**. At 3M rows that
  is the same scan as the 1.9 – 2.8 s baseline, repeated on every refresh, and it grows linearly with history.
  `REFRESH ... CONCURRENTLY` avoids blocking readers but still recomputes everything and then diffs it (and needs a
  unique index), so it is no cheaper.
- PostgreSQL has no incremental materialized view. A table lets the job recompute only the days that need it:
  `DELETE` the date range + `INSERT ... SELECT ... GROUP BY` for the same range, inside one transaction, so a reader
  sees the old rows or the new rows of that range and never a half-empty one. Both statements carry the date bound,
  so partition pruning confines the fact read to the partitions of that range.
- The unique key makes the refresh idempotent (a second run rewrites identical numbers) and lets a future
  `INSERT ... ON CONFLICT` style refresh replace the delete/insert without a schema change.
- A table **outlives a dropped fact partition**. Retention (8.4) removes old raw rows; the daily summary of those
  days stays, which a materialized view over the fact could not do.
- What the table costs: a job to run and a journal to read. That is the trade, and it is small.

A materialized view would win for a tiny fact table where a full recompute takes milliseconds, or where nobody can
be trusted to schedule a job. Neither applies here.

### 8.3 The refresh (`python -m erp.marketing.rollup`)

A run does three independent steps, each guarded on its own (one failing does not stop the next; the exit code says so):
partitions -> rollup -> retention report.

**What the rollup step recomputes**

1. The last `--days N` days (default 3), today included, **open ended** (everything from the window start onwards, so a
   future-dated row is covered too).
2. Any **older day whose facts were loaded or restated since the last successful run.** The pipeline's
   `ON CONFLICT DO UPDATE` sets `loaded_at = now()` on a restated row, so "which days changed" is
   `SELECT DISTINCT event_date FROM interaction_fact WHERE loaded_at >= <watermark>`, served by the BRIN index on
   `loaded_at`. The watermark is the start of the newest good run of the journal **minus one hour**, because a load
   that started before the previous run but committed after it carries an older `loaded_at`. With no earlier run, every
   day with facts counts as changed, so the very first plain run is a correct initial build (a `--full --yes` is the
   explicit way to say so). Changed days are merged into contiguous ranges and split at month boundaries, so no
   transaction spans more than one monthly partition.
3. `--window-only` skips (2) and does not move the watermark, so it can never hide a change from the next normal run.
4. `--full --yes` rebuilds **every month that still has facts**, first fact day onwards. It is refused without `--yes`
   (the job never prompts), logs a WARNING line, and never deletes rollup days *before* the oldest fact, which are the
   history of partitions dropped for retention. On an empty fact table it does nothing and says so.

Each range: `pg_advisory_xact_lock` per schema (two refreshes queue instead of deleting each other's rows), then the
timeouts are re-armed (`lock_timeout` 60 s, `statement_timeout` 30 min; `SET LOCAL` dies with its transaction, L-090),
then delete + insert + the channel level rebuilt from the campaign rows just written.

**Known limits of the change detection.** A *deleted* fact row does not bump `loaded_at`, so a day that lost rows is not
noticed until it falls in a `--days` window or a `--full` run. Nothing in this project deletes fact rows (retention drops
whole partitions, and the rollup keeps their days). The BRIN on `loaded_at` only helps once its ranges are
**summarised**, which `VACUUM` (autovacuum does it on a real table) does: see the numbers below.

Logging and exit codes follow the unattended-job rules (L-013): everything is caught at the top, output goes to the
console and `marketing_rollup.log`, exit **0** = every step finished, **1** = a step failed, **2** = bad arguments; no
prompt anywhere. `MARKETING_SCHEMA` / `--schema` select the schema, so the same code ran in a throwaway `perf_rollup`.
"Today" is the machine's local date; `event_date` is the source's own day (lesson L-097: the session timezone is
Asia/Bangkok), so a source that reports in another zone may put its newest day a day ahead or behind - the changed-day
detection and the open-ended window absorb that.

### 8.4 Partition maintenance and retention (`erp/marketing/partitions.py`)

- **Look-ahead.** Every run creates the current month and the next `MARKETING_PARTITIONS_AHEAD` (default 3, `--partitions-ahead`,
  cap 60) monthly partitions of `interaction_fact`, so a scheduled load never fails with "no partition of relation
  found" (there is deliberately no DEFAULT partition, see 07). Idempotent and additive (an empty partition is free).
- **Retention report, never action.** A monthly partition lying **entirely before** the cutoff is a *candidate*; the
  cutoff is the first day of the month `MARKETING_RETENTION_MONTHS` (default 24, `--retention-months`) before the
  current month, so with 24 months on 2026-09-25 the current month and the 24 full months before it are kept and
  everything before 2024-09-01 is listed. For each candidate the job prints the row estimate, the size and the exact
  commands, for example:

  ```sql
  ALTER TABLE public.interaction_fact DETACH PARTITION public.interaction_fact_2024_07 CONCURRENTLY;
  DROP TABLE public.interaction_fact_2024_07;   -- only after the detached table has been archived or you accept losing it
  ```

  **The module has no code path that executes a `DETACH` or a `DROP`** (a scenario in `tests/marketing_scenarios.py`
  parses it and asserts that no statement it executes contains either word). Detaching or dropping data is the
  owner's decision; the recommended order is detach, archive the detached table (`pg_dump -t`), then drop.
- The rollup rows of a dropped partition's days are kept (8.2), and `--full` leaves them alone.

### 8.5 The real numbers

`erp/marketing/rollup_check.py --yes`, 2026-09-25, the owner's dev machine, PostgreSQL 18.3, throwaway schema
`perf_rollup` (dropped afterwards; nothing in `public` was read, written or altered; free disk back to 6.3 GB).
**3,000,000** synthetic fact rows over 16 monthly partitions (12 loaded), 12 channels, 400 campaigns, whole run 779 s
(the first attempt of the same script, before the BRIN finding below: 891 s and 1,079 s). Each query was run once to
warm the cache and the second `EXPLAIN (ANALYZE, BUFFERS)` timing is reported.

**The report queries: raw fact versus rollup**

| Query shape | Raw `interaction_fact` | Campaign rollup (273,600 rows) | Channel rollup (2,928 rows) |
|---|---:|---:|---:|
| Phase 1 baseline, verbatim: full table, no date filter, per channel `sum(clicks)` | **2,793 ms** (16 partitions, parallel seq scan) | 76.0 ms (37x) | **0.8 ms** (about 3,400x) |
| Full range, per channel totals + CTR / CPC / CVR at read time | 1,889 ms | 148.4 ms (13x) | 3.2 ms (590x) |
| Full range, per channel per day (the whole-history trend chart) | 2,234 ms | 98.0 ms (23x) | 8.9 ms (250x) |
| 30-day range + 3 channels, per channel per day | 249.2 ms (2 partitions) | 4.6 ms (54x) | 0.2 ms (about 1,250x) |
| Top 10 campaigns by clicks, 30-day range | 220.5 ms | 31.3 ms (7x) | n/a (needs the campaign grain) |

The Phase 1 record of the same unpruned full-table query was 3,363.6 ms (section 5.2); this run measured 2,793 ms on
the raw table, the difference being machine load and cache state. **The full-range rollup query is dramatically
cheaper than the raw baseline: 0.8 - 8.9 ms from the channel rollup, 76 - 148 ms from the campaign rollup, against
1.9 - 2.8 s raw, and (the point of the layer) it no longer grows with the number of facts.**

Honest reading, including what was *not* dramatic:

- The **campaign-grain** rollup only compresses the facts **11.0 : 1** here (273,600 rows, 57.6 MB with indexes, for
  3,000,000 fact rows). The synthetic data is worst case for it (every campaign appears on every channel every day; a
  real account has far fewer active campaigns per day), but the honest statement is that a full-range query on the
  campaign grain is 13 - 37x cheaper, not 1,000x. That is why the **channel-grain table exists**: 2,928 rows, 0.71 MB,
  answers the channel report in single-digit milliseconds. A page should read the campaign table only when it needs a
  campaign.
- The narrow date-range queries were already fast on the raw table thanks to partition pruning (249 ms); the rollup
  still wins by 54x - 1,250x but the absolute saving is smaller. The big win is the unbounded and long-range queries.
- `top 10 campaigns` on a 30-day range gains only 7x (220 ms to 31 ms) for the same reason as the first bullet.

**Building and refreshing**

| Step | Result |
|---|---|
| Full rebuild (`--full --yes`), 3,000,000 facts -> 273,600 + 2,928 rows | **18.2 s** (28 - 41 s in earlier runs of the same script on a busier machine; about 70 - 165 thousand facts/s, 12 monthly transactions). Dominated by writing the rollup rows and their two indexes |
| Incremental `--days 3` (nothing else changed) | **0.29 s** total: 320 rollup rows (+16 channel rows) replaced by 320, changed-day detection 0.03 s. The refresh's own fact `SELECT` read 4 of 16 partitions (the current month plus the three empty look-ahead months, because the window is open ended) and 66,672 fact rows, 59 - 280 ms |
| Proof that only those days were touched | rows rewritten **older than the window: 0**; the newest `refreshed_at` among older days is still the full rebuild's; 320 rows rewritten inside the window |
| A restated old day (`clicks + 5` on 2026-06-17, `loaded_at = now()` as the pipeline's `ON CONFLICT DO UPDATE` does) | 84 rows differed before; the next plain run reported **1 older day** picked up (`2026-06-17..2026-06-17` plus the window), 360 rows replaced in 0.31 s, 0 rows differ afterwards |
| Empty fact table | window and `--full` are clean no-ops (0 rows, exit 0), existing rollup rows untouched by `--full` |

**A finding, stated plainly: changed-day detection is only cheap once the BRIN is summarised.** In the first run the
whole `--days 3` refresh took 3.8 - 4.0 s, of which 3.1 s was the detection query: right after a bulk load the BRIN
index on `loaded_at` has *unsummarised* ranges, and a BRIN treats an unsummarised range as "might match", so it read
about 1.4 GB (182,782 buffers) to return 0 rows. After `VACUUM (ANALYZE)` (70 s on this table; what autovacuum does on
a real, loaded table) the same query took **11.7 ms** (3,299 ms before, 11.7 ms after, in the same run), and the
whole incremental refresh 0.29 s. So: on a freshly bulk-loaded table run a `VACUUM` once, or use `--window-only`
until autovacuum has been through it; on a normally loaded table this is not a concern.

**Correctness and idempotency** (all against the 3M rows)

- Rollup versus a direct `GROUP BY` of the facts (same 17 columns, `EXCEPT` in both directions), campaign table and
  channel table, all days: **0 differing rows**; the grand totals of impressions, clicks, conversions, spend and revenue
  are equal between the fact, the campaign rollup and the channel rollup.
- Refresh twice in a row (window, window), then a `--full`: the rollup fingerprint (row count + md5 of every row
  without `refreshed_at`) is **identical** each time: 273,600 + 2,928 rows, md5 `dd455179` / `1663751f`.
- The real CLI in a subprocess: `--days 3` exit 0 and logged; `--full` without `--yes` exit 2 with the reason; a
  schema without the tables exit 1 with the install command; the retention report at 6 months listed 6 candidate
  partitions with their sizes and commands, **and the partition count before and after was 16 and 16** (nothing removed).

### 8.6 How to schedule it (the owner's decision, not done)

Nothing runs this today. To keep the rollup current, register `run_marketing_rollup.bat` in Windows Task Scheduler
**after the night's loads**, once a day is enough for a daily rollup (a run costs about a third of a second plus the
changed days). Because the batch file passes its arguments through, a task can also say `run_marketing_rollup.bat --days 7`
for a wider safety window. Recommended one-time sequence:

```powershell
psql -U erp_app -h 127.0.0.1 -p 5432 -d erp_support -f db\sql\09_marketing_rollup.sql   # once
venv\Scripts\python.exe -m erp.marketing.rollup --full --yes                            # first build (or a plain run)
schtasks /Create /TN "ERP marketing rollup" /TR "C:\Users\AKIBA\Desktop\python\run_marketing_rollup.bat" /SC DAILY /ST 02:30
```

The `schtasks` line is an example only. Once a task whose command contains `run_marketing_rollup.bat` exists, the Data
Flow node picks it up by itself and stops saying "recommended, not set up".

### 8.7 What Phase 3 (the report page) can now rely on

- Read `interaction_daily_channel_rollup` for every channel-level number (tiles, trend, comparison) and
  `interaction_daily_rollup` only for a campaign drill-down; never `interaction_fact` for an aggregate.
- Both tables are keyed by `(event_date, channel_id[, campaign_id])`, indexed for a channel + date range, and hold
  additive columns only: re-aggregate freely (week, month, several channels) and compute every ratio in the query with
  `nullif` on the denominator.
- Freshness: the newest complete day is yesterday; today's numbers are as fresh as the last refresh. A page can show
  `max(refreshed_at)` or the latest `marketing_rollup_run` row (`status = 'ok'`, `finished_at`) as "data as of".
- A missing day means no facts that day (the refresh writes no zero rows); fill gaps in the query or the chart.
- Money is in micros (divide by 1,000,000), like the fact, and **only ever added within one currency** (the rollup key carries `currency`; section 9.1).
- If a report ever needs a slice the rollup does not carry (source system, event-type-specific measure, an `attrs`
  key), add that column or a third table to `09_marketing_rollup.sql` and to `MEASURE_COLUMNS`/`refresh_range` together
  (a scenario checks that the DDL and the job agree), rather than querying the fact.

### 8.8 Limits, stated

- Not installed in `public` and not scheduled: both need the owner.
- The synthetic data is uniform and dense; real data will have a different compression ratio and skew. The
  correctness and idempotency results do not depend on the shape; the speed ratios do.
- The changed-day detection does not see deleted fact rows (8.3) and needs a summarised BRIN to be cheap (8.5).
- The campaign-grain table is 11 : 1 on this data; the channel-grain table is the one that makes long-range reports
  cheap. `source_id` and `creative_id` are not in either grain.
- The look-ahead only creates partitions. It does not detach, drop, archive or compress anything.

## 9. Currency, duplicate keys, grain and the two source-specific connectors (built, verified on the owner's real files)

The owner supplied two real exports (kept in the git-ignored `data_inbox/`, never committed; only their SHAPE is described
here). Making the pipeline handle them correctly forced four decisions, all approved by the owner and all built:

| File | Shape | Grain | Currency |
|---|---|---|---|
| `data1.csv` | 118 rows of paid-social ad performance: `Start End Month Period Campaign Adset "Ad name" Device Impression Reach Freq Spent "Link click" "LP view" Eng Lead Purchase Revenue/pur Revenue` | **weekly** (`Start`..`End` = 7 days, three weeks) | **VND**, written `475,401 ₫` |
| `data2.csv` | 128 rows of e-mail campaign metrics: `send_date brand country campaign_id segment delivered unique_opens unique_clicks orders revenue_eur unsubscribes spam_complaints` | **daily** (ISO date), `campaign_id` unique per row | **EUR** (`revenue_eur`) |

### 9.1 Currency: money keeps its currency and is never converted

1,000,000 VND is about 35 EUR, so a sum over the two files means nothing, and `interaction_fact` had no currency column.

- **`db/sql/10_marketing_currency.sql`** (additive, the owner runs it; nothing in this project runs it on the real database) adds
  `currency CHAR(3) NOT NULL DEFAULT 'USD'` with a `CHECK (currency ~ '^[A-Z]{3}$')` to the fact. `ADD COLUMN IF NOT EXISTS` makes it
  idempotent; the default is a constant, so PostgreSQL stores it in the catalog and rewrites nothing. On the owner's database the
  table has 0 rows and no partitions, so it is instant. The default `USD` is the assumption the Channels page always made before the
  column existed; it is documented in the file. A connector that knows better writes its own code (`ad_performance` reads it from the
  cell's symbol, `email_campaign` from the `revenue_eur` header, the generic reader from a `currency` column or `--currency`).
- **Order:** 07 (installed) -> **10** -> 09. `require_currency_column()` (pipeline and rollup job) refuses to run without the column
  and prints the exact `psql` command.
- **Native currency, no conversion, no rates anywhere.** The currency is deliberately *not* part of the dedupe identity: a
  restatement that changes a row's currency updates that row.
- **The rollup tables carry `currency` (and `grain`, below) in their key**, and the refresh groups by them: a day x channel with VND
  rows and EUR rows is two rollup rows. Counts (clicks, sessions ...) have no currency and add freely; money adds only inside one
  currency. 09 is not installed anywhere, so it was edited directly.
- **The Channels page never adds across currencies** (`money_cell()` in `desktop/channels_data.py` is the one place that decides):
  one currency in view -> the amount with its code; several -> one line per currency and no total (tiles, table cells, the total row,
  cost per conversion - computed per currency from that currency's own conversions - and the CSV, which writes `MULTIPLE` and leaves
  the amounts blank); none -> "no spend field". A money sort orders *within* each currency. A `currency` filter (whitelisted against
  the data, a 400 for an unknown code or a typo of the parameter name) narrows the whole view to one currency, counts included. A
  currency that carries no money (a source with no spend field) does not make a view mixed. The chart draws counts only, so it has no
  currency to mix. Revenue is a new tile and column; the rollup also keeps `revenue_derived_micros` so derived revenue is labelled
  **derived** (9.5).
- The generic flat-file reader still defaults to `USD` (`--currency VND` overrides it). Its `to_date` still tries day-first before
  month-first (a pre-existing behaviour, untouched: the strict connectors below exist because of exactly that ambiguity).
- `MAX_MONEY_MICROS` was raised from 1e15 to 1e16 micros: the old bound meant "1 billion currency units", which is a USD-scale
  assumption (1 billion VND is about 40,000 USD). Still 900x below the BIGINT ceiling.

### 9.2 Duplicate natural keys: keep both rows, and say so

The sample has **6 pairs** of rows that share (week start, campaign, adset, ad name, device) but carry different metrics (for
example 768 vs 341 impressions). The generic dedupe (`event_date, source, channel, campaign, creative, event type`) would treat the
second row as a restatement and **overwrite the first: silent data loss** (it does not even look at adset or device).

- The `ad_performance` connector numbers the rows within an identical natural key in file order (0, 1, ...) and builds an exact
  external id `<channel>|<start>|<campaign>|<adset>|<ad name>|<device>#<occurrence>` (case and spacing insensitive, like the
  dimensions; a digest replaces it if it would exceed the 200-character identity column). Both rows are kept.
  `email_campaign` does the same with (channel, send date, campaign_id, brand, country, segment).
- **Idempotent:** the same file again finds the same numbers, so the second import updates the same rows and adds none (proved on the
  real files, 9.8).
- **The trade-off, stated plainly:** the number is a position. It is stable only while the export's row order is stable. The same
  rows in another order keep the same identities and every total, but two rows under one key can swap their metrics with each
  other. Nothing is lost; the pairing is not guaranteed. (A scenario in `tests/marketing_scenarios.py` demonstrates it.)
- **The same trade-off for removal, addition and partial overlap (the run summary does NOT warn about these):**
  - *A row removed from a duplicate group* (an export that used to have 3 rows under a key now has 2): the survivors are numbered
    0 and 1 again, so if the removed row was #0 (rows A, B, C -> B, C), B overwrites A's stored row, C overwrites B's, and C's old
    row (#2) stays in the database as a **stale row** nothing updates.
  - *A row added to a group*: it takes the next free number when appended (correct), but inserted before its twins (X, A, B, C)
    every row takes its predecessor's number: X overwrites A's stored row, A overwrites B's, B overwrites C's, and C becomes a new row.
  - *Two files that partly overlap* (the same weeks exported twice with different cut-offs): rows are paired by position within a key,
    not by content, so a row can overwrite a different former twin and a stale row can remain.
  - The only signal is the count `restated changed` (below); it says that stored numbers moved, not which rows or why.
- **Restated rows, split honestly:** the summary prints `written to fact N (a new, b restated)` and, on the next line,
  `restated changed c` = how many of the restated rows hold numbers (impressions, clicks, reactions, sessions, conversions, spend,
  revenue, currency) that DIFFER from what the database had; `b - c` were identical re-imports. It costs no extra pass: the bulk
  existence lookup that already ran before the upsert now also returns the stored numbers, and they are compared in Python.
  A dry run does not touch the database, so it cannot print this line.
- **Reporting:** the run summary prints `key collisions  N kept as separate rows ..., M replaced` with the first samples (record
  number, file line, the natural key). "Kept" = a connector gave the row its own identity; "replaced" = two rows of one run had the
  *same* identity, the later one won (any generic file; also across chunks of one run, tracked up to `MAX_TRACKED_KEYS` = 2,000,000
  identities, and said when it stops). Nothing is overwritten silently any more.

### 9.3 Grain: a week is not a day

`data1` is weekly. Each row sits on its **start date** and carries `attrs.grain = 'week'`, `attrs.period_end` and `attrs.period_days = 7`
(a row whose End is not Start + 6 days is rejected as "not a weekly row"). The rollup keeps `grain` in its key (`week` when
`attrs->>'grain' = 'week'`, otherwise `day` - a two-value CASE with a CHECK, so a stray value cannot invent a third). The Channels page:

- the per-day chart **never draws a weekly row** (that would be a false one-day spike); weekly rows go to a separate **per-week
  chart**, one bar per week centred on the middle of its 7 days and labelled "week of ...". A range too long for the per-day chart
  (180 days) still shows its weekly bars (limited by count, `CHART_MAX_WEEKS` = 104, not by the day window);
- coverage counts weeks ("3 weeks of weekly data, each counted on its start day"), not "3 of 30 days", which would read as an outage;
- tile sparklines use the one grain the view has (labelled), and are left out when daily and weekly rows are mixed;
- a range that starts or ends inside a week includes or excludes the whole week by its start day: the source gave no daily split.
- the CSV daily file is one row per day x channel x grain x currency and says `grain` and `period_days`.

### 9.4 The two connectors (`erp/marketing/connectors/ad_performance.py`, `email_campaign.py`)

Both build on `connectors/strict_csv.py` and `parse.py`, and are run with
`python -m erp.marketing.ingest --connector <name> --csv <file> [--channel X] [--schema S] [--dry-run] [--date-format mdy|dmy]`.
The generic `flat_file` connector stays the default, with one behaviour change to know: it now **consumes** a `currency` /
`currency_code` / `iso_currency` column as the row's currency (it no longer lands in `attrs`) and **rejects** a row whose value is not
a three-letter code; before, that column was just another `attrs` entry and never rejected anything.

**Money cells never depend on a locale.** `parse.parse_money` refuses what could be read two ways. Zero-decimal currencies (VND,
JPY, KRW: `parse.ZERO_DECIMAL_CURRENCIES`) refuse any decimal part; `475,401`, `475.401` and `1,234,567` / `1.234.567` are thousands
groups. Any other currency (or none named) accepts plain digits with an optional `.dd` (`1249.13`) or comma thousands with a `.dd`
(`1,249.13`, `1,234,567`), and **refuses** `1.249` and `1,249` (1.249 or 1249?), `249,130`, and the European `1.249,13`, with a
message that lists the accepted forms. Digits are ASCII `[0-9]` only. A cell without a symbol is parsed under its row's currency
(the `Spent` currency for `Revenue`, EUR for `revenue_eur`). A header with padded names (`Revenue `) is refused, not stripped.
A row with neither `Revenue` nor `Revenue/pur` is stored with revenue 0 and basis `none`; the run summary says revenue is UNKNOWN
for those rows (a count) instead of printing 0.00 as if it had been reported.

`ad_performance` (channel `paid_social` by default, `--channel` overrides): Start -> event date; Impression -> impressions; Link click
-> clicks; Eng -> reactions; LP view -> sessions; **Purchase -> conversions**; **Lead -> `attrs.leads`, NOT added to conversions**;
Spent -> spend, currency from the symbol; Campaign / Ad name -> campaign / creative dimensions; Adset, Device, Period, Month, End ->
attrs; **Reach and Freq -> attrs only** (not additive: a person reached in two weeks is one person - nothing sums them).

`email_campaign` (channel `email`): send_date -> event date; **delivered -> impressions** (an e-mail that reached an inbox, *not* an
ad impression: `attrs.impressions_are = 'delivered'`, exact number in `attrs.delivered`); **unique_opens -> reactions** (not a social
reaction: `attrs.reactions_are = 'unique_opens'`, exact number in `attrs.unique_opens`); unique_clicks -> clicks; orders ->
conversions; revenue_eur -> revenue in EUR; campaign_id -> campaign; brand, country, segment, unsubscribes, spam_complaints -> attrs.
**Sanity rules, checked per row:** `unique_opens <= delivered` and `unique_clicks <= unique_opens`; a violating row is **rejected**
and counted with its reason in the run summary, never loaded quietly (both hold for every row of the sample).

**Strict, on purpose.** A source-specific connector has no excuse to guess.

- The header must be exactly the declared columns (any order): a missing, extra, repeated, renamed or differently-cased column is
  refused before a row is read, naming the difference. No fuzzy matching, so a column can never be mis-mapped.
- **Dates: ISO always; slash dates in ONE order for the whole file, decided by evidence and never guessed.** `6/16/2024` proves
  month-first (16 cannot be a month); a first part above 12 proves day-first; both kinds of evidence in one file, or none (every
  date has both parts 12 or below, e.g. `3/4/2024`), is **refused** with the flag that settles it (`--date-format mdy|dmy`). A
  forced order that the dates contradict is refused too. The sample has `6/16/2024`, so it is month-first.
- **Money:** thousands as commas in groups of three, decimals with a dot, a currency symbol (`₫ € £`) or ISO code in front or behind;
  `$` and `¥` are refused as ambiguous, `1.234,56`, `1,5`, signs and text are refused (the row is rejected, not guessed). Money is
  converted to integer micros with `Decimal`, no float on the way (`spend_micros` / `revenue_micros` reach the model exactly).
- UTF-8 with or without a byte-order mark, CRLF, a blank line, a row with too many or too few cells (rejected by name, never padded).
- Every rejected row is counted and skipped and the batch carries on (L-044); each record knows its file line.

### 9.5 Revenue in `data1` is DERIVED, and is marked so

The export's own `Revenue` column is empty in the sample, so revenue = `Purchase` x `Revenue/pur` (a fixed 1,000,000 or 1,500,000
VND per purchase). Every such row carries `attrs.revenue_derived = true` and `attrs.revenue_source = 'derived: Purchase x
Revenue/pur'`. If a later export fills `Revenue`, that reported number is used and the row says `revenue_derived = false`; with
neither, revenue is 0 and not marked derived. The rollup keeps the derived part of the sum in `revenue_derived_micros`, so the
Channels page labels it **derived** (tile, table cell, CSV column `revenue_derived`) and never presents it as reported. The
Channels page cannot say *which rows* inside a channel are derived, only how much of its revenue is.

### 9.6 `attrs_idx`: brand and segment were added

`brand` and `segment` joined `INDEXED_ATTR_KEYS` (`device` and `country` were already there). The scoped-GIN design allows it
cleanly: the index covers the whole `attrs_idx` column, so adding keys changes no DDL, only what the pipeline writes; the fact held
no rows, so there is nothing to backfill; rows without the keys cost nothing. The allowlist stays short (8 keys). The 07 file's
comment was updated to name them (the installed index itself is unchanged). Proved: `attrs_idx @> '{"brand": ...}'` counts every
brand, country, segment and device value of the sample exactly.

### 9.7 What is where

`erp/marketing/parse.py` (strict parsers), `connectors/strict_csv.py` (shared machinery), `connectors/ad_performance.py`,
`connectors/email_campaign.py`, `ingest.py` (`--connector`, `--currency`, `--date-format`), `pipeline.py` (currency column, collision
reporting, spend per currency in the summary), `schema.py` (`require_currency_column`), `rollup.py` (currency + grain + derived
revenue), `sample_check.py` (the verification below), `db/sql/10_marketing_currency.sql`, `09_marketing_rollup.sql`.

### 9.8 What was verified on the real files (throwaway schema `perf_samples`, dropped afterwards)

`python -u -m erp.marketing.sample_check --yes --data1 data_inbox\data1.csv --data2 data_inbox\data2.csv` installs the real DDL 07 +
10 + 09 into a `perf_` schema (search_path is that schema alone, never `public`), runs the real command line, compares direct SQL with
the source files parsed independently (csv + regex, not the connectors' own parsers), and drops the schema (65 checks, all passed on
2026-09-25). It records the schemas and the `public` table list before and after: identical.

| Check | Result |
|---|---|
| Migration 10 on an installed, **populated**, partitioned table (25 rows, one partition) | column added to parent and existing partition, **same `relfilenode` (no rewrite)**, old rows read `USD`, second run a no-op, a partition created afterwards has it, the CHECK refuses `vnd`, `VN`, `VNDD`, `V1D` and `''` |
| Dry run, then real load of `data1` | 118 read, 118 landed, **0 rejected**, 6 key collisions kept, 118 new, spend **VND 4,440,453**, revenue VND 277,000,000 (derived) |
| Dry run, then real load of `data2` | 128 read, 128 landed, **0 rejected**, 128 new, revenue **EUR 422,747.06**, no spend |
| Sums vs the source, every measure | data1: impressions 103,728, clicks 13,880, reactions 5,722, sessions 8,382, conversions 222, leads 345 (attrs), spend 4,440,453 VND, derived revenue 277,000,000 VND; data2: impressions (delivered) 6,723,403, reactions (opens) 1,218,931, clicks 152,848, conversions 7,425, revenue 422,747.06 EUR - **all equal** |
| Cell for cell | every fact row equals its source row (multiset of 118 and of 128 rows) |
| The 6 duplicate pairs | **both rows of every pair are in the fact** with their own metrics; no dedupe key repeats |
| Grain / markings | 118 rows `grain=week` with `period_end = start + 6`; 118 `revenue_derived`; reach/frequency in attrs and in no rollup column; data2 rows document delivered / unique_opens |
| Scoped GIN | `attrs_idx @> {brand / country / segment / device}` counts every value right (4 + 3 + 8 + 6 values) |
| Rollup (real job code) | equals a direct `GROUP BY` of the fact in both directions (0 differing rows), per (currency, grain): `VND/week` and `EUR/day` are separate; VND and EUR totals equal the source; weekly rows sit on their 3 start days; derived revenue = all of VND's |
| **Second identical import** | **0 new rows** (118 and 128 restated); fact row count + md5 of every row and the rollup fingerprint **unchanged** |

The Channels page was then pointed at that schema (a scratch ERP Desk on a spare port, `MARKETING_SCHEMA`) and checked in headless
Edge at 1920 and 390 px: with both files in range the money is shown per currency with no total, the weekly data has its own
per-week chart and is counted in weeks, `currency=EUR` / `VND` narrow everything, and there was no horizontal overflow and no
console error. The scenario tables (`tests/marketing_scenarios.py`, `tests/channels_scenarios.py`) hold the same rules without a
database, plus SQL scenarios in the session's temporary schema with two currencies and a weekly source.

### 9.9 Limits, stated

- Not installed in the real database: 10 and 09 are the owner's to run (07 is installed; `interaction_fact` has 0 rows). The Channels
  page there still says "not installed" and now lists 10 before 09.
- The occurrence index is a file position (9.2). A source that gives a real row id should use it as `external_id` instead.
- Derived revenue is a stated derivation (purchases x value per purchase), not an observation; the page labels it but cannot mark
  individual rows inside a channel.
- The rollup key is wider (currency, grain), so it has more rows than before wherever a day has several currencies or grains; on the
  sample that is 136 campaign-level and 104 channel-level rows for 246 facts.
- The chart shows counts only: spend and revenue are not drawn as a per-day series (a per-currency series is the natural next step).
- A currency-less `USD` default can still be wrong for a file that names no currency; pass `--currency`.
- "Low volume" now needs fewer than `LOW_N` sessions **and** fewer than `LOW_N` clicks (an e-mail source has no sessions but
  thousands of clicks and was wrongly flagged).

## 10. The inbox processor (`python -m erp.marketing.autorun`) — built

**What it is.** The owner's use case is an analyst at a large company who receives marketing exports (CSV now, database pulls later) and
wants reports without hand-running four commands. The company has its own systems, so this is a personal automation toolkit, not a new
system: one command processes a folder. It adds no table, no migration and no parsing — it is a thin orchestrator over what already
exists (`ingest.build_connector`, `Pipeline.run`, `rollup.refresh`).

**Flow.** For each `*.csv` in `data_inbox/incoming` (`--inbox`, `MARKETING_INBOX`): settle check (modified in the last 5 s, or size/mtime
changed while it was hashed -> skipped, left alone) -> SHA-256 -> already loaded? -> detect -> load -> move. After the files, one
incremental rollup refresh if any file loaded rows. `processed/` and `failed/` are siblings of the inbox folder.

**Detection never guesses.** The two strict connectors' `check_header` decide `ad_performance` and `email_campaign` (exact header, any
order). `flat_file` needs a channel hint: `<channel>__<currency>__whatever.csv` (channel slugged, currency a 3-letter code) or a header
column that slugs to `channel`; the filename channel/currency are passed to the same `--channel` / `--currency` flags of the ingest
command line. A strict header always wins over a filename hint. No hint -> `failed/` + `<moved name>.reason.txt` with the detected
header, each strict connector's own refusal, and how to fix it. Empty, binary (NUL bytes) and non-UTF-8-header files get their own reason.

**Outcome rules** (the pipeline's status is honoured, and three cases the pipeline reports as `ok` are made explicit):

| Pipeline says | Autorun does |
| --- | --- |
| `ok`, rows loaded | `processed/`; rejected rows are counted and named in the summary |
| `partial`, rows loaded | `processed/`, status `partial` |
| `failed` (systematic SQL failure / failure budget) | `failed/` + reason (the pipeline's own error); rollup still runs if some chunks had committed |
| `ok` but a header-only file, or every row rejected | `failed/` + reason (nothing loaded is not a success) |
| database unreachable / tables missing | the run **stops**, every remaining file stays in the inbox (`deferred`), exit 1, no rollup |

**Idempotence.** `marketing_ingest_run` has no hash column (`origin` is a free-text file name), so the content hash lives in
`<inbox>/.autorun_index.json`, keyed per schema, written atomically after each successful file and before the move (a crash in between
just makes the next run see a duplicate and move it). Only files that loaded are remembered, so a failed file dropped again is retried.
The same file holds the record of the last real run (time, counts, rollup result, exit code), which the Data Flow node shows. Deleting
the index is safe: the load itself is an idempotent upsert on the natural key (section 9.2), so at worst a file is read again.

**Safety properties.** Originals are only renamed (never rewritten, never deleted); a move failure is reported and leaves the file where
it was; `--dry-run` uses `Pipeline.run(dry_run=True)`, which opens no database connection, and writes no index, lock, folder or sidecar;
one run at a time per inbox (`.autorun.lock`, a lock older than 2 hours is treated as a dead run's); the connector's lazy file handles are
closed before the move (Windows will not rename an open file).

**Not scheduled.** Nothing runs it. The Data Flow map lists it with a recommended, not-set-up Task Scheduler trigger, exactly like the
rollup job; `run_marketing_autorun.bat` has no `pause` so it is safe under Task Scheduler if the owner chooses to register it.

**Verification** (`tests/marketing_autorun_scenarios.py`, 139 scenarios, run by hand, not part of the smoke gate): detection of both real
header shapes and unknown / empty / binary / padded / extra-column files; the partial-file boundary (4.9 s vs 5.1 s), a file that grows
while being read; duplicates by hash within a run, across runs, per schema; routing, timestamp prefixes, same-name and same-second
collisions, reason sidecars; every pipeline status; a dead database; the rollup called exactly once (never on failure-only or duplicate-only
runs); dry run changes nothing; lock; run record; and an end-to-end run in a throwaway `perf_autorun_*` schema with copies of `data1.csv`
(118 rows) and `data2.csv` (128 rows) plus a filename-hint file, a bad-row file and an unknown file: fact rows equal the reported loads,
VND spend equals an independent sum of the source, a second drop of the same exports loads nothing and records no new ingest or rollup run,
and `public` is byte-for-byte the same (table list and `interaction_fact` count) afterwards.

**Limits, stated.** CSV only (database pulls are a later slice); one delimiter/encoding story per connector (strict: comma + UTF-8; a bad
byte in the header block is refused with its position, `flat_file` decodes with replacement); the strict connectors take one global
`--date-format` per run; a file without a channel hint is never loaded (by design); the hash index is per inbox folder, so moving to
another inbox forgets what was loaded (harmless, the load is idempotent); processed/ and failed/ grow until the owner cleans them.

## 11. Placements: where the display ads ran (built)

**What it is.** A display-network report has one row per day x campaign x ad group x placement (a site, a marketplace app or a banner slot) x
ad size x position x device. This slice loads that report, rolls it up, shows it on the Channels page and lets the Insights rules point at
placements worth excluding or scaling. It changes no existing table, view or output.

**Connector `placement_performance`** (`erp/marketing/connectors/placement_performance.py`): exact 14-column header (any order, no others),
placement text sanitised (whitespace collapsed, cut at 200 characters, a control or invisible character refuses the row), duplicate natural keys
kept and occurrence-numbered like `ad_performance`, money and currency like `ad_performance` (currency on the cell, or `--currency`; a bare `$`
is ambiguous and refused). A blank ad size, position, viewability or conversion value is stored as unknown, never 0. The placement becomes a
`marketing_creative` row (`slug + 8 hex digits of a hash`), so there is no new dimension table. Detection in the inbox processor runs
`ad_performance`, `email_campaign`, then `placement_performance`; the three headers are different exact sets, so none can take another's file.
The currency is not part of a row's identity: the same key re-sent in another currency is a restatement, not a second row.

**Rollup** (`db/sql/11_marketing_placements.sql`, `erp/marketing/rollup_placements.py`): one additive table
`interaction_placement_rollup` (day x channel x campaign x placement x type x size x position x currency), refreshed by the SAME rollup job over
the same ranges and the same `loaded_at` watermark. If the table is empty and placement facts exist (migration installed late) the next run builds it
from scratch. Cardinality guard: each campaign keeps its first 500 placements (by creative id, which does not move when a later day is loaded) and
folds the rest into `placement_id = 0` with the sums intact and a count of folded placements. Without migration 11 the step logs one line and is skipped.

**Measured** (throwaway schema, 600,000 synthetic placement fact rows, 6,000 placements, 60 days; dropped afterwards): the fact partitions
took 227 MB, the placement rollup 448 kB (1,560 rows, 4,500 placements folded). Full rebuild 12.1 s. The Placements page for 60 days took about
100 ms against the rollup; the same GROUP BY on the fact took 2.4 s (rollup 87 ms). A one-day incremental refresh recomputed only the changed day
and the window. The page never reads `interaction_fact` or the landing table (the scenario file captures every statement it sends).

**Page and exports.** `GET /api/channels/placements` (+ `.csv`), same filters as the Channels page plus `psort` / `pdir`; unknown parameters are a
400 before the cache. Money per currency, never added; rates need `LOW_N` in the denominator; blank means unknown; viewability is viewable /
MEASURED impressions with the measured share shown. The Excel report gains a Placements sheet when the table is installed (bounded before it is built).

**Rules** (`desktop/insights_rules.py`, named `PLC_*` thresholds, judged per campaign and currency, thin data is info only, no automatic change):
exclude candidates (no conversions with a luck test, or a very low CTR), scale candidates, low viewability, spend concentration, and a
reallocation ESTIMATE shown only when both groups have enough data, flagged, with its assumption (constant cost per conversion, no saturation).

**Verification.** `venv\Scripts\python.exe -B -m tests.placement_scenarios` (synthetic data only, throwaway `perf_plc_*` schemas, an end-to-end
autorun of synthetic CSVs from a temp inbox; `public` is compared before and after).
