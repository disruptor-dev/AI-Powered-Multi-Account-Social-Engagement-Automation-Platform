import logging
import re
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.health import router as health_router
from app.api.routes.auth import router as auth_router
from app.api.routes.accounts import router as accounts_router
from app.api.routes.webhooks import router as webhooks_router
from app.api.routes.comments import router as comments_router
from app.api.routes.automation import router as automation_router
from app.api.routes.team import router as team_router
from app.api.routes.workspaces import router as workspaces_router
from app.api.routes.analytics import router as analytics_router
from app.api.routes.youtube_comment_queue import router as youtube_comment_queue_router
from app.core.config import get_settings

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

app = FastAPI(
    title=settings.app_name,
    description="Multi-workspace social engagement management with unified comments, review-first AI automation, secure team access, and analytics reporting.",
    version="0.8.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-CSRF-Token"],
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(accounts_router)
app.include_router(webhooks_router)
app.include_router(comments_router)
app.include_router(automation_router)
app.include_router(team_router)
app.include_router(workspaces_router)
app.include_router(analytics_router)
app.include_router(youtube_comment_queue_router)


@app.middleware("http")
async def security_response_headers(request, call_next):
    """Attach a traceable request ID and conservative browser security headers."""
    incoming = request.headers.get("X-Request-ID", "")
    request_id = incoming if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", incoming) else uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers.setdefault("X-Request-ID", request_id)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if settings.is_production and request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/", tags=["root"], summary="API information")
def root() -> dict[str, str]:
    return {
        "name": settings.app_name,
        "status": "running",
        "docs": "/docs",
        "health": "/health",
        "readiness": "/ready",
    }
