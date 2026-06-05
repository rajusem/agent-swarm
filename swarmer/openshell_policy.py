"""
OpenShell policy YAML generator for AgentSwarm sessions.

build_session_policy() assembles a complete OpenShell sandbox policy from
composable blocks based on: repos attached, agent tool, model provider,
Jira MCP enablement, and session language (golang/python).

All filesystem paths use /sandbox/ — /workspace/ is never referenced.
"""
import yaml


# ── Static base policy sections ───────────────────────────────────────────────

_BASE_FILESYSTEM_POLICY = {
    "include_workdir": True,
    "read_only": ["/usr", "/lib", "/proc", "/dev/urandom", "/app", "/etc", "/var/log"],
    "read_write": ["/sandbox", "/tmp", "/dev/null", "/home"],
}

_GO_DEVELOPMENT_BLOCK = {
    "name": "golang",
    "endpoints": [
        {"host": "proxy.golang.org", "port": 443},
        {"host": "sum.golang.org", "port": 443},
        {"host": "storage.googleapis.com", "port": 443},
    ],
    "binaries": [
        {"path": "/usr/local/go/bin/go"},
        {"path": "/usr/bin/git"},
    ],
}

_GOVULNCHECK_BLOCK = {
    "name": "govulncheck",
    "endpoints": [
        {"host": "vuln.go.dev", "port": 443},
        {"host": "storage.googleapis.com", "port": 443},
    ],
    "binaries": [
        {"path": "/usr/local/go/bin/govulncheck"},
        {"path": "/home/node/go/bin/govulncheck"},
    ],
}

_PYTHON_DEVELOPMENT_BLOCK = {
    "name": "pypi",
    "endpoints": [
        {"host": "pypi.org", "port": 443},
        {"host": "files.pythonhosted.org", "port": 443},
        {"host": "downloads.python.org", "port": 443},
    ],
    "binaries": [
        {"path": "/usr/bin/pip"},
        {"path": "/usr/local/bin/uv"},
        {"path": "/sandbox/.venv/bin/pip"},
        {"path": "/sandbox/.venv/bin/python*"},
    ],
}

_JIRA_MCP_BLOCK = {
    "name": "jira-mcp",
    "endpoints": [
        {
            "host": "*.atlassian.net",
            "port": 443,
            "protocol": "rest",
            "enforcement": "enforce",
            "access": "full",
        }
    ],
    "binaries": [
        {"path": "/usr/local/bin/jira-mcp-server"},
        {"path": "/usr/bin/python3"},
        {"path": "/sandbox/.venv/bin/python*"},
    ],
}


# ── Per-repo dynamic blocks ───────────────────────────────────────────────────

def _repo_org_name(repo) -> tuple[str, str]:
    """Extract (org, name) from a repo object, falling back to URL parsing."""
    org = getattr(repo, "org", None)
    name = getattr(repo, "name", None)
    if not org or not name:
        parts = repo.repo_url.rstrip("/").split("/")
        if len(parts) >= 2:
            name = parts[-1]
            org = parts[-2]
    return (org or "unknown"), (name or "unknown")


def _repo_slug(repo) -> str:
    org, name = _repo_org_name(repo)
    return f"{org}_{name}".replace("-", "_").replace(".", "_")


def _build_github_git_block(org: str, name: str) -> dict:
    return {
        "name": f"github-git-{org}-{name}",
        "endpoints": [
            {
                "host": "github.com",
                "port": 443,
                "protocol": "rest",
                "enforcement": "enforce",
                "rules": [
                    {"allow": {"method": "GET", "path": f"/{org}/{name}.git/info/refs*"}},
                    {"allow": {"method": "POST", "path": f"/{org}/{name}.git/git-upload-pack"}},
                    {"allow": {"method": "POST", "path": f"/{org}/{name}.git/git-receive-pack"}},
                ],
            }
        ],
        "binaries": [{"path": "/usr/bin/git"}],
    }


def _build_github_api_block(org: str, name: str) -> dict:
    return {
        "name": f"github-api-{org}-{name}",
        "endpoints": [
            {
                "host": "api.github.com",
                "port": 443,
                "path": f"/repos/{org}/{name}/**",
                "protocol": "rest",
                "enforcement": "enforce",
                "rules": [{"allow": {"method": "*", "path": f"/repos/{org}/{name}/**"}}],
            }
        ],
        "binaries": [
            {"path": "/usr/bin/gh"},
            {"path": "/usr/bin/curl"},
        ],
    }


# ── Agent API block (tool and model-dependent) ────────────────────────────────

def _build_agent_api_block(agent_tool: str, model: str) -> dict:
    if agent_tool == "crush":
        block = {
            "name": "agent-api",
            "endpoints": [
                {"host": "*.aiplatform.googleapis.com", "port": 443},
                {"host": "generativelanguage.googleapis.com", "port": 443},
                {"host": "api.anthropic.com", "port": 443},
                {"host": "api.openai.com", "port": 443},
            ],
            "binaries": [
                {"path": "/usr/local/bin/crush"},
            ],
        }
    else:
        block = {
            "name": "agent-api",
            "endpoints": [
                {"host": "*.aiplatform.googleapis.com", "port": 443},
                {"host": "generativelanguage.googleapis.com", "port": 443},
                {"host": "oauth2.googleapis.com", "port": 443},
                {"host": "api.anthropic.com", "port": 443},
                {"host": "opencode.ai", "port": 443},
            ],
            "binaries": [
                {"path": "/usr/local/bin/opencode"},
                {"path": "/usr/bin/node"},
            ],
        }
    return {"agent_api": block}


# ── Public API ────────────────────────────────────────────────────────────────

def build_session_policy(
    session,
    repos: list,
    mcp_servers: list,
    agent_tool: str,
    model: str,
) -> str:
    """Assemble a complete OpenShell sandbox policy YAML for this session."""
    network_policies: dict = {}

    network_policies.update(_build_agent_api_block(agent_tool, model))

    for repo in repos:
        slug = _repo_slug(repo)
        org, name = _repo_org_name(repo)
        network_policies[f"github_git_{slug}"] = _build_github_git_block(org, name)
        network_policies[f"github_api_{slug}"] = _build_github_api_block(org, name)

    if any(getattr(mcp, "slug", None) == "jira" for mcp in (mcp_servers or [])):
        network_policies["jira_mcp"] = _JIRA_MCP_BLOCK

    lang = getattr(session, "language", "golang")
    if lang == "golang":
        network_policies["golang"] = _GO_DEVELOPMENT_BLOCK
        network_policies["govulncheck"] = _GOVULNCHECK_BLOCK
    elif lang == "python":
        network_policies["pypi"] = _PYTHON_DEVELOPMENT_BLOCK

    policy = {
        "version": 1,
        "filesystem_policy": _BASE_FILESYSTEM_POLICY,
        "landlock": {"compatibility": "best_effort"},
        "process": {"run_as_group": "sandbox", "run_as_user": "sandbox"},
        "network_policies": network_policies,
    }

    return yaml.dump(policy, default_flow_style=False, allow_unicode=True)
