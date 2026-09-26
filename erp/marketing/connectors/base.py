"""What a connector has to provide - the whole contract for adding a new marketing source.

A connector does exactly two things:

  * `records()`     yields the raw records of its source, one dict per record, in whatever shape
                    the source uses. They are stored unchanged in the landing table.
  * `map_record()`  turns ONE raw record into the pipeline's common shape (erp/marketing/model.py).
                    It may return None to say "this record is not an interaction" (a header row, a
                    totals line, an empty object).

Everything else - typing, validation, dedupe keys, dimensions, partitions, bulk loading, the ingest
journal - is shared pipeline code and must not be re-implemented per source.

Failure philosophy is the project's usual one (erp/config.py, erp/ai_client.py, erp/clickup_client.py):
a connector logs and degrades, it does not crash its caller. `map_record` raising for ONE record costs
that record (it is counted as invalid and skipped, lesson L-044), never the batch.

Run: imported; see erp/marketing/connectors/flat_file.py for the worked example.
"""
from __future__ import annotations

import abc
from typing import Iterator


class ConnectorError(Exception):
    """The connector cannot even start on this input (an export whose header is not the shape it maps, dates it would
    have to guess at). Raised BEFORE any row is read into the pipeline or any database is touched; the command line
    prints the message and exits 1. A bad ROW is different: that is `model.InvalidRecord`, counted and skipped."""


class Connector(abc.ABC):
    """Base class for every marketing source. Subclasses set `source_key` and implement the two methods."""

    #: Stable key of the source system, slug form ('flat_file', 'facebook_ads'). It becomes a row in
    #: marketing_source and is part of every dedupe key, so changing it re-imports everything.
    source_key: str = "unknown"

    @property
    def name(self) -> str:
        """Human-readable connector name, stored on the ingest run (never a credential)."""
        return type(self).__name__

    def origin(self) -> str | None:
        """Where this batch came from - a file name, an API date window. Shown in the ingest journal."""
        return None

    @abc.abstractmethod
    def records(self) -> Iterator[dict]:
        """Yield the raw records, exactly as the source gives them."""

    @abc.abstractmethod
    def map_record(self, raw: dict) -> dict | None:
        """One raw record -> the common shape (a plain dict), or None when it is not an interaction."""
