"""CSV import API (issue #138): the same mapping-first preview + commit as QIF,
for CSV bank exports (Amex activity, Quicken CSV). Rides the shared planner and
response models; adds an optional ``column_map`` (when auto-detect fails) and a
per-account skip (``account_map`` value null = "don't import").

Layout-detection failure is signalled distinctly: the preview returns 422 with
``detail = {code: "csv_layout_unrecognized", message, columns}`` so the UI can
render a column-mapping sub-form seeded with the detected headers, rather than a
generic parse error.
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from purser import networth, qif_import
from purser.admin.auth import ReadToken, WriteToken
from purser.api.import_models import (
    AccountOptions,
    ImportPreviewResponse,
    ImportResult,
    service_options,
)
from purser.core.csv_import import CsvLayoutError, CsvParseError
from purser.core.qif import QifParseError
from purser.db import get_session
from purser.ledger import LedgerError

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/import/csv", tags=["import"])

LAYOUT_UNRECOGNIZED = "csv_layout_unrecognized"


class CsvPreviewRequest(BaseModel):
    content: str
    date_format: str = "mdy"
    # A null value marks a file account "don't import" (its rows count
    # skipped_unmapped); a missing key is simply unmapped-so-far.
    account_map: dict[str, str | None] | None = None
    account_options: dict[str, AccountOptions] = {}
    # Explicit {date, amount, payee, memo?, category?, account?} -> header names,
    # supplied by the UI when auto-detection fails.
    column_map: dict[str, str] | None = None


class CsvImportRequest(BaseModel):
    content: str
    date_format: str = "mdy"
    account_map: dict[str, str | None]  # file account -> Purser id/path, or null to skip
    category_map: dict[str, str] = {}
    account_options: dict[str, AccountOptions] = {}
    column_map: dict[str, str] | None = None


def _split_map(
    account_map: dict[str, str | None] | None,
) -> tuple[dict[str, str], frozenset[str]]:
    """Split a nullable map into the resolvable targets and the skip set."""
    if not account_map:
        return {}, frozenset()
    real = {name: ref for name, ref in account_map.items() if ref is not None}
    skip = frozenset(name for name, ref in account_map.items() if ref is None)
    return real, skip


def _layout_error(exc: CsvLayoutError) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": LAYOUT_UNRECOGNIZED, "message": str(exc), "columns": exc.columns},
    )


@router.post("/preview")
def preview(body: CsvPreviewRequest, token: ReadToken, session: DbSession) -> ImportPreviewResponse:
    """DB-aware, read-only CSV mapping preview — identical contract to QIF's.
    A layout that can't be auto-detected returns 422 with a
    ``csv_layout_unrecognized`` code + the header columns for the mapping UI."""
    account_map, skip = _split_map(body.account_map)
    try:
        return ImportPreviewResponse.model_validate(
            qif_import.preview(
                session,
                body.content,
                date_format=body.date_format,
                account_map=account_map,
                account_options=service_options(body.account_options),
                fmt="csv",
                column_map=body.column_map,
                skip=skip,
            )
        )
    except CsvLayoutError as exc:
        raise _layout_error(exc) from exc
    except (CsvParseError, QifParseError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except LedgerError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("")
def import_csv(
    body: CsvImportRequest,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> ImportResult:
    """Commit a CSV import through the shared content-hash dedup pipeline —
    re-running the same request is a no-op; file accounts mapped to null are
    skipped (counted skipped_unmapped)."""
    account_map, skip = _split_map(body.account_map)
    try:
        stats = qif_import.import_qif(
            session,
            content=body.content,
            account_map=account_map,
            category_map=body.category_map,
            date_format=body.date_format,
            account_options=service_options(body.account_options),
            fmt="csv",
            column_map=body.column_map,
            skip=skip,
            token=token,
        )
    except CsvLayoutError as exc:
        raise _layout_error(exc) from exc
    except (CsvParseError, QifParseError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except LedgerError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    session.commit()
    # Same as /import/qif: backdated history dirties net_worth_daily.
    background_tasks.add_task(networth.consume_watermark)
    return ImportResult.model_validate(stats)
