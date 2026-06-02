from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    secret_key_file: str = "auth/secret.key"
    openshift_oauth_url: str = ""   # e.g. https://oauth-openshift.apps.example.com
    redirect_base_url: str = ""     # e.g. https://swarmer-swarmer.apps.example.com  (explicit callback base)
    k8s_api_url: str = "https://kubernetes.default.svc"
    database_url: str = "sqlite+aiosqlite:///data/swarmer.db"
    k8s_in_cluster: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    agent_image: str = ""
    agent_image_opencode: str = ""
    agent_image_crush: str = ""
    default_agent_tool: str = "opencode"
    crush_server_port: int = 4096
    agent_image_pull_secret: str = ""
    agent_image_pull_policy: str = "IfNotPresent"
    k8s_namespace: str = ""
    openshell_gateway_url: str = ""
    openshell_tls_ca_path: str = ""
    openshell_tls_cert_path: str = ""
    openshell_tls_key_path: str = ""
    openshell_bearer_token: str = ""
    openshell_enabled: bool = False
    sandbox_gc_interval: int = 600  # seconds between sandbox GC runs

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
