from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/qvault"
    )

    # VLM
    vlm_base_url: str = "http://vlm-server:8000/v1"
    vlm_api_key: str = "dummy"
    vlm_model: str = "your-vlm-model-name"
    vlm_embedding_model: str = "your-embedding-model-name"
    vlm_max_concurrency: int = 5
    vlm_retry_count: int = 2

    # Qwen3.5 sampling — Instruct mode for general tasks
    vlm_temperature: float = 0.7
    vlm_top_p: float = 0.8
    vlm_top_k: int = 20
    vlm_min_p: float = 0.0
    vlm_presence_penalty: float = 1.5
    vlm_repetition_penalty: float = 1.0

    # Upload
    upload_dir: str = "./uploads"
    max_upload_size_mb: int = 100

    # Auth (JWT verification — oauth2-proxy handles the OAuth flow)
    auth_public_key_path: str = "./keys/public.pem"
    dev_skip_auth: bool = False

    model_config = {"env_file": ".env"}

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
