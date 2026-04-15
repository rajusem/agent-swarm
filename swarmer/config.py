from pathlib import Path
from pydantic_settings import BaseSettings

LANGUAGE_OPTIONS = ("golang", "python")


class Settings(BaseSettings):
    auth_hash_file: Path = Path("auth/password.hash")
    database_url: str = "sqlite+aiosqlite:///data/swarmer.db"
    k8s_in_cluster: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    agent_image: str = "opencode-golang:latest"
    agent_image_python: str = ""
    agent_image_pull_secret: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    def image_for_language(self, language: str) -> str:
        """Return the container image for the given language variant."""
        if language == "python":
            if self.agent_image_python:
                return self.agent_image_python
            return self.agent_image.replace("golang", "python")
        return self.agent_image


settings = Settings()
