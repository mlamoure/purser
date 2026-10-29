"""QIF import API (wizard step 4 + the Import page): mapping-first preview and
commit with per-account cutoffs and sign inversion (issue #125). Shares the
preview/result response models and the planner with CSV import (issue #138)."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from purser import networth, qif_import
from purser.admin.auth import ReadToken, WriteToken
from purser.api.import_models import (
    AccountOptions,
    ImportPreviewResponse,
    ImportResult,
    service_options,
)
from purser.core.qif import QifParseError
from purser.db import get_session
from purser.ledger import LedgerError

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/import/qif", tags=["import"])


class QifPreviewRequest(BaseModel):
    content: str
    date_format: str = "mdy"
    # Optional here (the preview is explorable before mapping is complete);
    # provided targets are resolved and 422 on unknown, exactly like import.
    account_map: dict[str, str] | None = None
    account_options: dict[str, AccountOptions] = {}


_ACCOUNT_MAP_DOC = (
    "QIF account name -> Purser account id or path. An investment account (one with a "
    "`!Type:Invst` section, or declared Invst/Port/401(k)/403(b)/Mutual) may be omitted: it "
    "is then left out, and bank-side transfers into it import as one-leg rows. Its "
    "investment records are counted `skipped_investment` either way."
)


class QifImportRequest(BaseModel):
    content: str
    date_format: str = "mdy"
    account_map: dict[str, str] = Field(description=_ACCOUNT_MAP_DOC)
    category_map: dict[str, str] = {}  # QIF category -> category path
    account_options: dict[str, AccountOptions] = {}


@router.post("/preview")
def preview(body: QifPreviewRequest, token: ReadToken, session: DbSession) -> ImportPreviewResponse:
    """DB-aware, read-only mapping preview: per-file-account target, suggested
    cutoff (feed earliest minus 3d), sign diagnostic, and REAL dedup/cutoff
    counts for the supplied options. Writes nothing."""
    try:
        return ImportPreviewResponse.model_validate(
            qif_import.preview(
                session,
                body.content,
                date_format=body.date_format,
                account_map=body.account_map,
                account_options=service_options(body.account_options),
            )
        )
    except QifParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except LedgerError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("")
def import_qif(
    body: QifImportRequest,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> ImportResult:
    """Run the import through the same content-hash dedup pipeline as the
    feed — re-running the same request is a no-op; transfers link, never
    double; rows inside a per-account cutoff are left to the feed."""
    try:
        stats = qif_import.import_qif(
            session,
            content=body.content,
            account_map=body.account_map,
            category_map=body.category_map,
            date_format=body.date_format,
            account_options=service_options(body.account_options),
            token=token,
        )
    except QifParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except LedgerError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    session.commit()
    # Historical imports backdate balances; re-materialize net_worth_daily
    # from the watermark without blocking the response (decision 38).
    background_tasks.add_task(networth.consume_watermark)
    return ImportResult.model_validate(stats)
