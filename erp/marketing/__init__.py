"""Marketing / channel interaction data: the scale-ready star schema, the ingestion pipeline (Phase 1) and the rollup layer (Phase 4).

  model.py                the common intermediate shape every connector maps to, plus the typing / validation rules
  schema.py               where the tables live, whether they are installed, and the monthly partitions
  pipeline.py             land -> validate/type -> dedupe -> load dimensions + fact (bulk, never row by row)
  connectors/base.py      what a connector has to provide
  connectors/flat_file.py the generic connector: any CSV / list-of-dicts source, columns recognised by name
  connectors/strict_csv.py, ad_performance.py, email_campaign.py
                          the two STRICT connectors for the owner's real exports (exact header, no guessed dates or columns,
                          duplicate natural keys kept and reported, money in its own currency); parse.py holds their parsers
  sample_check.py         verifies both connectors on the real files in a throwaway schema (never the real tables)
  ingest.py               the command line entry point (python -m erp.marketing.ingest)
  perf_check.py           the one-off scale verification in a throwaway schema (never the real tables)
  rollup.py               refreshes the daily channel/campaign rollup of the fact (python -m erp.marketing.rollup)
  partitions.py           pre-creates the next monthly fact partitions and REPORTS (never drops) old ones
  rollup_check.py         the one-off scale verification of the rollup layer, in a throwaway schema
  autorun.py              the inbox processor: every CSV in a folder -> detect connector, load, move, rollup once (python -m erp.marketing.autorun)

The DDL is db/sql/07_marketing_schema.sql and 09_marketing_rollup.sql; the reasoning is docs/marketing-data-architecture.md.
"""
