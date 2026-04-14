from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    auth_hash_file: Path = Path("auth/password.hash")
    database_url: str = "sqlite+aiosqlite:///data/swarmer.db"
    k8s_in_cluster: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    agent_image: str = "opencode-golang:latest"
    agent_image_pull_secret: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
