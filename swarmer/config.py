from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    auth_hash_file: Path = Path("auth/password.hash")
    database_url: str = "sqlite+aiosqlite:///data/swarmer.db"
    k8s_in_cluster: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    agent_image: str = ""
    agent_image_opencode: str = ""
    agent_image_python: str = ""
    agent_image_crush: str = ""
    crush_version: str = "0.57.0"
    default_agent_tool: str = "opencode"
    crush_server_port: int = 4096
    agent_image_pull_secret: str = ""
    k8s_namespace: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
