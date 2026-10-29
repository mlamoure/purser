"""AI/deterministic categorization API."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from purser import appsettings
from purser import categorize as service
from purser.admin.auth import ReadToken, WriteToken
from purser.db import get_session

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/categorize", tags=["categorize"])


@router.get("/status")
def status(token: ReadToken) -> dict[str, str | bool]:
    """Provider configuration (never the key). The core works with AI off."""
    settings = appsettings.effective()
    return {
        "provider": settings.ai_provider,
        "model": settings.ai_model or "",
        "configured": settings.ai_provider != "none",
    }


@router.post("/run")
def run(token: WriteToken, session: DbSession) -> dict[str, int]:
    """Categorize uncategorized transactions: deterministic payee-memory
    first, configured model only for misses. Pending rows are edited in
    place; posted rows become reversal+replacement corrections. Provenance
    lands in the audit log."""
    stats = service.run(session, token=token)
    session.commit()
    return stats
