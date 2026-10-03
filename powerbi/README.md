# ERP Desk in Power BI

Three report templates over one shared DirectQuery model, so every number is read live from PostgreSQL.

| File | Report | Pages |
|---|---|---|
| `Social.pbip` | Social listening | Overview, Detail |
| `Leads.pbip` | Leads and SLA | Overview, Waiting and speed |
| `Channels.pbip` | Channels (marketing) | Overview, Trend |

`ERPDesk.SemanticModel/` is the model all three use (7 tables, 42 DAX measures). `ERPDesk.theme.json` is the ERP Desk look
(palette, Montserrat, rounded white panels on a cream page); it is already inside each report, so you only need it to restyle other reports.

## Before you open anything

1. **Create the views** (once, as the database owner):
   `psql -U erp_app -h 127.0.0.1 -d erp_support -f db/sql/14_powerbi_views.sql`
   They need the leads (03, 05), marketing (07, 09, 11) and social listening (12) scripts. The views carry no e-mail, phone or person name.
2. **Create the read-only login** if it does not exist: `db/sql/06_powerbi_readonly.sql`, then run `14_powerbi_views.sql` again so it can grant `powerbi_reader` the new views.
3. **Power BI Desktop**: install the Npgsql driver when it asks, and turn on *File > Options > Preview features > Power BI Project (.pbip) save option*
   and *Store reports using enhanced metadata format (PBIR)* (newer versions have both on by default).

## Open a report

Open `Social.pbip` (or `Leads.pbip`, `Channels.pbip`) in Power BI Desktop. If PostgreSQL is not on this machine, change the two
parameters first (*Transform data > Edit parameters*): `PgServer` (default `127.0.0.1`) and `PgDatabase` (default `erp_support`).
Sign in with *Database* credentials: user `powerbi_reader`, the password from `.env` (`POWERBI_DB_PASSWORD`).

Because the model is DirectQuery, nothing is imported; pages query PostgreSQL as you filter. *Home > Refresh* re-reads.

## What is where

- **Social**: slicers (date range, brand, channel), five tiles, mentions per day, sentiment mix, topics, channels; detail page with negative share by topic and brand, posts by hour, the posts with most engagement, the events marked on the charts and the cleaning funnel.
- **Leads**: tiles (leads, answered within SLA, waiting past SLA, median reply, potential score), leads per week, SLA outcome, rep and source breakdowns; detail page lists exactly the leads still waiting past their SLA (a row appears only while *Waiting Past SLA* is not blank), time to first reply and a rep by outcome matrix.
- **Channels**: sessions, clicks, conversions, conversion rate and spend per channel, with a per-channel table; detail page with conversions by channel and month and paid against organic.

## Rules the model keeps (same as the ERP Desk pages)

- **Money is never added across currencies.** `Spend (one currency)`, `Revenue (one currency)` and `Cost per Conversion` return a value only while one currency is in the filter; pick a currency in the slicer to see them.
- A rate with no denominator is blank, not 0 (`DIVIDE` without a third argument).
- Spam posts and duplicates are already removed by `sl.run_cleaning`; the views read only `sl.v_mention`.
- Leads' "within SLA" compares the first synced comment with `leads.sla_due_at`, which holds the 5-business-hour deadline calculated by `erp/business_hours.py`.

## Not the same as ERP Desk

No animation, no live pulse, no side drawers (use *drill through* or a report page tooltip instead), no Data Flow map and no AI summary.
Montserrat is not embedded: install it on every machine that opens the report, or Power BI falls back to another font.

## Changing the templates

The `.pbip` files are generated. Edit `powerbi/build_pbip.py` (model, measures, pages, layout, theme) and run
`python powerbi/build_pbip.py --check`; the check fails when a visual uses a field the model does not have.
Edits made in Power BI Desktop are kept in the project folders, but a re-run of the script overwrites them: do one or the other.

## Honest status

These files were written and checked by script (every field a visual uses exists in the model, no overlapping visuals, valid JSON) but
**were opened once in Power BI Desktop (September 2026, 2.158)**, which found two problems (a wrong `$schema` in `definition.pbir`, and measures named like a column: Power BI compares names case-insensitively); both are fixed and the build check now guards them. Other problems may remain: send the exact error text. If Desktop reports a file it cannot read, open `ERPDesk.SemanticModel` alone first
(*File > Open > Power BI project*), then recreate the page layouts from the tables above; the model and the measures are the valuable part.
The Channels report is empty until marketing data is loaded into the rollup tables.
