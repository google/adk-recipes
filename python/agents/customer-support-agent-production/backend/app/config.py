import os

from pydantic_settings import BaseSettings

# Support per-environment .env files for local development.
# In Cloud Run, actual env vars (set via --set-env-vars) take priority over env_file.
_ENV_TO_FILE = {"development": "dev", "staging": "staging", "production": "prod"}
_env = os.environ.get("ENV", "")
_env_suffix = _ENV_TO_FILE.get(_env, _env)
_env_file = f"../.env.{_env_suffix}" if _env_suffix and os.path.exists(f"../.env.{_env_suffix}") else "../.env"


class Settings(BaseSettings):
    google_cloud_project: str
    google_cloud_location: str = "us-central1"
    agent_engine_resource_name: str
    frontend_url: str = "http://localhost:3000"
    port: int = 8000
    firestore_database: str = "customer-support-db"
    model_armor_enabled: bool = False
    model_armor_template_id: str = ""
    model_armor_mode: str = "INSPECT_AND_BLOCK"
    env: str = "production"

    class Config:
        env_file = _env_file
        case_sensitive = False


settings = Settings()
