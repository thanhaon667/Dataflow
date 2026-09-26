"""Load one marketing / channel export into the interaction star schema (land -> validate -> dedupe -> load).

This is the command-line entry point of the pipeline. Four connectors are wired in:

  flat_file         (default) any table-shaped export, columns recognised by name - the generic reader
  ad_performance    the owner's paid-social export: weekly rows, money written with a currency symbol, exact 19-column header
  email_campaign    the owner's e-mail export: daily rows, EUR, exact 12-column header
  placement_performance  a display-network PLACEMENT report (site / app / banner slot, size, position): daily rows, exact 14-column header

The three source-specific connectors are STRICT: an export whose header is not exactly theirs is refused (no fuzzy column
guessing), a slash-date order they cannot prove is refused (--date-format settles it), and a row that breaks their sanity
rules is rejected and counted, never loaded quietly. See erp/marketing/connectors/ad_performance.py, email_campaign.py and
placement_performance.py for the mapping of every column.

Run:
  venv\\Scripts\\python.exe -m erp.marketing.ingest --csv data\\channel_export.csv
  venv\\Scripts\\python.exe -m erp.marketing.ingest --connector ad_performance --csv data_inbox\\data1.csv --dry-run
  venv\\Scripts\\python.exe -m erp.marketing.ingest --connector ad_performance --csv data1.csv --channel facebook --schema perf_x
  venv\\Scripts\\python.exe -m erp.marketing.ingest --connector email_campaign --csv data_inbox\\data2.csv
  venv\\Scripts\\python.exe -m erp.marketing.ingest --connector placement_performance --csv data_inbox\\placements.csv --currency EUR --dry-run
  venv\\Scripts\\python.exe -m erp.marketing.ingest --csv export.csv --source facebook_export --currency VND --dry-run

--dry-run reads and validates the file and opens no database connection at all, so it is the safe way to
see how a new export maps (how many rows are usable, what would be rejected and why, spend per currency, and every
natural-key collision) before loading it.

Exit code: 0 when the run finished, 1 when it could not run or a chunk failed (see the ingest journal,
table marketing_ingest_run, and the logged reasons). A header or date-order refusal is exit 1 with the reason and touches nothing.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger("erp.marketing.ingest")

CONNECTORS = ("flat_file", "ad_performance", "email_campaign", "placement_performance")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m erp.marketing.ingest",
        description="Load a marketing / channel export (CSV) into the interaction star schema.",
    )
    p.add_argument("--csv", required=True, help="the export file to read")
    p.add_argument("--connector", choices=CONNECTORS, default="flat_file",
                   help="which reader maps the file: flat_file (generic, default), ad_performance (paid-social weekly export) or "
                        "email_campaign (e-mail daily export) or placement_performance (display-network placement report)")
    p.add_argument("--source", default=None,
                   help="key of the source system this file came from. Default: flat_file for the generic reader, the connector's "
                        "own name otherwise. It is part of every dedupe key, so keep it stable for the same source.")
    p.add_argument("--channel", default=None,
                   help="the marketing channel of a file that names none (ad_performance default: paid_social, email_campaign "
                        "default: email; flat_file: the file's own channel column)")
    p.add_argument("--currency", default=None,
                   help="ISO code of spend/revenue for rows that name none. flat_file: the default for rows with no currency column (USD). "
                        "placement_performance: the currency of a Cost cell written without one (a cell that names another currency is rejected). "
                        "ad_performance / email_campaign read the currency from the file and refuse this flag")
    p.add_argument("--date-format", choices=("auto", "mdy", "dmy"), default="auto",
                   help="slash-date order for ad_performance / email_campaign: auto detects it and REFUSES when the file is "
                        "ambiguous (every date has both parts 12 or below); mdy or dmy settles it")
    p.add_argument("--schema", default=None, help="database schema holding the marketing tables (default: MARKETING_SCHEMA in .env, else public)")
    p.add_argument("--delimiter", default=None, help="force a column delimiter instead of sniffing it (flat_file); a strict file is comma separated unless given")
    p.add_argument("--channel-default", default=None, help="flat_file: the same as --channel (kept for older commands)")
    p.add_argument("--limit", type=int, default=None, help="stop after this many records (a quick look at a big file)")
    p.add_argument("--chunk-size", type=int, default=None, help="records per transaction (default: MARKETING_BATCH_SIZE)")
    p.add_argument("--dry-run", action="store_true", help="validate only: no database connection, nothing written")
    p.add_argument("--no-resolve-leads", action="store_true", help="skip matching identities to existing leads")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    return p


def build_connector(args, path: Path):
    """The connector the arguments ask for. Raises ConnectorError (a refusal) or ValueError (a bad combination of flags)."""
    channel = args.channel or args.channel_default
    if args.connector == "flat_file":
        from erp.marketing.connectors.flat_file import FlatFileConnector
        if args.currency is not None:
            from erp.marketing.model import to_currency
            if to_currency(args.currency) is None:
                raise ValueError(f"--currency {args.currency!r} is not a three-letter ISO 4217 code")
        return FlatFileConnector.from_csv(path, source_key=args.source or "flat_file", delimiter=args.delimiter,
                                          channel_default=channel, currency_default=args.currency)
    if args.connector == "placement_performance":
        from erp.marketing.connectors.placement_performance import PlacementPerformanceConnector
        connector = PlacementPerformanceConnector.from_csv(path, channel=channel, date_format=args.date_format, delimiter=args.delimiter or ",",
                                                           currency=args.currency)
        if args.source:
            from erp.marketing.model import slug
            connector.source_key = slug(args.source, 60) or connector.source_key
        return connector
    if args.currency is not None:
        raise ValueError(f"--currency applies to the flat_file and placement_performance connectors only: {args.connector} reads the currency from the file itself")
    if args.connector == "ad_performance":
        from erp.marketing.connectors.ad_performance import AdPerformanceConnector as cls
    else:
        from erp.marketing.connectors.email_campaign import EmailCampaignConnector as cls
    connector = cls.from_csv(path, channel=channel, date_format=args.date_format, delimiter=args.delimiter or ",")
    if args.source:
        from erp.marketing.model import slug
        connector.source_key = slug(args.source, 60) or connector.source_key
    return connector


def money_lines(money: dict) -> list[str]:
    """One line per currency - never a sum across them."""
    out = []
    for cur in sorted(money):
        m = money[cur]
        unknown = m.get("revenue_unknown_rows", 0)
        if not unknown:
            revenue = f"revenue {m['revenue_micros'] / 1e6:,.2f}"
        elif not m["revenue_micros"]:
            revenue = f"revenue UNKNOWN ({unknown} rows have no revenue basis)"
        else:
            revenue = (f"revenue {m['revenue_micros'] / 1e6:,.2f} from the other rows "
                       f"(UNKNOWN for {unknown} rows with no revenue basis - stored as 0, not reported)")
        out.append(f"  money {cur}      spend {m['spend_micros'] / 1e6:,.2f}   {revenue}")
    return out


def main(argv: list[str] | None = None) -> int:
    # The run summary quotes raw file cells (reject/collision samples). A character outside the console
    # code page must never turn a finished load into a UnicodeEncodeError traceback and exit 1.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # imported here, not at module level: --help and a bad path must not need a database driver
    from erp.marketing.connectors.base import ConnectorError
    from erp.marketing.pipeline import Pipeline
    from erp.marketing.schema import SchemaError

    path = Path(args.csv)
    if not path.is_file():
        logger.error("no such file: %s", path)
        return 1

    try:
        connector = build_connector(args, path)
    except ConnectorError as exc:
        logger.error("refused - %s", exc)
        return 1
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    try:
        pipeline = Pipeline(schema=args.schema, chunk_size=args.chunk_size,
                            resolve_leads=not args.no_resolve_leads)
        result = pipeline.run(connector, limit=args.limit, dry_run=args.dry_run)
    except SchemaError as exc:
        logger.error("%s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - an unattended run must log and exit non-zero, never traceback (L-013)
        logger.exception("marketing ingest failed before it could run: %s", type(exc).__name__)
        return 1

    print()
    print(f"  file            {path.name}")
    print(f"  connector       {args.connector}   (source {result.source})" + ("   (DRY RUN - nothing was written)" if args.dry_run else ""))
    print(f"  schema          {result.schema}" + ("   (not touched)" if args.dry_run else ""))
    print(f"  batch           {result.batch_id}")
    print(f"  records read    {result.rows_read}")
    print(f"  landed raw      {result.rows_landed}")
    print(f"  rejected        {result.rows_invalid}")
    print(f"  duplicates      {result.rows_duplicate}   (identity seen twice in this run: the later row replaced the earlier)")
    print(f"  key collisions  {result.collisions_kept} kept as separate rows (same natural key, different row), "
          f"{result.collisions_replaced} replaced")
    if args.dry_run:
        print(f"  would write     {result.rows_read - result.rows_invalid - result.rows_duplicate}   (valid, distinct rows)")
    else:
        print(f"  written to fact {result.rows_loaded}   ({result.rows_inserted} new, {result.rows_updated} restated)")
        print(f"  restated changed {result.rows_restated_changed}   (of the restated rows: stored numbers that DIFFER from this file; "
              f"the other {result.rows_updated - result.rows_restated_changed} were identical re-imports)")
    for line in money_lines(result.money):
        print(line)
    if result.partitions_created:
        print(f"  new partitions  {', '.join(result.partitions_created)}")
    for sample in result.invalid_samples:
        print(f"  rejected: {sample}")
    for sample in result.collision_samples:
        print(f"  collision: {sample}")
    if result.error:
        print(f"  ERROR           {result.error}")
    print(f"  status          {result.status} in {result.seconds:.1f}s")
    print()
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
