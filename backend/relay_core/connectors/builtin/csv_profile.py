"""CSV profiling for uploaded attachments (docs/system-design.md section 11.5, trimmed to
CSV only — XLSX support is a straightforward follow-on once something needs it). Building a
profile (columns, a few sample rows, inferred capabilities) is cheap enough to do inline in
the upload request handler; the async ingestion pipeline in section 11.1 is for documents
(PDF/DOCX), not flat tables.

Capability inference is a column-name keyword heuristic, not the Flash-Lite classifier from
section 7.2 — good enough to make the `csv_only` connector profile demonstrable without an
extra LLM call on every upload; revisit if the heuristic's false-positive/negative rate turns
out to matter in the eval suite.
"""

import csv
import io
from typing import Any

_SAMPLE_ROWS = 5
# Profiling reads the whole file once at upload time (section 11.5: "attachments aren't
# embedded row by row"); this just guards against a pathologically large upload turning that
# one pass into an unbounded read.
_MAX_ROWS_READ = 50_000

_CAPABILITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "subscription.read": ("subscription", "plan", "renew", "expire", "mrr", "contract"),
    "usage.read": ("usage", "active_user", "api_call", "session", "event", "login"),
    "customer.read": ("customer", "account", "company", "client", "owner_email"),
}


class CSVProfile:
    def __init__(
        self, *, columns: list[str], sample_rows: list[dict[str, str]], row_count: int
    ) -> None:
        self.columns = columns
        self.sample_rows = sample_rows
        self.row_count = row_count

    def to_json(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "sample_rows": self.sample_rows,
            "row_count": self.row_count,
        }


def profile_csv(raw: bytes) -> CSVProfile:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    columns = list(reader.fieldnames or [])
    sample_rows: list[dict[str, str]] = []
    row_count = 0
    for row in reader:
        if row_count < _SAMPLE_ROWS:
            sample_rows.append(dict(row))
        row_count += 1
        if row_count >= _MAX_ROWS_READ:
            break
    return CSVProfile(columns=columns, sample_rows=sample_rows, row_count=row_count)


def read_rows(raw: bytes, *, limit: int) -> tuple[list[dict[str, str]], bool]:
    """Full(er) read for `file_upload.read_table` — separate from `profile_csv`'s fixed
    5-row sample, which exists only to describe the file, not to serve query results."""
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, str]] = []
    truncated = False
    for i, row in enumerate(reader):
        if i >= limit:
            truncated = True
            break
        rows.append(dict(row))
    return rows, truncated


def infer_capabilities(columns: list[str]) -> list[str]:
    lowered = [c.lower() for c in columns]
    return [
        capability
        for capability, keywords in _CAPABILITY_KEYWORDS.items()
        if any(keyword in col for col in lowered for keyword in keywords)
    ]
