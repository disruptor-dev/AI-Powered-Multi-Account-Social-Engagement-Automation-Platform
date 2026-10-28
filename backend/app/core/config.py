from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Social Engagement Platform API"
    app_env: Literal["development", "test", "staging", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://social_dev:change_me_only_for_local_dev@localhost:5432/social_platform"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: str = "http://localhost:3000"
    frontend_app_url: str = "http://localhost:3000"

    # The configured owner remains the bootstrap identity; active membership and workspace roles authorize sessions.
    app_admin_email: str = ""
    app_admin_password_hash: str = ""
    session_secret_key: str = ""
    session_ttl_hours: int = 8
    session_cookie_name: str = "socialdesk_session"
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    csrf_cookie_name: str = "socialdesk_csrf"
    token_encryption_key: str = ""
    default_workspace_name: str = "My workspace"

    # YouTube OAuth (Google Cloud Console -> OAuth client -> Web application).
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_redirect_uri: str = "http://localhost:8000/api/v1/accounts/youtube/callback"

    # Instagram API with Instagram Login (Meta app -> Instagram -> API setup).
    instagram_app_id: str = ""
    instagram_app_secret: str = ""
    instagram_redirect_uri: str = "http://localhost:8000/api/v1/accounts/instagram/callback"
    meta_graph_api_version: str = "v26.0"

    # Meta Webhooks: this shared token is used only for the initial GET handshake.
    # Keep it distinct from the Instagram App Secret and OAuth client secrets.
    meta_webhook_verify_token: str = ""

    # Phase 3 comment inbox and scheduled synchronization.
    comment_sync_interval_seconds: int = 300
    comment_sync_max_media_per_account: int = 50
    comment_sync_max_comments_per_media: int = 200
    comment_sync_max_replies_per_media: int = 100
    comment_sync_max_pages_per_edge: int = 10
    # External-video imports paginate independently from routine account syncs.
    comment_sync_max_external_video_comments: int = 10000
    comment_sync_max_external_video_replies: int = 10000
    comment_sync_request_timeout_seconds: float = 20.0

    # Phase 4: optional OpenAI-compatible LLM. API keys stay server-side only.
    ai_reply_enabled: bool = True
    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_request_timeout_seconds: float = 25.0
    ai_max_output_tokens: int = 280
    ai_draft_max_comments_per_rule_run: int = 500
    automation_rule_interval_seconds: int = 300
    automation_rule_lookback_limit: int = 1000

    # Phase 8: provider analytics refresh and reporting.
    analytics_sync_interval_seconds: int = 86400
    analytics_sync_lookback_days: int = 30
    analytics_data_lag_days: int = 2
    analytics_request_timeout_seconds: float = 20.0
    analytics_max_refresh_accounts: int = 50
    analytics_media_limit: int = 10

    # Phase 7: transactional email, account recovery, and distributed rate limits.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "Socialdesk"
    smtp_starttls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = 8.0
    password_reset_ttl_minutes: int = 30
    email_outbox_max_attempts: int = 5
    email_outbox_poll_seconds: int = 5

    rate_limit_enabled: bool = True
    # If unset, development fails open when Redis is unavailable; staging/production fail closed.
    rate_limit_fail_closed: bool | None = None
    rate_limit_trust_proxy_headers: bool = False
    rate_limit_login_limit: int = 10
    rate_limit_login_window_seconds: int = 60
    rate_limit_password_reset_limit: int = 5
    rate_limit_password_reset_window_seconds: int = 3600
    rate_limit_invitation_limit: int = 10
    rate_limit_invitation_window_seconds: int = 60
    rate_limit_social_action_limit: int = 30
    rate_limit_social_action_window_seconds: int = 60

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip().rstrip("/") for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def youtube_oauth_configured(self) -> bool:
        return bool(self.youtube_client_id.strip() and self.youtube_client_secret.strip())

    @property
    def instagram_oauth_configured(self) -> bool:
        return bool(self.instagram_app_id.strip() and self.instagram_app_secret.strip())

    @property
    def smtp_configured(self) -> bool:
        base_configured = bool(self.smtp_host.strip() and self.smtp_from_email.strip())
        if not base_configured:
            return False
        if self.is_production and not (self.smtp_use_ssl or self.smtp_starttls):
            return False
        return True

    @property
    def effective_rate_limit_fail_closed(self) -> bool:
        if self.rate_limit_fail_closed is not None:
            return self.rate_limit_fail_closed
        return self.app_env in ("staging", "production")

    @property
    def ai_drafting_configured(self) -> bool:
        return bool(self.ai_reply_enabled and self.ai_api_key.strip() and self.ai_base_url.strip() and self.ai_model.strip())

    @property
    def admin_login_configured(self) -> bool:
        return bool(
            self.app_admin_email.strip()
            and self.app_admin_password_hash.strip()
            and len(self.session_secret_key) >= 32
        )

    @property
    def token_encryption_configured(self) -> bool:
        return bool(self.token_encryption_key.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
