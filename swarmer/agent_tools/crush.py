import base64
import json
import shlex

from swarmer.agent_tools import AgentToolStrategy
from swarmer.config import settings


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


_SMALL_MODEL = "gemini/gemini-3.5-flash"


def _derive_small_model(model: str) -> str | None:
    """Return the small model paired with the given large model, or None if same."""
    if model == _SMALL_MODEL:
        return None  # already the small model — no separate small needed
    return _SMALL_MODEL


class CrushStrategy(AgentToolStrategy):

    @property
    def name(self) -> str:
        return "crush"

    @property
    def display_name(self) -> str:
        return "Crush"

    def get_image(self) -> str:
        if not settings.agent_image_crush:
            raise ValueError(
                "AGENT_IMAGE_CRUSH is not set. "
                "Set it in .env or as an environment variable to the Crush container image "
                "(e.g. quay.io/jpacker/crush:0.2.1)."
            )
        return settings.agent_image_crush

    def get_config_map_name(self) -> str:
        return "crush-config"

    def build_config_data(self, secret=None, mcp_servers=None, use_inference_local: bool = False, model: str = "") -> dict[str, str]:
        # Crush requires explicit provider entries in the config — it does not
        # auto-detect from env vars alone.  The value is a map[string]ProviderConfig
        # (keyed by provider ID), NOT an array.  Use $VAR references so values are
        # resolved at runtime from whatever the sandbox environment provides
        # (injected by the OpenShell provider mechanism or K8s secret env vars).
        providers = {
            "gemini": {
                "name": "Google Gemini",
                "type": "gemini",
                "api_key": "$GOOGLE_API_KEY",
            },
        }

        config = {
            "$schema": "https://charm.land/crush.json",
            "options": {
                "disable_metrics": True,
                "disable_notifications": True,
                "data_directory": ".crush",
                "auto_lsp": True,
            },
            "providers": providers,
            "lsp": {
                "go": {"command": "gopls"},
                "python": {"command": "pyright-langserver", "args": ["--stdio"]},
            },
        }

        if mcp_servers:
            mcp_config = {}
            for srv in mcp_servers:
                mcp_config[srv.slug] = {
                    "type": "stdio",
                    "command": "jira-mcp-server",
                    "env": {
                        "JIRA_SERVER_URL": "$JIRA_SERVER_URL",
                        "JIRA_ACCESS_TOKEN": "$JIRA_ACCESS_TOKEN",
                        "JIRA_EMAIL": "$JIRA_EMAIL",
                    },
                }
            if mcp_config:
                config["mcp"] = mcp_config

        return {
            "crush.json": json.dumps(config, indent=2),
            "gitconfig": "[safe]\n\tdirectory = *\n",
        }

    def get_config_mount_path(self) -> str:
        return "/workspace/.config/crush"

    def get_secret_name(self) -> str:
        return "crush-secret"

    def get_container_name(self) -> str:
        return "crush"

    def get_server_port(self) -> int | None:
        return settings.crush_server_port

    def get_share_dir(self) -> str:
        return "$HOME/.local/share/crush"

    def build_share_setup_cmd(self) -> str:
        return (
            "mkdir -p /workspace/.crush $HOME/.local/share && "
            "rm -rf $HOME/.local/share/crush && "
            "ln -sf /workspace/.crush $HOME/.local/share/crush && "
        )

    def build_model_setup_cmd(self, model: str) -> str:
        if not model:
            return ""
        if "/" in model:
            provider_id, model_id = model.split("/", 1)
        else:
            provider_id, model_id = "", model
        large = {"model": model_id, "provider": provider_id}
        models_cfg: dict = {"large": large}

        small = _derive_small_model(model)
        if small:
            sp, sm = small.split("/", 1)
            models_cfg["small"] = {"model": sm, "provider": sp}

        config_data = json.dumps({"models": models_cfg})
        return (
            "mkdir -p $HOME/.local/share/crush && "
            f"printf '%s' {shlex.quote(config_data)} "
            "> $HOME/.local/share/crush/crush.json && "
        )

    def build_main_cmd(self, session, model: str, resolved_prompt: str = "") -> str:
        if session.mode == "server":
            port = settings.crush_server_port
            return f"crush server --host tcp://0.0.0.0:{port}"
        elif session.mode == "tui":
            return "sleep infinity"
        else:
            base_parts = ["crush", "run"]
            # Do not pass --model on the CLI — Crush validates it against its
            # built-in catalogue and rejects unknown IDs like gemini-3.1-pro-preview.
            # The model is already written to crush.json by build_model_setup_cmd.
            prompt_text = resolved_prompt or session.instruction_prompt
            prompt_parts = [prompt_text] if prompt_text else []
            return " ".join(shlex.quote(p) for p in base_parts + prompt_parts)

    def get_server_mode_ports(self) -> list:
        from kubernetes import client
        port = settings.crush_server_port
        return [client.V1ContainerPort(container_port=port, name="crush")]

    def is_valid_model(self, model: str) -> bool:
        return model.startswith("gemini/")

    def get_model_options(self, secret=None) -> list[dict]:
        options = []
        if secret and getattr(secret, "google_api_key_enc", ""):
            options.extend([
                {"value": "gemini/gemini-3.5-flash", "label": "Gemini 3.5 Flash (fast)", "group": "Gemini"},
                {"value": "gemini/gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro", "group": "Gemini"},
            ])
        return options

    def get_default_model(self, has_adc: bool, has_gemini: bool) -> str:
        return "gemini/gemini-3.1-pro-preview"

    def exec_model_update(self, pod_name: str, namespace: str, model: str) -> None:
        pass

    def get_env_from_sources(self, secret_name: str = "") -> list:
        from kubernetes import client
        return [
            client.V1EnvFromSource(
                secret_ref=client.V1SecretEnvSource(
                    name=secret_name or "crush-secret", optional=True
                )
            )
        ]

    def get_extra_env(self, has_adc: bool) -> list:
        from kubernetes import client
        env = []
        if has_adc:
            env.append(client.V1EnvVar(
                name="GOOGLE_APPLICATION_CREDENTIALS",
                value="/app/gcloud/credentials.json",
            ))
        return env

    def get_extra_volumes(self, has_adc: bool, secret_name: str = "") -> list:
        from kubernetes import client
        volumes = []
        if has_adc:
            volumes.append(
                client.V1Volume(
                    name="gcloud-creds",
                    secret=client.V1SecretVolumeSource(
                        secret_name=secret_name or "crush-secret",
                        items=[
                            client.V1KeyToPath(
                                key="application_default_credentials.json",
                                path="credentials.json",
                            )
                        ],
                    ),
                )
            )
        return volumes

    def get_extra_volume_mounts(self, has_adc: bool) -> list:
        from kubernetes import client
        mounts = []
        if has_adc:
            mounts.append(
                client.V1VolumeMount(
                    name="gcloud-creds",
                    mount_path="/app/gcloud",
                    read_only=True,
                )
            )
        return mounts

    def build_k8s_secret_data(self, secret) -> dict[str, str]:
        data = {}
        if secret.google_api_key_enc:
            data["GEMINI_API_KEY"] = _b64(secret.google_api_key)
        return data

    def build_mcp_config_cmd(self, mcp_servers) -> str:
        config = {
            "$schema": "https://charm.land/crush.json",
            "options": {
                "disable_metrics": True,
                "disable_notifications": True,
                "data_directory": ".crush",
                "auto_lsp": True,
            },
            "lsp": {
                "go": {"command": "gopls"},
                "python": {"command": "pyright-langserver", "args": ["--stdio"]},
            },
        }
        if mcp_servers:
            mcp_config = {}
            for srv in mcp_servers:
                mcp_config[srv.slug] = {
                    "type": "stdio",
                    "command": "jira-mcp-server",
                    "env": {
                        "JIRA_SERVER_URL": "$JIRA_SERVER_URL",
                        "JIRA_ACCESS_TOKEN": "$JIRA_ACCESS_TOKEN",
                        "JIRA_EMAIL": "$JIRA_EMAIL",
                    },
                }
            config["mcp"] = mcp_config
        config_json = json.dumps(config)
        config_path = self.get_config_mount_path()
        return (
            f"printf '%s' {shlex.quote(config_json)} "
            f"> {config_path}/crush.json && "
        )
