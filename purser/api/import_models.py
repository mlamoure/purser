"""Shared request/response models for the manual-import endpoints (issue #138).

QIF (``/import/qif``) and CSV (``/import/csv``) return the identical preview and
result shapes — they ride one planner (``purser.qif_import``) — so the Pydantic
models live here and are reused by both routers. Modelling them (they were
``dict[str, Any]`` before) gives the frontend generated TypeScript types and
pins the DESIGN §7.2 counts contract in code.
"""

from datetime import date

from pydantic import BaseModel

from purser import qif_import


class AccountOptions(BaseModel):
    """Per-file-account import knobs. ``cutoff``: import only rows strictly older
    than this date (null = import everything). ``invert``: flip every amount."""

    cutoff: date | None = None
    invert: bool = False


def service_options(
    options: dict[str, AccountOptions],
) -> dict[str, qif_import.QifAccountOptions]:
    """Adapt the API option models to the service dataclass."""
    return {
        name: qif_import.QifAccountOptions(cutoff=opt.cutoff, invert=opt.invert)
        for name, opt in options.items()
    }


class ImportCounts(BaseModel):
    """Per-file-account disposition of rows (DESIGN §7.2). The preview counts for
    a request equal the commit's per-account results for the same request.
    ``in_file`` is the sum of the other buckets, ``uncleared`` excepted;
    ``skipped_scheduled`` counts Quicken reminder rows (not history, never
    imported); ``skipped_investment`` counts QIF ``!Type:Invst`` records
    (investment actions, never bank rows; decision 84); ``promoted`` counts
    rows that posted an ``expected`` row an earlier import left, because the
    file now marks them cleared (per leg for a transfer); ``uncleared`` is the
    subset of the created rows that land ``expected`` because the file marks
    them uncleared (decision 60). ``split_transfers`` (QIF ``S[Account]``
    lines carved out of split rows as transfers) and ``split_remainders``
    (split rows whose lines left part of the amount unexplained, booked to
    the uncategorized placeholder) are sub-counts too (decision 87)."""

    in_file: int
    would_import: int
    skipped_dedup: int
    skipped_cutoff: int
    skipped_unmapped: int
    skipped_zero_amount: int
    skipped_scheduled: int
    skipped_investment: int
    transfers: int
    opening_balances: int
    one_leg_transfers: int
    promoted: int
    uncleared: int
    split_transfers: int
    split_remainders: int


class SignDiagnostic(BaseModel):
    """Sign diagnostic over non-transfer rows (raw amounts). Monetary fields are
    strings — money is never a float. ``suspect`` drives the invert pre-check."""

    positive: str
    negative: str
    net: str
    suspect: bool


class MappedAccount(BaseModel):
    id: str
    path: str
    type: str


class ImportPreviewAccount(BaseModel):
    name: str
    type: str
    mapped: MappedAccount | None
    transactions: int
    default_cutoff: str | None
    counts: ImportCounts
    sign: SignDiagnostic


class ImportDateRange(BaseModel):
    start: str | None
    end: str | None


class ImportPreviewResponse(BaseModel):
    accounts: list[ImportPreviewAccount]
    categories: list[str]
    date_range: ImportDateRange
    total_transactions: int


class ImportResult(BaseModel):
    imported: int
    skipped: int
    transfers_created: int
    opening_balances: int
    skipped_cutoff: int
    skipped_unmapped: int
    skipped_zero_amount: int
    skipped_scheduled: int
    skipped_investment: int
    one_leg_transfers: int
    promoted: int
    uncleared: int
    split_transfers: int
    split_remainders: int
    transfers_absorbed: int
    transfers_auto_linked: int
    categories_created: list[str]
    per_account: dict[str, ImportCounts]
