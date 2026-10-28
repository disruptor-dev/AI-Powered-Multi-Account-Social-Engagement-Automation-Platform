from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.redis_client import get_redis_client
from app.core.database import engine

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness check")
def health_check() -> dict[str, str]:
    """Reports that the API process is running; it does not depend on external services."""
    settings = get_settings()
    return {"status": "ok", "service": "api", "environment": settings.app_env}


@router.get("/ready", summary="Readiness check")
def readiness_check(response: Response):
    """Returns ready only when PostgreSQL and Redis can be reached."""
    dependencies: dict[str, str] = {"database": "unknown", "redis": "unknown"}

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        dependencies["database"] = "ok"
    except (SQLAlchemyError, OSError):
        dependencies["database"] = "unavailable"

    try:
        get_redis_client().ping()
        dependencies["redis"] = "ok"
    except (RedisError, OSError, ValueError):
        dependencies["redis"] = "unavailable"

    all_ready = all(value == "ok" for value in dependencies.values())
    if not all_ready:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "dependencies": dependencies},
        )

    return {"status": "ready", "dependencies": dependencies}
