from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Data root (all runtime paths derived from this) ──
    data_dir: Optional[str] = None

    # ── PostgreSQL primitives (DATABASE_URL auto-derived if not set) ──
    pg_user: str = "qvault"
    pg_password: str = "postgres"
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_db: str = "qvault"

    # Database
    database_url: str = ""
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_recycle: int = (
        300  # seconds — recycle connections before PG/pgBouncer timeout
    )

    # VLM
    vlm_base_url: str = "http://vlm-server:8000/v1"
    vlm_api_key: str = "dummy"
    vlm_model: str = "your-vlm-model-name"
    vlm_embedding_model: str = "your-embedding-model-name"
    vlm_max_concurrency: int = 5
    vlm_retry_count: int = 2
    vlm_timeout: float = 120.0  # seconds per VLM API request
    subprocess_timeout: float = 300.0  # seconds for LibreOffice/pdftoppm

    # Qwen3.5 sampling — Instruct mode for general tasks
    vlm_temperature: float = 0.7
    vlm_top_p: float = 0.8
    vlm_top_k: int = 20
    vlm_min_p: float = 0.0
    vlm_presence_penalty: float = 1.5
    vlm_repetition_penalty: float = 1.0

    # Upload
    upload_dir: str = ""
    max_upload_size_mb: int = 100

    # Logging
    log_dir: str = ""

    # Auth — OIDC (app handles the full OAuth 2.0 flow)
    auth_public_key_path: str = ""
    oidc_issuer_url: str = ""
    oauth2_client_id: str = ""
    oauth2_client_secret: str = ""
    oauth2_redirect_url: str = ""  # e.g. http://qvault.example.com/auth/callback
    session_secret: str = ""  # signs session cookies; auto-derived if not set
    dev_skip_auth: bool = False

    # Mock data mode — serves placeholder data without DB/VLM
    mock_data: bool = False

    model_config = {"env_file": ".env"}

    @model_validator(mode="after")
    def _derive_defaults(self) -> "Settings":
        root = self.data_dir

        # Derive paths from DATA_DIR when individual vars are not explicitly set
        if not self.upload_dir:
            self.upload_dir = f"{root}/uploads" if root else "./uploads"
        if not self.log_dir:
            self.log_dir = f"{root}/logs" if root else "./logs"
        if not self.auth_public_key_path:
            self.auth_public_key_path = (
                f"{root}/keys/public.pem" if root else "./keys/public.pem"
            )

        # Derive SESSION_SECRET from OAUTH2_CLIENT_SECRET if not explicitly set
        if not self.session_secret and self.oauth2_client_secret:
            import hashlib

            self.session_secret = hashlib.sha256(
                self.oauth2_client_secret.encode()
            ).hexdigest()

        # Derive DATABASE_URL from PG_* primitives when not explicitly set
        if not self.database_url:
            pw = quote_plus(self.pg_password)
            self.database_url = (
                f"postgresql+asyncpg://{self.pg_user}:{pw}"
                f"@{self.pg_host}:{self.pg_port}/{self.pg_db}"
            )

        return self

    @property
    def upload_path(self) -> Path:
        p = Path(self.upload_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def images_path(self) -> Path:
        p = self.upload_path / "images"
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
