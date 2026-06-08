"""
OpenShell policy proto builder for AgentSwarm sessions.

build_session_policy() assembles a complete OpenShell SandboxPolicy proto from
composable blocks based on: repos attached, agent tool, model provider,
Jira MCP enablement, and session language (golang/python).

Network policies are included directly in the returned SandboxPolicy proto so
the sandbox starts with all required network access pre-approved.  This avoids
the fragile probe-deny-approve cycle that depended on a hardcoded sleep timer
and was the root cause of intermittent git clone failures (ACM-34909).

IMPORTANT — harness: True on all binary entries:
  OPA resolves binary paths by traversing /proc/{pid}/root inside the sandbox
  container.  This fails with "Cannot access container filesystem for symlink
  resolution" for binaries that are installed at paths OPA cannot traverse.
  Setting harness=True tells OPA to use its process harness for binary matching
  instead of symlink resolution, which works reliably regardless of the binary's
  installation path.  The supervisor generates harness=True automatically when it
  creates draft chunks from denial analysis — we must match that behaviour in all
  statically pre-set rules (confirmed by inspecting live sandbox draft chunk state).

Returns a SandboxPolicy proto object that is set directly on SandboxSpec.policy.
All filesystem paths use /sandbox/ — /workspace/ is never referenced.
"""


def _bin(path: str) -> dict:
    """Binary entry with harness=True so OPA uses process harness matching."""
    return {"path": path, "harness": True}


# ── Static base policy sections ───────────────────────────────────────────────

_BASE_FILESYSTEM = {
    "include_workdir": True,
    "read_only": ["/usr", "/lib", "/proc", "/dev/urandom", "/app", "/etc", "/var/log"],
    "read_write": ["/sandbox", "/tmp", "/dev/null", "/home/sandbox"],
}

_GO_DEVELOPMENT_BLOCK = {
    "name": "golang",
    "endpoints": [
        {"host": "proxy.golang.org", "port": 443},
        {"host": "sum.golang.org", "port": 443},
        {"host": "storage.googleapis.com", "port": 443},
    ],
    "binaries": [
        _bin("/usr/local/go/bin/go"),
        _bin("/usr/local/bin/git"),  # agent container image path (confirmed via OPA logs)
        _bin("/usr/bin/git"),        # fallback for other base images
    ],
}

