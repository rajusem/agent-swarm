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
    max_concurrent_agents: int = 5

    # OpenShell integration (feature-flagged — K8s pod path is the default)
    openshell_enabled: bool = False
    openshell_gateway_url: str = ""
    openshell_supervisor_url: str = ""
    openshell_tls_cert: str = ""        # path to client TLS cert
    openshell_tls_key: str = ""         # path to client TLS key
    openshell_tls_ca: str = ""          # path to CA bundle
    openshell_bearer_token: str = ""    # bearer token for gateway/supervisor auth
    sandbox_gc_interval: int = 300      # seconds between sandbox GC sweeps

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
