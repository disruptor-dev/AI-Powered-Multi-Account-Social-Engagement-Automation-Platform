from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.workspace import Workspace
from app.services.account_connections import ensure_default_workspace


def active_workspace(db: Session) -> Workspace:
    """Return the tenant selected by the authenticated request, with legacy fallback."""
    raw_id = db.info.get("active_workspace_id")
    if raw_id:
        try:
            workspace = db.get(Workspace, uuid.UUID(str(raw_id)))
        except (ValueError, TypeError):
            workspace = None
        if workspace is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Selected workspace is unavailable")
        return workspace
    # Supports internal/bootstrap code paths that run before an authenticated request.
    return ensure_default_workspace(db)