_GOVULNCHECK_BLOCK = {
    "name": "govulncheck",
    "endpoints": [
        {"host": "vuln.go.dev", "port": 443},
        {"host": "storage.googleapis.com", "port": 443},
    ],
    "binaries": [
        _bin("/usr/local/go/bin/govulncheck"),
        _bin("/home/node/go/bin/govulncheck"),
        _bin("/home/sandbox/go/bin/govulncheck"),  # sandbox user GOPATH variant
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
        _bin("/usr/bin/pip"),
        _bin("/usr/local/bin/uv"),
        _bin("/sandbox/.venv/bin/pip"),
        _bin("/sandbox/.venv/bin/python*"),
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
        _bin("/usr/local/bin/jira-mcp-server"),
        _bin("/usr/bin/python3"),
        _bin("/sandbox/.venv/bin/python*"),
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
    """Build the Landlock network policy block for git clone of a GitHub repo.

    git clone over HTTPS touches three hosts:
      - github.com:443                    — smart HTTP protocol (info/refs + upload-pack)
      - objects.githubusercontent.com:443 — pack-file / blob CDN (actual object data)
      - codeload.github.com:443           — shallow clone pack data (--depth=1 tarballs)

    All three must be accessible to the git binary or the clone will fail partway
    through even if the initial ref-discovery handshake succeeds (ACM-34909).

    Both /usr/local/bin/git and /usr/bin/git are listed — the agent container image
    installs git at /usr/local/bin/git, but some base images use /usr/bin/git.
    OPA matches on the actual resolved binary path so both must be present.
    """
    return {
        "name": f"github-git-{org}-{name}",
        "endpoints": [
            {
                # Full access to github.com for git smart HTTP protocol.
                # Path-scoped rules caused 403s because OPA enforces at the TLS CONNECT
                # layer before git can send the HTTP request (ACM-34909).
                "host": "github.com",
                "port": 443,
                "protocol": "rest",
                "enforcement": "enforce",
                "access": "full",
            },
            {
                # Pack-file object data served from GitHub's CDN — needed for all clones.
                "host": "objects.githubusercontent.com",
                "port": 443,
                "protocol": "rest",
                "enforcement": "enforce",
                "access": "full",
            },
            {
                # Shallow clone (--depth=1) pack data and tarball downloads.
                "host": "codeload.github.com",
                "port": 443,
                "protocol": "rest",
                "enforcement": "enforce",
                "access": "full",
            },
        ],
        "binaries": [
            _bin("/usr/local/bin/git"),  # agent container image path (confirmed via OPA logs)
            _bin("/usr/bin/git"),         # fallback for other base images
        ],
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
            _bin("/usr/bin/gh"),
            _bin("/usr/bin/curl"),
        ],
    }


# ── Agent API block (tool and model-dependent) ────────────────────────────────

def _endpoint(host: str) -> dict:
    return {
        "host": host,
        "port": 443,
        "protocol": "rest",
        "enforcement": "enforce",
        "access": "full",
    }


def _build_agent_api_block(agent_tool: str, model: str) -> dict:
    if agent_tool == "crush":
        block = {
            "name": "agent-api",
            "endpoints": [
                _endpoint("generativelanguage.googleapis.com"),
            ],
            "binaries": [
                _bin("/usr/local/bin/crush"),
            ],
        }
    else:
        block = {
            "name": "agent-api",
            "endpoints": [
                _endpoint("generativelanguage.googleapis.com"),
                _endpoint("oauth2.googleapis.com"),
                _endpoint("opencode.ai"),
                _endpoint("models.dev"),
            ],
            # opencode ships as a native binary installed via npm.
            # The npm wrapper at /usr/local/share/npm-global/bin/opencode invokes
            # the actual native binary at .../opencode-linux-x64/bin/opencode
            # (copied to opencode.exe by the Containerfile).  OPA sees the native
            # binary path, so both paths must be listed with harness=True.
            "binaries": [
                _bin("/usr/local/share/npm-global/lib/node_modules/opencode-linux-x64/bin/opencode"),
                _bin("/usr/local/share/npm-global/bin/opencode"),
                _bin("/usr/local/share/npm-global/lib/node_modules/opencode-ai/bin/opencode.exe"),
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
):
    """Assemble a complete OpenShell SandboxPolicy proto for this session.

    Network policies are pre-computed from the session's repos, agent tool,
    model provider, MCP configuration, and language — and are included
    directly in the returned SandboxPolicy proto.  This means the sandbox
    starts with all required Landlock network rules already approved, so git
    clone and AI API calls work immediately without any probe-deny-approve
    cycle (ACM-34909).

    Returns a SandboxPolicy proto object to be set on SandboxSpec.policy.
    """
    from google.protobuf.json_format import ParseDict
    from openshell._proto import openshell_pb2

    network_policies_dict = build_session_network_policies(
        session, repos, mcp_servers, agent_tool, model
    )

    policy_dict = {
        "version": 1,
        "filesystem": _BASE_FILESYSTEM,
        "landlock": {"compatibility": "best_effort"},
        "process": {"run_as_group": "sandbox", "run_as_user": "sandbox"},
        "network_policies": network_policies_dict,
    }
    # Get SandboxPolicy class from a SandboxSpec instance
    policy_instance = openshell_pb2.SandboxSpec().policy.__class__()
    return ParseDict(policy_dict, policy_instance, ignore_unknown_fields=True)


def build_session_network_policies(
    session,
    repos: list,
    mcp_servers: list,
    agent_tool: str,
    model: str,
) -> dict:
    """Return the computed network_policies dict for this session.

    Called by build_session_policy() to populate spec.policy.network_policies
    at sandbox creation time.  Also exposed directly for testing and for the
    policy-extract smoke test harness (scripts/openshell_smoke_test.py).
    """
    network_policies_dict: dict = {}
    network_policies_dict.update(_build_agent_api_block(agent_tool, model))

    for repo in repos:
        slug = _repo_slug(repo)
        org, name = _repo_org_name(repo)
        network_policies_dict[f"github_git_{slug}"] = _build_github_git_block(org, name)
        network_policies_dict[f"github_api_{slug}"] = _build_github_api_block(org, name)

    if any(getattr(mcp, "slug", None) == "jira" for mcp in (mcp_servers or [])):
        network_policies_dict["jira_mcp"] = _JIRA_MCP_BLOCK

    lang = getattr(session, "language", "golang")
    if lang == "golang":
        network_policies_dict["golang"] = _GO_DEVELOPMENT_BLOCK
        network_policies_dict["govulncheck"] = _GOVULNCHECK_BLOCK
    elif lang == "python":
        network_policies_dict["pypi"] = _PYTHON_DEVELOPMENT_BLOCK

    return network_policies_dict
