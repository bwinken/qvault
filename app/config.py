from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/fa_insight"

    vlm_base_url: str = "http://vlm-server:8000/v1"
    vlm_api_key: str = "dummy"
    vlm_model: str = "your-vlm-model-name"

    upload_dir: str = "./uploads"
    max_upload_size_mb: int = 100

    vlm_max_concurrency: int = 5
    vlm_retry_count: int = 2

    model_config = {"env_file": ".env"}


settings = Settings()
